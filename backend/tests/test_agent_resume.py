"""Answering a gate and continuing the run.

A suspended transcript ends with an assistant turn whose `tool_use` blocks
have no `tool_result`. The API rejects any continuation in that shape, so
the user's decision has to *become* the tool_result — approve produces the
tool's real output, reject produces an `is_error` result the model can
re-plan around (spec: "The API constraint that shapes everything").
"""
from __future__ import annotations

import uuid

import boto3
import pandas as pd
import pytest
from moto import mock_aws

from app.agent_runtime import artifacts, context, runtime, store
from app.agent_runtime.runtime import ToolCall, Turn
from app.db.base import session_scope
from app.db.models import AccountORM
from app.memory import accounts as accounts_memory
from app.models import AutonomyLevel, RunEventType, RunStatus

BUCKET = "reconops-test-bucket"


@pytest.fixture(autouse=True)
def _s3_env(monkeypatch):
    monkeypatch.setenv("RECONOPS_S3_BUCKET", BUCKET)
    monkeypatch.setenv("RECONOPS_S3_REGION", "us-east-1")
    monkeypatch.delenv("RECONOPS_S3_ENDPOINT_URL", raising=False)
    monkeypatch.setenv("RECONOPS_S3_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("RECONOPS_S3_SECRET_ACCESS_KEY", "testing")


class ScriptedDriver:
    def __init__(self, turns):
        self._turns = list(turns)

    def next_turn(self, *, system, messages, tools, task_budget_tokens):
        self.last_messages = list(messages)
        if not self._turns:
            return Turn(text="done", tool_calls=[])
        return self._turns.pop(0)


@pytest.fixture()
def account_id() -> str:
    acct = str(uuid.uuid4())
    with session_scope() as s:
        s.add(AccountORM(id=acct, payload={}))
    return acct


def _suspend_on_profile(account_id, autonomy=AutonomyLevel.observe):
    """Drive a run to a gate and leave it suspended.

    `observe` gates even a read, which is the cheapest way to reach a
    suspend without needing a write tool's side effects.
    """
    run = store.create_run(
        account_id=account_id, goal={"intent": "look"},
        autonomy=autonomy, budget={"tool_call_cap": 10},
    )
    token = context.set_run_context(
        context.RunContext(run_id=run.id, account_id=account_id),
    )
    try:
        ds = artifacts.put_dataset(
            run_id=run.id, account_id=account_id,
            df=pd.DataFrame({"x": [1, 2, 3]}), label="d",
        )
    finally:
        context.reset_run_context(token)

    driver = ScriptedDriver([
        Turn(text=None, tool_calls=[
            ToolCall(id="t1", name="profile_schema", input={"dataset_id": ds}),
        ]),
        Turn(text="all done", tool_calls=[]),
    ])
    finished = runtime.execute_run(
        run_id=run.id, account_id=account_id, driver=driver,
    )
    assert finished.status is RunStatus.suspended
    return run.id, ds


def _assistant_tool_use_turn(messages):
    """Index and content of the assistant turn a resume answers.

    Scans forward rather than trusting `messages[-1]`: the callers of this
    helper need the turn `resume_run` was actually continuing from, and a
    positional scan is what lets them check the message that follows it —
    which is exactly what `messages[-1]`-style filtering cannot see.
    """
    for i, m in enumerate(messages):
        if m["role"] == "assistant" and any(
            isinstance(b, dict) and b.get("type") == "tool_use"
            for b in m["content"]
        ):
            return i, m
    raise AssertionError("transcript has no assistant tool_use turn")


def _assert_single_user_turn_answers_every_pending_call(messages):
    """Pin the two rules a violation of either is a guaranteed 400 on:

    every `tool_use` block is answered by a `tool_result` with the same id,
    and all results for one turn arrive in a single user message.

    Asserted positionally — on the message immediately following the
    assistant `tool_use` turn — rather than via `[m for m in messages if
    m["role"] == "user"][-1]`, which is blind to results split across two
    user messages (it would just find whichever came last) and blind to a
    dropped id (it never compares against the pending set).
    """
    idx, assistant_turn = _assistant_tool_use_turn(messages)
    pending_ids = {
        b["id"] for b in assistant_turn["content"]
        if isinstance(b, dict) and b.get("type") == "tool_use"
    }

    following = messages[idx + 1]
    assert following["role"] == "user"
    result_ids = {
        b["tool_use_id"] for b in following["content"]
        if isinstance(b, dict) and b.get("type") == "tool_result"
    }
    assert result_ids == pending_ids
    return following["content"]


@mock_aws
def test_approve_executes_the_pending_tool_and_continues(account_id):
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
    run_id, _ = _suspend_on_profile(account_id)

    claimed = store.claim_suspended(run_id, account_id)
    assert claimed is not None

    driver = ScriptedDriver([Turn(text="all done", tool_calls=[])])
    finished = runtime.resume_run(
        run_id=run_id, account_id=account_id, decision="approve",
        driver=driver,
    )

    assert finished.status is RunStatus.done
    events = store.events_since(run_id=run_id, account_id=account_id)
    returned = [
        e for e in events
        if e.type is RunEventType.tool_returned
        and e.payload.get("tool") == "profile_schema"
    ]
    assert len(returned) == 1
    assert returned[0].payload["output"]["row_count"] == 3

    # The model saw the tool's real output, answering every pending call,
    # in one user turn.
    content = _assert_single_user_turn_answers_every_pending_call(
        driver.last_messages,
    )
    assert all(b["type"] == "tool_result" for b in content)
    assert not any(b.get("is_error") for b in content)


@mock_aws
def test_a_single_call_turn_keeps_the_original_question_payload(account_id):
    """The pre-`calls` keys stay byte-for-byte what they were.

    `calls` is additive: anything already reading `text` / `tool` / `input`
    has to keep working, and for the single-call turn — every suspend the
    suite had before this — the payload is unchanged apart from the new key.
    """
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
    run_id, ds = _suspend_on_profile(account_id)

    events = store.events_since(run_id=run_id, account_id=account_id)
    asked = [e for e in events if e.type is RunEventType.question_asked]
    assert len(asked) == 1
    payload = asked[0].payload
    assert payload["text"] == "Run profile_schema?"
    assert payload["tool"] == "profile_schema"
    assert payload["input"] == {"dataset_id": ds}
    assert payload["calls"] == [
        {"tool": "profile_schema", "input": {"dataset_id": ds}},
    ]


@mock_aws
def test_a_two_gated_call_turn_names_both_and_approve_runs_both(account_id):
    """`question_asked` must not understate what one approve authorizes.

    The gate loop returns on the *first* gated call, so `tool`/`input` name
    only that one — but approve is whole-turn (decision 1) and hands every
    pending `tool_use` block to `_execute_calls`. `calls` is the durable
    record that closes the gap: without it a run approved here carries an
    audit trail naming one tool for a decision that ran two.

    `observe` gates every call including reads, so both calls in this turn
    are genuinely gated, and both are registered so both really execute on
    approve.
    """
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)

    run = store.create_run(
        account_id=account_id, goal={"intent": "look"},
        autonomy=AutonomyLevel.observe, budget={"tool_call_cap": 10},
    )
    token = context.set_run_context(
        context.RunContext(run_id=run.id, account_id=account_id),
    )
    try:
        ds = artifacts.put_dataset(
            run_id=run.id, account_id=account_id,
            df=pd.DataFrame({
                "order_id": ["A-1", "A-2", "A-3"],
                "gross_total": [1.0, 2.0, 3.0],
            }),
            label="orders",
        )
    finally:
        context.reset_run_context(token)

    suspended = runtime.execute_run(
        run_id=run.id, account_id=account_id,
        driver=ScriptedDriver([
            Turn(text=None, tool_calls=[
                ToolCall(id="t1", name="profile_schema",
                         input={"dataset_id": ds}),
                ToolCall(id="t2", name="bind_columns",
                         input={"dataset_id": ds}),
            ]),
        ]),
    )
    assert suspended.status is RunStatus.suspended

    events = store.events_since(run_id=run.id, account_id=account_id)
    asked = [e for e in events if e.type is RunEventType.question_asked]
    assert len(asked) == 1
    payload = asked[0].payload

    # Old keys unchanged: still the call that tripped the gate.
    assert payload["tool"] == "profile_schema"
    assert payload["input"] == {"dataset_id": ds}
    # New key: every call one approve will run.
    assert payload["calls"] == [
        {"tool": "profile_schema", "input": {"dataset_id": ds}},
        {"tool": "bind_columns", "input": {"dataset_id": ds}},
    ]
    # And the prose says so too, rather than naming one of the two.
    assert payload["text"] == (
        "Run all 2 tool calls in this turn (profile_schema, bind_columns)? "
        "profile_schema is what requires approval, and approving runs the "
        "whole turn."
    )

    store.claim_suspended(run.id, account_id)
    driver = ScriptedDriver([Turn(text="done", tool_calls=[])])
    finished = runtime.resume_run(
        run_id=run.id, account_id=account_id, decision="approve",
        driver=driver,
    )

    # Whole-turn approval: both calls ran, exactly once each.
    assert finished.status is RunStatus.done
    events = store.events_since(run_id=run.id, account_id=account_id)
    returned = [e for e in events if e.type is RunEventType.tool_returned]
    assert [e.payload["tool"] for e in returned] == [
        "profile_schema", "bind_columns",
    ]
    assert finished.spend["tool_calls"] == 2
    content = _assert_single_user_turn_answers_every_pending_call(
        driver.last_messages,
    )
    assert not any(b.get("is_error") for b in content)


