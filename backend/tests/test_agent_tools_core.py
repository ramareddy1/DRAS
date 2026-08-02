from __future__ import annotations

import uuid

import boto3
import pandas as pd
import pytest
from moto import mock_aws

from app.agent_runtime import artifacts, context, store, tools_core
from app.db.base import session_scope
from app.db.models import AccountORM
from app.models import AutonomyLevel


@pytest.fixture(autouse=True)
def _s3_env(monkeypatch):
    monkeypatch.setenv("RECONOPS_S3_BUCKET", "reconops-test-bucket")
    monkeypatch.setenv("RECONOPS_S3_REGION", "us-east-1")
    monkeypatch.delenv("RECONOPS_S3_ENDPOINT_URL", raising=False)
    monkeypatch.setenv("RECONOPS_S3_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("RECONOPS_S3_SECRET_ACCESS_KEY", "testing")


@pytest.fixture()
def run_ctx():
    acct = str(uuid.uuid4())
    with session_scope() as s:
        s.add(AccountORM(id=acct, payload={}))
    run = store.create_run(
        account_id=acct, goal={}, autonomy=AutonomyLevel.assist, budget={},
    )
    ctx = context.RunContext(run_id=run.id, account_id=acct)
    token = context.set_run_context(ctx)
    yield ctx
    context.reset_run_context(token)


def _orders() -> pd.DataFrame:
    return pd.DataFrame({
        "order_id": ["A-1", "A-2", "A-3", "A-4"],
        "gross_total": [10.0, 20.0, 30.0, 40.0],
    })


def _payments() -> pd.DataFrame:
    return pd.DataFrame({
        "reference": ["A-1", "A-2", "A-9"],
        "amount": [10.0, 20.0, 90.0],
    })


def test_tools_take_no_account_id_parameter():
    """Scope comes from context, never from model-supplied arguments."""
    for fn in (tools_core.profile_schema, tools_core.bind_columns,
               tools_core.match_datasets):
        schema = fn.to_dict()
        assert "account_id" not in schema["input_schema"]["properties"]


@mock_aws
def test_profile_schema_returns_bounded_per_column_stats(run_ctx):
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="reconops-test-bucket")
    ds = artifacts.put_dataset(
        run_id=run_ctx.run_id, account_id=run_ctx.account_id,
        df=_orders(), label="orders",
    )
    out = tools_core.profile_schema(ds)

    assert out["row_count"] == 4
    assert len(out["columns"]) == 2
    col = next(c for c in out["columns"] if c["name"] == "order_id")
    assert col["null_rate"] == 0.0
    assert col["cardinality"] == 4
    assert len(col["samples"]) <= 3


@mock_aws
def test_profile_schema_caps_samples_regardless_of_row_count(run_ctx):
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="reconops-test-bucket")
    big = pd.DataFrame({"x": [f"v{i}" for i in range(5000)]})
    ds = artifacts.put_dataset(
        run_id=run_ctx.run_id, account_id=run_ctx.account_id,
        df=big, label="big",
    )
    out = tools_core.profile_schema(ds)
    assert out["row_count"] == 5000
    assert len(out["columns"][0]["samples"]) == 3


@mock_aws
def test_bind_columns_returns_mappings_not_rows(run_ctx):
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="reconops-test-bucket")
    ds = artifacts.put_dataset(
        run_id=run_ctx.run_id, account_id=run_ctx.account_id,
        df=_orders(), label="orders",
    )
    out = tools_core.bind_columns(ds)

    assert out["dataset_id"] == ds
    assert out["total_count"] == 2
    assert isinstance(out["mappings"], list)
    for m in out["mappings"]:
        assert set(m) == {"column", "concept", "confidence"}


@mock_aws
def test_match_datasets_conserves_rows(run_ctx):
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="reconops-test-bucket")
    a = artifacts.put_dataset(
        run_id=run_ctx.run_id, account_id=run_ctx.account_id,
        df=_orders(), label="orders",
    )
    b = artifacts.put_dataset(
        run_id=run_ctx.run_id, account_id=run_ctx.account_id,
        df=_payments(), label="payments",
    )
    out = tools_core.match_datasets(a, b, "order_id", "reference")

    assert out["rows_in_a"] == 4
    assert out["rows_in_b"] == 3
    assert out["matched"] + out["unmatched_a"] == out["rows_in_a"]
    assert out["matched"] + out["unmatched_b"] == out["rows_in_b"]


def test_current_run_raises_outside_a_run():
    context.reset_run_context(context.set_run_context(None))
    with pytest.raises(RuntimeError):
        context.current_run()
