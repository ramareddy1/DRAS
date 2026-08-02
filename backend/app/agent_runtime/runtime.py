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
from . import artifacts, critic, registry, store, tools_core  # noqa: F401
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
    fn = getattr(tools_core, name, None)
    if fn is None:
        raise KeyError(f"unknown tool {name}")
    return fn(**payload)


def execute_run(
    *, run_id: str, account_id: str, driver: Optional[Driver] = None,
) -> Run:
    run = store.load_run(run_id, account_id)
    if run is None:
        raise KeyError(f"run {run_id} not found for account {account_id}")

    driver = driver or AnthropicDriver()
    budget = Budget.from_dict(run.budget)
    spend = Spend()
    tools = registry.tier1_tools()

    token = set_run_context(RunContext(run_id=run.id, account_id=account_id))
    store.set_status(
        run_id=run.id, account_id=account_id, status=RunStatus.running,
    )
    store.append_event(
        run=run, type=RunEventType.goal_received, payload=run.goal,
    )

    messages: List[Dict[str, Any]] = [
        {"role": "user", "content": _goal_text(run)},
    ]
    final_status = RunStatus.done

    try:
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

            results: List[Dict[str, Any]] = []
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

                store.append_event(
                    run=run, type=RunEventType.tool_called,
                    payload={"tool": call.name, "input": call.input},
                )
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
                    final_status = RunStatus.aborted
                    store.set_status(
                        run_id=run.id, account_id=account_id,
                        status=final_status,
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

            reason = exceeded(budget, spend)
            if reason:
                store.append_event(
                    run=run, type=RunEventType.budget_exceeded,
                    payload={"reason": reason},
                )
                final_status = RunStatus.aborted
                store.set_status(
                    run_id=run.id, account_id=account_id,
                    status=final_status, error=reason,
                )
                return store.load_run(run.id, account_id)

        store.append_event(
            run=run, type=RunEventType.run_finished,
            payload={"spend": spend.to_dict()},
        )
        store.save_transcript(
            run_id=run.id, account_id=account_id,
            transcript=messages, spend=spend.to_dict(),
        )
        store.set_status(
            run_id=run.id, account_id=account_id, status=final_status,
        )
        return store.load_run(run.id, account_id)

    except Exception as exc:
        store.set_status(
            run_id=run.id, account_id=account_id, status=RunStatus.failed,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    finally:
        reset_run_context(token)
