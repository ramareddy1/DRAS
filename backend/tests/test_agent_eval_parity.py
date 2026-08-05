"""Phase A gate: the runtime path must not change the numbers.

If these diverge, the macro-tool wrapper regressed the pipeline and the
migration is no longer zero-regression (spec 1.3).
"""
from __future__ import annotations

import boto3
import pandas as pd
import pytest
from moto import mock_aws

from app.agent_runtime import artifacts, context, runtime, store, tools_macro  # noqa: F401
from app.agent_runtime.runtime import ToolCall, Turn
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
        if not self._turns:
            return Turn(text="done", tool_calls=[])
        return self._turns.pop(0)


def _orders():
    return pd.DataFrame({
        "order_id": ["A-1", "A-2", "A-3", "A-4"],
        "order_total": [10.0, 20.0, 30.0, 40.0],
        "order_date": ["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04"],
    })


def _payouts():
    return pd.DataFrame({
        "order_ref": ["A-1", "A-2", "A-3"],
        "amount_paid": [10.0, 20.0, 29.5],
        "paid_on": ["2026-06-03", "2026-06-04", "2026-06-06"],
    })


@pytest.fixture()
def account(monkeypatch):
    monkeypatch.setenv("RECONOPS_STUB_LLM", "1")
    return accounts_memory.create_account(display_name="Parity Test")


@mock_aws
def test_runtime_path_matches_direct_pipeline(account):
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
    from app.eval import parity_report

    report = parity_report(account.id, _orders(), _payouts())
    assert report["matches"], report


@mock_aws
def test_auto_still_suspends_before_the_macro_tool_writes(account):
    """Why parity is measured at the tool boundary, not through the loop.

    `run_reconciliation` is `Effect.write`, and a write gates at every
    autonomy level — `auto` included. A loop-driven run therefore suspends
    for approval and never executes the tool, so there is no output event to
    compare against. If this ever stops suspending, the autonomy dial has
    been weakened and `parity_report` should go back through `execute_run`.
    """
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
    run = store.create_run(
        account_id=account.id, goal={"intent": "reconcile"},
        autonomy=AutonomyLevel.auto, budget={"tool_call_cap": 5},
    )
    token = context.set_run_context(
        context.RunContext(run_id=run.id, account_id=account.id),
    )
    try:
        a = artifacts.put_dataset(
            run_id=run.id, account_id=account.id, df=_orders(), label="orders",
        )
        b = artifacts.put_dataset(
            run_id=run.id, account_id=account.id, df=_payouts(), label="payouts",
        )
    finally:
        context.reset_run_context(token)

    driver = ScriptedDriver([
        Turn(text=None, tool_calls=[ToolCall(
            id="t1", name="run_reconciliation",
            input={
                "dataset_a_id": a, "dataset_b_id": b,
                "label_a": "Orders", "label_b": "Payouts",
            },
        )]),
        Turn(text="reconciled", tool_calls=[]),
    ])

    finished = runtime.execute_run(
        run_id=run.id, account_id=account.id, driver=driver,
    )

    assert finished.status is RunStatus.suspended
    assert finished.suspended_on is not None

    events = store.events_since(run_id=run.id, account_id=account.id)
    asked = [e for e in events if e.type is RunEventType.question_asked]
    assert [e.payload["tool"] for e in asked] == ["run_reconciliation"]
    assert not [e for e in events if "output" in e.payload], (
        "the tool must not have executed"
    )


def test_no_verdict_originates_from_the_model(account):
    """House law: the LLM never computes money."""
    run = store.create_run(
        account_id=account.id, goal={}, autonomy=AutonomyLevel.auto, budget={},
    )
    driver = ScriptedDriver([
        Turn(text="I calculate the total is $999,999.", tool_calls=[]),
    ])

    runtime.execute_run(run_id=run.id, account_id=account.id, driver=driver)

    events = store.events_since(run_id=run.id, account_id=account.id)
    tool_outputs = [
        e.payload["output"] for e in events if "output" in e.payload
    ]
    assert tool_outputs == [], "no tool ran, so no figure is authoritative"