@mock_aws
def test_reject_feeds_an_error_result_back_and_the_model_replans(account_id):
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
    run_id, _ = _suspend_on_profile(account_id)
    store.claim_suspended(run_id, account_id)

    driver = ScriptedDriver([Turn(text="understood", tool_calls=[])])
    finished = runtime.resume_run(
        run_id=run_id, account_id=account_id, decision="reject",
        note="Not on production data.", driver=driver,
    )

    assert finished.status is RunStatus.done

    # The tool must NOT have run.
    events = store.events_since(run_id=run_id, account_id=account_id)
    assert not [e for e in events if e.type is RunEventType.tool_returned]

    # The model saw a denial for every pending call, in one user turn.
    denial = _assert_single_user_turn_answers_every_pending_call(
        driver.last_messages,
    )
    assert all(b["type"] == "tool_result" for b in denial)
    assert all(b["is_error"] for b in denial)


@mock_aws
def test_the_note_lands_in_a_legal_system_position(account_id):
    """A mid-conversation system message must follow a user turn and be last."""
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
    run_id, _ = _suspend_on_profile(account_id)
    store.claim_suspended(run_id, account_id)

    driver = ScriptedDriver([Turn(text="ok", tool_calls=[])])
    runtime.resume_run(
        run_id=run_id, account_id=account_id, decision="approve",
        note="Only June, please.", driver=driver,
    )

    seen = driver.last_messages
    assert seen[-1] == {"role": "system", "content": "Only June, please."}
    assert seen[-2]["role"] == "user"
    assert seen[0]["role"] != "system"


