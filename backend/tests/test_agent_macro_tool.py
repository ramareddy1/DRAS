from __future__ import annotations

import uuid

import boto3
import pandas as pd
import pytest
from moto import mock_aws

from app.agent_runtime import artifacts, context, store, tools_macro
from app.agent_runtime.registry import Effect, effect_of
from app.db.base import session_scope
from app.db.models import AccountORM
from app.memory import accounts as accounts_memory
from app.models import AutonomyLevel


@pytest.fixture(autouse=True)
def _s3_env(monkeypatch):
    monkeypatch.setenv("RECONOPS_S3_BUCKET", "reconops-test-bucket")
    monkeypatch.setenv("RECONOPS_S3_REGION", "us-east-1")
    monkeypatch.delenv("RECONOPS_S3_ENDPOINT_URL", raising=False)
    monkeypatch.setenv("RECONOPS_S3_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("RECONOPS_S3_SECRET_ACCESS_KEY", "testing")


@pytest.fixture()
def run_ctx(monkeypatch):
    monkeypatch.setenv("RECONOPS_STUB_LLM", "1")
    acct = accounts_memory.create_account(display_name="Macro Test")
    run = store.create_run(
        account_id=acct.id, goal={}, autonomy=AutonomyLevel.auto, budget={},
    )
    ctx = context.RunContext(run_id=run.id, account_id=acct.id)
    token = context.set_run_context(ctx)
    yield ctx
    context.reset_run_context(token)


def _orders():
    return pd.DataFrame({
        "order_id": ["A-1", "A-2", "A-3"],
        "order_total": [10.0, 20.0, 30.0],
        "order_date": ["2026-06-01", "2026-06-02", "2026-06-03"],
    })


def _payouts():
    return pd.DataFrame({
        "order_ref": ["A-1", "A-2"],
        "amount_paid": [10.0, 20.0],
        "paid_on": ["2026-06-03", "2026-06-04"],
    })


def test_macro_tool_is_registered_as_a_write(run_ctx):
    assert effect_of("run_reconciliation") is Effect.write


@mock_aws
def test_macro_tool_returns_counts_not_rows(run_ctx):
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="reconops-test-bucket")
    a = artifacts.put_dataset(
        run_id=run_ctx.run_id, account_id=run_ctx.account_id,
        df=_orders(), label="orders",
    )
    b = artifacts.put_dataset(
        run_id=run_ctx.run_id, account_id=run_ctx.account_id,
        df=_payouts(), label="payouts",
    )

    out = tools_macro.run_reconciliation(a, b, "Orders", "Payouts")

    assert out["matched"] == 2
    assert out["unmatched_a"] == 1
    assert out["unmatched_b"] == 0
    assert isinstance(out["job_id"], str)

    serialized = str(out)
    assert "A-1" not in serialized
    assert "order_total" not in serialized


@mock_aws
def test_macro_tool_persists_a_readable_job(run_ctx):
    from app import storage

    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="reconops-test-bucket")
    a = artifacts.put_dataset(
        run_id=run_ctx.run_id, account_id=run_ctx.account_id,
        df=_orders(), label="orders",
    )
    b = artifacts.put_dataset(
        run_id=run_ctx.run_id, account_id=run_ctx.account_id,
        df=_payouts(), label="payouts",
    )

    out = tools_macro.run_reconciliation(a, b, "Orders", "Payouts")
    job = storage.load_job(out["job_id"])

    assert job is not None
    assert job["account_id"] == run_ctx.account_id
