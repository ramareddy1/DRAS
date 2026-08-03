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


@mock_aws
def test_approve_executes_the_pending_tool_and_continues(account_id):
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
    run_id, _ = _suspend_on_profile(account_id)

    claimed = store.claim_suspended(run_id, account_id)
    assert claimed is not None

    finished = runtime.resume_run(
        run_id=run_id, account_id=account_id, decision="approve",
        driver=ScriptedDriver([Turn(text="all done", tool_calls=[])]),
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
    user_turns = [m for m in driver.last_messages if m["role"] == "user"]
    denial = user_turns[-1]["content"]
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
def test_spend_accumulates_across_the_suspend_boundary(account_id):
    """The caps are per-run, not per-segment."""
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
    run_id, _ = _suspend_on_profile(account_id)
    store.claim_suspended(run_id, account_id)

    finished = runtime.resume_run(
        run_id=run_id, account_id=account_id, decision="approve",
        driver=ScriptedDriver([Turn(text="done", tool_calls=[])]),
    )
    assert finished.spend["tool_calls"] == 1


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