@mock_aws
def test_spend_accumulates_across_the_suspend_boundary(monkeypatch):
    """The caps are per-run, not per-segment.

    A tool that ran *before* the suspend has to still count against the cap
    *after* resume. Executing only one call, entirely post-resume, cannot
    tell `Spend.from_dict(run.spend)` apart from a fresh `Spend()` — both
    would show `tool_calls == 1`. Running one non-gated call pre-suspend and
    one gated call post-resume makes the two diverge: `2` only if the spend
    is rehydrated, `1` if it silently resets.
    """
    monkeypatch.setenv("RECONOPS_STUB_LLM", "1")
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)

    acct = accounts_memory.create_account(display_name="resume-spend")
    run = store.create_run(
        account_id=acct.id, goal={"intent": "reconcile"},
        autonomy=AutonomyLevel.assist, budget={"tool_call_cap": 10},
    )
    token = context.set_run_context(
        context.RunContext(run_id=run.id, account_id=acct.id),
    )
    try:
        ds = artifacts.put_dataset(
            run_id=run.id, account_id=acct.id,
            df=pd.DataFrame({"x": [1, 2, 3]}), label="d",
        )
        a = artifacts.put_dataset(
            run_id=run.id, account_id=acct.id,
            df=pd.DataFrame({
                "order_id": ["A-1", "A-2"],
                "order_total": [10.0, 20.0],
                "order_date": ["2026-06-01", "2026-06-02"],
            }), label="orders",
        )
        b = artifacts.put_dataset(
            run_id=run.id, account_id=acct.id,
            df=pd.DataFrame({
                "order_ref": ["A-1", "A-2"],
                "amount_paid": [10.0, 20.0],
                "paid_on": ["2026-06-03", "2026-06-04"],
            }), label="payouts",
        )
    finally:
        context.reset_run_context(token)

    driver = ScriptedDriver([
        # A read: `assist` autonomy doesn't gate it, so it executes in this
        # (pre-suspend) segment. spend.tool_calls -> 1.
        Turn(text=None, tool_calls=[
            ToolCall(id="t1", name="profile_schema", input={"dataset_id": ds}),
        ]),
        # A write: always gates regardless of autonomy — suspends before it
        # runs, leaving it pending for the resume.
        Turn(text=None, tool_calls=[
            ToolCall(id="t2", name="run_reconciliation", input={
                "dataset_a_id": a, "dataset_b_id": b,
                "label_a": "Orders", "label_b": "Payouts",
            }),
        ]),
    ])
    suspended = runtime.execute_run(
        run_id=run.id, account_id=acct.id, driver=driver,
    )
    assert suspended.status is RunStatus.suspended
    assert suspended.spend["tool_calls"] == 1

    store.claim_suspended(run.id, acct.id)

    finished = runtime.resume_run(
        run_id=run.id, account_id=acct.id, decision="approve",
        driver=ScriptedDriver([Turn(text="done", tool_calls=[])]),
    )
    assert finished.status is RunStatus.done
    assert finished.spend["tool_calls"] == 2


