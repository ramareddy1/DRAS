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
from typing import Any, Dict, List, Optional, Protocol, Tuple

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


@dataclass
class _Executed:
    """Outcome of running one turn's tool calls.

    `abort` is set when a cap or a critic stopped the turn part-way. The
    caller persists and terminates; carrying it in the return value rather
    than raising keeps the two entry points handling it identically.
    """
    results: List[Dict[str, Any]] = field(default_factory=list)
    abort: Optional[Tuple[RunStatus, str]] = None


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
    """Execute a registered tool by name.

    Resolution is the registry's table, not an attribute lookup on a
    module: a name is executable exactly when it is registered, and
    registration is also what assigns the effect the gate reads. That
    keeps "gateable" and "runnable" from drifting apart, and it is what
    makes tools outside `tools_core` — `run_reconciliation` — dispatchable.
    """
    fn = registry.callable_for(name)
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
        accumulated_s=spend.accumulated_s,
    )
    return exceeded(budget, probe)


def _question_text(turn: Turn, gated: ToolCall) -> str:
    """The human-readable question a suspend records.

    Approval is whole-turn and all-or-nothing (design decision 1), so a turn
    carrying more than one call has to say so: naming only the call that
    tripped the gate would understate what approving actually runs. A
    single-call turn keeps the original wording verbatim — that is the
    overwhelmingly common case and there is nothing extra to disclose.
    """
    if len(turn.tool_calls) == 1:
        return f"Run {gated.name}?"
    names = ", ".join(c.name for c in turn.tool_calls)
    return (
        f"Run all {len(turn.tool_calls)} tool calls in this turn "
        f"({names})? {gated.name} is what requires approval, and "
        f"approving runs the whole turn."
    )


