"""The plan-execute-verify loop.

Drives the Anthropic Tool Runner over the tier-1 registry, mirroring the
message history into `runs.transcript` so a run can suspend on a gate and
resume in a different process (spec 1.2, 2.6).

Every tool call passes three gates before its result reaches the model:
autonomy (registry.requires_gate), post-conditions (critic.check), and hard
caps (budget.exceeded). A gate that trips writes an event and stops the run
— nothing is swallowed.

The `Driver` seam exists so the loop is testable without network. Gates,
critic paths, and budget trips all need deterministic coverage, and those
cannot be scripted through a live model.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol

from ..llm import DEFAULT_MODEL
from ..models import Run, RunEventType, RunStatus
from . import artifacts, critic, registry, store, tools_core, tools_macro  # noqa: F401
from .budget import Budget, Spend, exceeded
from .context import RunContext, reset_run_context, set_run_context

SYSTEM_PROMPT = """You are an operational data agent.

You orchestrate deterministic tools over the user's data. You never compute money
yourself: every number you report must come from a tool result. If a tool has
not produced a figure, you do not have it.

Work from handles. Tools take dataset ids and column names, never rows.

Prefer acting to narrating. When a goal is small enough to answer in a few
tool calls, just make them — do not propose a plan first."""

MAX_ITERATIONS = 40


@dataclass
class ToolCall:
    id: str
    name: str
    input: Dict[str, Any]


@dataclass
class Turn:
    text: Optional[str] = None
    tool_calls: List[ToolCall] = field(default_factory=list)


class Driver(Protocol):
    def next_turn(
        self, *, system, messages, tools, task_budget_tokens,
    ) -> Turn: ...


class AnthropicDriver:
    """Wraps the SDK Tool Runner, surfacing one turn at a time.

    The runner executes registered tools itself; we intercept each yielded
    assistant message so the loop can gate before results are accepted.
    """

    def __init__(self, model: str = DEFAULT_MODEL):
        self.model = model

    def next_turn(self, *, system, messages, tools, task_budget_tokens) -> Turn:
        import os

        # Same gate the rest of the codebase uses (app/llm.py). Without this,
        # any test that reaches the runtime through HTTP makes a live call.
        if os.getenv("RECONOPS_STUB_LLM") == "1":
            return Turn(text="[stubbed] no action taken.", tool_calls=[])

        import anthropic

        client = anthropic.Anthropic()
        runner = client.beta.messages.tool_runner(
            model=self.model,
            max_tokens=16000,
            system=system,
            messages=messages,
            tools=tools,
            thinking={"type": "adaptive"},
            output_config={
                "effort": "xhigh",
                "task_budget": {"type": "tokens", "total": task_budget_tokens},
            },
            betas=["task-budgets-2026-03-13"],
        )
        message = next(iter(runner))

        text_parts = [b.text for b in message.content if b.type == "text"]
        calls = [
            ToolCall(id=b.id, name=b.name, input=dict(b.input))
            for b in message.content if b.type == "tool_use"
        ]
        return Turn(
            text="".join(text_parts) or None,
            tool_calls=calls,
        )


def _system_blocks(run: Run) -> List[Dict[str, Any]]:
    """Stable instructions first, with the cache breakpoint on them.

    Account-specific context is appended after this block in later phases;
    keeping the stable text first is what lets the prefix cache be shared
    across every account (spec 4.1).
    """
    return [{
        "type": "text",
        "text": SYSTEM_PROMPT,
        "cache_control": {"type": "ephemeral"},
    }]


def _goal_text(run: Run) -> str:
    intent = run.goal.get("intent", "")
    entities = run.goal.get("entities") or {}
    if entities:
        return f"{intent}\n\nContext: {json.dumps(entities, sort_keys=True)}"
    return intent or "Help with this account's data."


def _dispatch(name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    # Deferred: resolution is by attribute lookup on `tools_core`, not an
    # explicit allowlist, so any module-level callable there is reachable by
    # name. What keeps this safe today is `registry.requires_gate` failing
    # closed — an unregistered name always gates and never reaches here
    # unattended. Narrowing this to the registry's own table is a follow-up.
    fn = getattr(tools_core, name, None)
    if fn is None:
        raise KeyError(f"unknown tool {name}")
    return fn(**payload)


def _would_exceed(budget: Budget, spend: Spend) -> Optional[str]:
    """Would admitting one more tool call breach a cap?

    `exceeded` is a post-hoc test (`tool_calls > cap`), so asking it about
    the current spend still lets the (cap+1)-th call run. Probing with the
    pending call already counted makes the cap hard: with `tool_call_cap=N`
    exactly N tool calls execute. The probe is a copy — `spend` keeps
    recording only calls that actually ran, so the persisted spend never
    over-reports. Non-count caps (usd, wall clock) read identically on the
    probe, so this stays a strict superset of `exceeded(budget, spend)`.
    """
    probe = Spend(
        tool_calls=spend.tool_calls + 1,
        usd=spend.usd,
        started_at=spend.started_at,
    )
    return exceeded(budget, probe)


def execute_run(
    *, run_id: str, account_id: str, driver: Optional[Driver] = None,
) -> Run:
    run = store.load_run(run_id, account_id)
    if run is None:
        raise KeyError(f"run {run_id} not found for account {account_id}")

    # Only a run that has not finished may be started. This function rebuilds
    # the message history from the goal and ignores `run.transcript`, so
    # re-invoking a finished run would restart it from scratch and duplicate
    # its event log. Resuming a suspended run is a separate capability
    # (spec 2.6) and must not be silently conflated with a fresh start.
    # `running` is allowed because that is what a crashed worker leaves
    # behind, and a retry has to be able to pick it up.
    if run.status not in (RunStatus.pending, RunStatus.running):
        raise ValueError(
            f"run {run.id} is not startable: status is {run.status.value}"
        )

    driver = driver or AnthropicDriver()
    budget = Budget.from_dict(run.budget)
    spend = Spend()
    tools = registry.tier1_tools()

    messages: List[Dict[str, Any]] = [
        {"role": "user", "content": _goal_text(run)},
    ]

    # Everything from here on runs inside the try, so `finally` always clears
    # the context. A RunContext left set on a pooled worker thread is exactly
    # the cross-account hazard context.py exists to prevent, so no statement
    # that can raise may sit between the set and the try.
    token = set_run_context(RunContext(run_id=run.id, account_id=account_id))
    try:
        store.set_status(
            run_id=run.id, account_id=account_id, status=RunStatus.running,
        )
        store.append_event(
            run=run, type=RunEventType.goal_received, payload=run.goal,
        )

        for _ in range(MAX_ITERATIONS):
            turn = driver.next_turn(
                system=_system_blocks(run),
                messages=messages,
                tools=tools,
                task_budget_tokens=budget.task_budget_tokens,
            )

            assistant_content: List[Dict[str, Any]] = []
            if turn.text:
                assistant_content.append({"type": "text", "text": turn.text})
                store.append_event(
                    run=run, type=RunEventType.assistant_text,
                    payload={"text": turn.text},
                )
            for call in turn.tool_calls:
                assistant_content.append({
                    "type": "tool_use", "id": call.id,
                    "name": call.name, "input": call.input,
                })
            messages.append({"role": "assistant", "content": assistant_content})

            if not turn.tool_calls:
                break

            # Pass 1 — gate the whole turn before executing any of it.
            # Suspension is all-or-nothing per turn: if a later call needs
            # approval, an earlier read must not have already run. Otherwise
            # the saved transcript ends with an assistant message whose
            # tool_use blocks are only partly answered, and the run cannot be
            # resumed coherently.
            for call in turn.tool_calls:
                if registry.requires_gate(call.name, run.autonomy):
                    q = store.append_event(
                        run=run, type=RunEventType.question_asked,
                        payload={
                            "text": f"Run {call.name}?",
                            "tool": call.name, "input": call.input,
                        },
                    )
                    store.save_transcript(
                        run_id=run.id, account_id=account_id,
                        transcript=messages, spend=spend.to_dict(),
                    )
                    store.set_status(
                        run_id=run.id, account_id=account_id,
                        status=RunStatus.suspended, suspended_on=q.id,
                    )
                    return store.load_run(run.id, account_id)

            # Pass 2 — execute, checking the caps before each call rather
            # than after the batch. A cap that can only trip once a whole
            # turn has run is not a hard cap.
            results: List[Dict[str, Any]] = []
            for call in turn.tool_calls:
                reason = _would_exceed(budget, spend)
                if reason:
                    store.append_event(
                        run=run, type=RunEventType.budget_exceeded,
                        payload={"reason": reason},
                    )
                    store.save_transcript(
                        run_id=run.id, account_id=account_id,
                        transcript=messages, spend=spend.to_dict(),
                    )
                    store.set_status(
                        run_id=run.id, account_id=account_id,
                        status=RunStatus.aborted, error=reason,
                    )
                    return store.load_run(run.id, account_id)

                store.append_event(
                    run=run, type=RunEventType.tool_called,
                    payload={"tool": call.name, "input": call.input},
                )
                # Phase A spend tracking is tool calls and wall clock only.
                # `Spend.record_llm` is never called — `Turn` carries no token
                # usage back from the driver — so `spend.usd` stays 0.0 and
                # `budget.usd_cap` is inert: the money cap cannot trip. The
                # tool-call and wall-clock caps carry enforcement for now;
                # wiring real USD accounting is a follow-up task.
                spend.record_tool_call()

                try:
                    output = _dispatch(call.name, call.input)
                except Exception as exc:
                    store.append_event(
                        run=run, type=RunEventType.tool_failed,
                        payload={
                            "tool": call.name,
                            "error": f"{type(exc).__name__}: {exc}",
                        },
                    )
                    results.append({
                        "type": "tool_result", "tool_use_id": call.id,
                        "content": f"error: {exc}", "is_error": True,
                    })
                    continue

                verdict = critic.check(call.name, output)
                store.append_event(
                    run=run, type=RunEventType.critic_check,
                    payload={
                        "tool": call.name, "passed": verdict.passed,
                        "failures": verdict.failures,
                    },
                )
                if not verdict.passed:
                    store.save_transcript(
                        run_id=run.id, account_id=account_id,
                        transcript=messages, spend=spend.to_dict(),
                    )
                    store.set_status(
                        run_id=run.id, account_id=account_id,
                        status=RunStatus.aborted,
                        error="; ".join(verdict.failures),
                    )
                    return store.load_run(run.id, account_id)

                store.append_event(
                    run=run, type=RunEventType.tool_returned,
                    payload={"tool": call.name, "output": output},
                )
                results.append({
                    "type": "tool_result", "tool_use_id": call.id,
                    "content": json.dumps(output, default=str),
                })

            messages.append({"role": "user", "content": results})

            # Backstop: a turn that lands exactly on a cap (or runs the clock
            # out) trips here, before another model round-trip is paid for.
            reason = exceeded(budget, spend)
            if reason:
                store.append_event(
                    run=run, type=RunEventType.budget_exceeded,
                    payload={"reason": reason},
                )
                store.save_transcript(
                    run_id=run.id, account_id=account_id,
                    transcript=messages, spend=spend.to_dict(),
                )
                store.set_status(
                    run_id=run.id, account_id=account_id,
                    status=RunStatus.aborted, error=reason,
                )
                return store.load_run(run.id, account_id)
        else:
            # No `break`: the loop ran out of iterations while the model was
            # still asking for tools. The run is truncated mid-work, not
            # finished — reporting `done` here would make a cut-off run
            # indistinguishable from a completed one.
            reason = f"iteration cap reached: {MAX_ITERATIONS} model turns"
            store.append_event(
                run=run, type=RunEventType.budget_exceeded,
                payload={"reason": reason},
            )
            store.save_transcript(
                run_id=run.id, account_id=account_id,
                transcript=messages, spend=spend.to_dict(),
            )
            store.set_status(
                run_id=run.id, account_id=account_id,
                status=RunStatus.aborted, error=reason,
            )
            return store.load_run(run.id, account_id)

        # Reached only via the `break` above: the model stopped calling tools.
        store.append_event(
            run=run, type=RunEventType.run_finished,
            payload={"spend": spend.to_dict()},
        )
        store.save_transcript(
            run_id=run.id, account_id=account_id,
            transcript=messages, spend=spend.to_dict(),
        )
        store.set_status(
            run_id=run.id, account_id=account_id, status=RunStatus.done,
        )
        return store.load_run(run.id, account_id)

    except Exception as exc:
        # Persist whatever the run got through before recording the failure,
        # so the run and its event log still agree. A failure to save must
        # never mask the original exception, hence the inner guard.
        try:
            store.save_transcript(
                run_id=run.id, account_id=account_id,
                transcript=messages, spend=spend.to_dict(),
            )
        except Exception:
            pass
        store.set_status(
            run_id=run.id, account_id=account_id, status=RunStatus.failed,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    finally:
        reset_run_context(token)