@mock_aws
def test_resume_refuses_a_run_that_was_not_claimed(account_id):
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
    run_id, _ = _suspend_on_profile(account_id)

    # No claim_suspended() — the run is still `suspended`.
    with pytest.raises(ValueError):
        runtime.resume_run(
            run_id=run_id, account_id=account_id, decision="approve",
            driver=ScriptedDriver([]),
        )


def test_resume_refuses_a_finished_run(account_id):
    run = store.create_run(
        account_id=account_id, goal={}, autonomy=AutonomyLevel.auto, budget={},
    )
    store.set_status(
        run_id=run.id, account_id=account_id, status=RunStatus.done,
    )
    with pytest.raises(ValueError):
        runtime.resume_run(
            run_id=run.id, account_id=account_id, decision="approve",
            driver=ScriptedDriver([]),
        )


@mock_aws
def test_resume_refuses_an_invalid_decision(account_id):
    """Pin the decision guard, not the status check standing in for it.

    A `pending` run would raise `ValueError` from the status check for *any*
    decision, valid or not — so it cannot tell whether the decision guard
    exists. The fixture is therefore a genuinely suspended run, claimed so
    its status is `running` and the status check passes; the only thing left
    to reject "maybe" is the guard itself. `match=` pins the guard's own
    message so the status check's wording cannot satisfy it.
    """
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
    run_id, _ = _suspend_on_profile(account_id)

    claimed = store.claim_suspended(run_id, account_id)
    assert claimed is not None
    assert claimed.status is RunStatus.running

    with pytest.raises(
        ValueError, match=r"decision must be 'approve' or 'reject'",
    ):
        runtime.resume_run(
            run_id=run_id, account_id=account_id, decision="maybe",
            driver=ScriptedDriver([]),
        )