def _execute_calls(
    *, run: Run, account_id: str, calls: List[ToolCall],
    budget: Budget, spend: Spend,
) -> _Executed:
    """Run one turn's tool calls, checking caps before each.

    A cap that can only trip once a whole turn has run is not a hard cap,
    so the probe happens per call rather than per batch.
    """
    out = _Executed()
    for call in calls:
        reason = _would_exceed(budget, spend)
        if reason:
            store.append_event(
                run=run, type=RunEventType.budget_exceeded,
                payload={"reason": reason},
            )
            out.abort = (RunStatus.aborted, reason)
            return out

        store.append_event(
            run=run, type=RunEventType.tool_called,
            payload={"tool": call.name, "input": call.input},
        )
        # Phase A spend tracking is tool calls and wall clock only.
        # `Spend.record_llm` is never called — `Turn` carries no token usage
        # back from the driver — so `spend.usd` stays 0.0 and `budget.usd_cap`
        # is inert. The tool-call and wall-clock caps carry enforcement.
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
            out.results.append({
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
            out.abort = (RunStatus.aborted, "; ".join(verdict.failures))
            return out

        store.append_event(
            run=run, type=RunEventType.tool_returned,
            payload={"tool": call.name, "output": output},
        )
        out.results.append({
            "type": "tool_result", "tool_use_id": call.id,
            "content": json.dumps(output, default=str),
        })
    return out


def _finish_run(
    *, run: Run, account_id: str, messages: List[Dict[str, Any]],
    spend: Spend, status: RunStatus, error: Optional[str],
) -> Run:
    """Persist a run's terminal transcript, spend, and status together.

    Shared by every terminal path in `_drive` and by `resume_run`'s abort
    branch, so a change to failure persistence has exactly one place to
    happen — not one written once and hand-inlined a second time.
    """
    store.save_transcript(
        run_id=run.id, account_id=account_id,
        transcript=messages, spend=spend.to_dict(),
    )
    store.set_status(
        run_id=run.id, account_id=account_id, status=status, error=error,
    )
    return store.load_run(run.id, account_id)


def _drive(
    *, run: Run, account_id: str, messages: List[Dict[str, Any]],
    budget: Budget, spend: Spend, driver: Driver,
) -> Run:
    """The plan-execute-verify loop, shared by fresh starts and resumes.

    The caller owns the run context and the initial status; this owns the
    turns. Both entry points hand it a `messages` list that already ends
    somewhere the model can continue from.
    """
    tools = registry.tier1_tools()

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

        # A refused call must not come back around. Re-gating it would ask
        # the user the same question again and spend the run's budget on a
        # loop they already declined.
        for call in turn.tool_calls:
            if _was_rejected(run, call):
                reason = f"call already rejected by the user: {call.name}"
                store.append_event(
                    run=run, type=RunEventType.run_finished,
                    payload={"reason": "repeat_of_rejected_call",
                             "tool": call.name},
                )
                return _finish_run(
                    run=run, account_id=account_id, messages=messages,
                    spend=spend, status=RunStatus.aborted, error=reason,
                )

        # Pass 1 — gate the whole turn before executing any of it.
        # Suspension is all-or-nothing per turn: if a later call needs
        # approval, an earlier read must not have already run. Otherwise the
        # saved transcript ends with an assistant message whose tool_use
        # blocks are only partly answered, and the run cannot be resumed
        # coherently.
        for call in turn.tool_calls:
            if registry.requires_gate(call.name, run.autonomy):
                # `question_asked` is the only durable record of what the
                # human was asked, and approve executes the *whole* turn
                # (decision 1). `tool`/`input` name the call that tripped
                # the gate — existing consumers read those — and `calls`
                # lists everything approving will run, including calls that
                # did not need a gate of their own.
                q = store.append_event(
                    run=run, type=RunEventType.question_asked,
                    payload={
                        "text": _question_text(turn, call),
                        "tool": call.name, "input": call.input,
                        "calls": [
                            {"tool": c.name, "input": c.input}
                            for c in turn.tool_calls
                        ],
                    },
                )
                store.save_transcript(
                    run_id=run.id, account_id=account_id,
                    transcript=messages, spend=spend.to_dict(),
                )
                store.set_suspended_at(run_id=run.id, account_id=account_id)
                store.set_status(
                    run_id=run.id, account_id=account_id,
                    status=RunStatus.suspended, suspended_on=q.id,
                )
                return store.load_run(run.id, account_id)

        executed = _execute_calls(
            run=run, account_id=account_id, calls=turn.tool_calls,
            budget=budget, spend=spend,
        )
        if executed.abort is not None:
            status, error = executed.abort
            return _finish_run(
                run=run, account_id=account_id, messages=messages,
                spend=spend, status=status, error=error,
            )

        messages.append({"role": "user", "content": executed.results})

        # Backstop: a turn that lands exactly on a cap (or runs the clock
        # out) trips here, before another model round-trip is paid for.
        reason = exceeded(budget, spend)
        if reason:
            store.append_event(
                run=run, type=RunEventType.budget_exceeded,
                payload={"reason": reason},
            )
            return _finish_run(
                run=run, account_id=account_id, messages=messages,
                spend=spend, status=RunStatus.aborted, error=reason,
            )
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
        return _finish_run(
            run=run, account_id=account_id, messages=messages,
            spend=spend, status=RunStatus.aborted, error=reason,
        )

    # Reached only via the `break` above: the model stopped calling tools.
    store.append_event(
        run=run, type=RunEventType.run_finished,
        payload={"spend": spend.to_dict()},
    )
    return _finish_run(
        run=run, account_id=account_id, messages=messages,
        spend=spend, status=RunStatus.done, error=None,
    )


def execute_run(
    *, run_id: str, account_id: str, driver: Optional[Driver] = None,
) -> Run:
    run = store.load_run(run_id, account_id)
    if run is None:
        raise KeyError(f"run {run_id} not found for account {account_id}")

    # Only a run that has not finished may be started. This function rebuilds
    # the message history from the goal and ignores `run.transcript`, so
    # re-invoking a finished run would restart it from scratch and duplicate
    # its event log. Resuming a suspended run is `resume_run`, and must not
    # be silently conflated with a fresh start. `running` is allowed because
    # that is what a crashed worker leaves behind, and a retry has to be able
    # to pick it up.
    if run.status not in (RunStatus.pending, RunStatus.running):
        raise ValueError(
            f"run {run.id} is not startable: status is {run.status.value}"
        )

    # `running` alone cannot tell "crashed mid-run" from "claimed for
    # resume": `store.claim_suspended` flips `suspended -> running` too. The
    # transcript is what distinguishes them. A run whose transcript ends in
    # unanswered `tool_use` blocks is mid-gate — restarting it here would
    # rebuild `messages` from the goal and overwrite that transcript,
    # destroying the only thing a resume can continue from and duplicating
    # the event log. Refuse, the same way a non-startable status is refused,
    # so a reaper for stale `running` rows cannot silently do it.
    if _awaits_resume(run.transcript):
        raise ValueError(
            f"run {run.id} is not startable: its transcript ends with "
            f"unanswered tool calls — it needs resume, not restart"
        )

    driver = driver or AnthropicDriver()
    budget = Budget.from_dict(run.budget)
    spend = Spend()
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
        return _drive(
            run=run, account_id=account_id, messages=messages,
            budget=budget, spend=spend, driver=driver,
        )
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


APPROVE = "approve"
REJECT = "reject"


def _was_rejected(run: Run, call: ToolCall) -> bool:
    """Has the user already refused this exact call in this run?

    Matches on name and arguments together: refusing `run_reconciliation`
    on one pair of datasets should not block it on a different pair.
    """
    return any(
        entry.get("tool") == call.name and entry.get("input") == call.input
        for entry in run.rejected_calls
    )


def _pending_calls(messages: List[Dict[str, Any]]) -> List[ToolCall]:
    """The unanswered tool_use blocks a suspended transcript ends with.

    Raises if the transcript is not in that shape. Every downstream step
    depends on the invariant, and a malformed continuation is a 400 from
    the API with a far less obvious message than this one.
    """
    if not messages:
        raise ValueError("cannot resume: transcript is empty")
    last = messages[-1]
    if last.get("role") != "assistant":
        raise ValueError(
            f"cannot resume: transcript ends with a {last.get('role')} turn"
        )
    calls = [
        ToolCall(
            id=b["id"], name=b["name"], input=dict(b.get("input") or {}),
        )
        for b in last.get("content", [])
        if isinstance(b, dict) and b.get("type") == "tool_use"
    ]
    if not calls:
        raise ValueError("cannot resume: no pending tool call to answer")
    return calls


def _awaits_resume(messages: List[Dict[str, Any]]) -> bool:
    """Is this transcript one only a resume can continue?

    Deliberately phrased as "does `_pending_calls` accept it" rather than as
    a second shape check. There is exactly one definition of "ends in an
    unanswered `tool_use` turn"; a paraphrase here could drift from the one
    `resume_run` actually relies on, and the two disagreeing is precisely
    the failure this guard exists to prevent.
    """
    try:
        _pending_calls(messages)
    except ValueError:
        return False
    return True


def resume_run(
    *, run_id: str, account_id: str, decision: str,
    note: Optional[str] = None, driver: Optional[Driver] = None,
) -> Run:
    """Answer a gate and continue the run.

    The caller must already hold the run via `store.claim_suspended`, which
    is what makes a double-answer safe; this refuses anything not in
    `running`. The decision becomes the `tool_result` answering every
    pending `tool_use` — the API has no other legal continuation from a
    suspended transcript.
    """
    if decision not in (APPROVE, REJECT):
        raise ValueError(f"decision must be {APPROVE!r} or {REJECT!r}")

    run = store.load_run(run_id, account_id)
    if run is None:
        raise KeyError(f"run {run_id} not found for account {account_id}")
    if run.status is not RunStatus.running:
        raise ValueError(
            f"run {run.id} is not resumable: status is {run.status.value}"
        )

    driver = driver or AnthropicDriver()
    budget = Budget.from_dict(run.budget)
    spend = Spend.from_dict(run.spend)
    messages: List[Dict[str, Any]] = list(run.transcript)

    token = set_run_context(RunContext(run_id=run.id, account_id=account_id))
    try:
        # `_pending_calls` must run inside this `try`. The caller has
        # already flipped the run to `running` via `claim_suspended` before
        # calling this function, so a raise before this point would leave
        # it stuck there: `claim_suspended` only ever claims a `suspended`
        # run, so it can never pick this run up again, and `execute_run`
        # refuses anything but `pending`/`running` — and for `running` it
        # restarts from the goal, duplicating the event log. Failing loudly
        # here still has to leave the run `failed` and observable, the way
        # every other failure in this loop does.
        pending = _pending_calls(messages)
        store.append_event(
            run=run, type=RunEventType.question_answered,
            payload={
                "decision": decision,
                "note": note,
                "tools": [c.name for c in pending],
            },
        )

        if decision == APPROVE:
            executed = _execute_calls(
                run=run, account_id=account_id, calls=pending,
                budget=budget, spend=spend,
            )
            if executed.abort is not None:
                status, error = executed.abort
                return _finish_run(
                    run=run, account_id=account_id, messages=messages,
                    spend=spend, status=status, error=error,
                )
            results = executed.results
        else:
            denial = note or "The user declined this action."
            results = [
                {
                    "type": "tool_result", "tool_use_id": call.id,
                    "content": f"declined: {denial}", "is_error": True,
                }
                for call in pending
            ]
            rejected = list(run.rejected_calls) + [
                {"tool": call.name, "input": call.input} for call in pending
            ]
            run.rejected_calls = rejected
            store.record_rejected_calls(
                run_id=run.id, account_id=account_id, calls=rejected,
            )
            store.append_event(
                run=run, type=RunEventType.proposal_rejected,
                payload={"tools": [c.name for c in pending]},
            )

        # One user message for the whole turn: splitting tool_results across
        # messages trains the model out of parallel tool calls.
        messages.append({"role": "user", "content": results})

        if note:
            # Mid-conversation system message (spec 4.6). Legal only here:
            # it follows a user turn and is last. Opus 4.8 only — an
            # unsupported model (including an `ANTHROPIC_MODEL` env
            # override, which `AnthropicDriver` honors) returns 400.
            messages.append({"role": "system", "content": note})

        return _drive(
            run=run, account_id=account_id, messages=messages,
            budget=budget, spend=spend, driver=driver,
        )
    except Exception as exc:
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