def test_a_malformed_transcript_fails_loudly_without_wedging_the_run(account_id):
    """`_pending_calls` must raise inside the `try`, not before it.

    The caller has already flipped this run to `running` via
    `claim_suspended` by the time `resume_run` is called — exactly what a
    real caller does. `claim_suspended` only ever claims a `suspended` run,
    so a run left `running` here could never be claimed again, and
    `execute_run` refuses `running` runs except by restarting them from the
    goal, which would duplicate the event log. A raise here still has to
    leave the run `failed` and carrying an `error`, the way every other
    failure in this loop does — not stuck `running` and unobservable.
    """
    run = store.create_run(
        account_id=account_id, goal={"intent": "test"},
        autonomy=AutonomyLevel.assist, budget={},
    )
    # A transcript that does not end in an assistant tool_use turn.
    # `claim_suspended` itself can never produce this shape — it only
    # flips status — but `_pending_calls` has to fail loudly and
    # recoverably on it regardless of how it got there.
    store.save_transcript(
        run_id=run.id, account_id=account_id,
        transcript=[{"role": "user", "content": "hello"}], spend={},
    )
    store.set_status(
        run_id=run.id, account_id=account_id, status=RunStatus.suspended,
    )

    claimed = store.claim_suspended(run.id, account_id)
    assert claimed is not None
    assert claimed.status is RunStatus.running

    with pytest.raises(ValueError):
        runtime.resume_run(
            run_id=run.id, account_id=account_id, decision="approve",
            driver=ScriptedDriver([]),
        )

    reloaded = store.load_run(run.id, account_id)
    assert reloaded.status is RunStatus.failed
    assert reloaded.error is not None
    events = store.events_since(run_id=run.id, account_id=account_id)
    assert not any(e.type is RunEventType.question_answered for e in events)


@mock_aws
def test_re_proposing_a_rejected_call_aborts_rather_than_gating_again(
    account_id,
):
    """Reject has to mean something.

    Without this the model re-proposes, the gate fires, the user rejects
    again — and the run spends its whole budget on a loop the user already
    refused. `MAX_ITERATIONS` would eventually stop it, but only after
    paying for every turn.
    """
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
    run_id, ds = _suspend_on_profile(account_id)
    store.claim_suspended(run_id, account_id)

    # The model immediately asks for the very call that was just refused.
    driver = ScriptedDriver([
        Turn(text=None, tool_calls=[
            ToolCall(id="t2", name="profile_schema", input={"dataset_id": ds}),
        ]),
    ])
    finished = runtime.resume_run(
        run_id=run_id, account_id=account_id, decision="reject",
        driver=driver,
    )

    assert finished.status is RunStatus.aborted
    assert "rejected" in (finished.error or "")

    events = store.events_since(run_id=run_id, account_id=account_id)
    # It aborted instead of asking a second time.
    assert len([
        e for e in events if e.type is RunEventType.question_asked
    ]) == 1


@mock_aws
def test_a_different_call_still_reaches_the_gate_after_a_rejection(account_id):
    """The guard is per-call, not a blanket freeze on the run.

    The run stays in `observe`, where every call gates — so a *different*
    call suspending again is the proof that the guard matched on arguments
    rather than freezing the run outright.
    """
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
    run_id, _ = _suspend_on_profile(account_id)
    store.claim_suspended(run_id, account_id)

    token = context.set_run_context(
        context.RunContext(run_id=run_id, account_id=account_id),
    )
    try:
        other = artifacts.put_dataset(
            run_id=run_id, account_id=account_id,
            df=pd.DataFrame({"y": [1, 2]}), label="other",
        )
    finally:
        context.reset_run_context(token)

    driver = ScriptedDriver([
        Turn(text=None, tool_calls=[
            ToolCall(
                id="t2", name="profile_schema",
                input={"dataset_id": other},
            ),
        ]),
    ])
    finished = runtime.resume_run(
        run_id=run_id, account_id=account_id, decision="reject",
        driver=driver,
    )

    # Same tool, different arguments → not the refused call, so it gates
    # normally instead of aborting on the guard.
    assert finished.status is RunStatus.suspended
