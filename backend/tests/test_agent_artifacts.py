"""Datasets must survive a process boundary — a suspended run resumes elsewhere."""
from __future__ import annotations

import uuid

import boto3
import pandas as pd
import pytest
from moto import mock_aws

from app.agent_runtime import artifacts, store
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
def account_id() -> str:
    acct = str(uuid.uuid4())
    with session_scope() as s:
        s.add(AccountORM(id=acct, payload={}))
    return acct


@pytest.fixture()
def run(account_id):
    return store.create_run(
        account_id=account_id, goal={}, autonomy=AutonomyLevel.assist, budget={},
    )


def _frame() -> pd.DataFrame:
    return pd.DataFrame({
        "order_id": ["A-1", "A-2", "A-3"],
        "gross_total": [10.50, 20.25, 30.00],
        "placed_at": pd.to_datetime(["2026-06-01", "2026-06-02", "2026-06-03"]),
    })


@mock_aws
def test_dataset_round_trips_with_dtypes_intact(run, account_id):
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="reconops-test-bucket")
    dataset_id = artifacts.put_dataset(
        run_id=run.id, account_id=account_id, df=_frame(), label="orders",
    )
    loaded = artifacts.get_dataset(dataset_id=dataset_id, account_id=account_id)
    pd.testing.assert_frame_equal(loaded, _frame())


@mock_aws
def test_get_dataset_is_account_scoped(run, account_id):
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="reconops-test-bucket")
    dataset_id = artifacts.put_dataset(
        run_id=run.id, account_id=account_id, df=_frame(), label="orders",
    )
    other = str(uuid.uuid4())
    with session_scope() as s:
        s.add(AccountORM(id=other, payload={}))

    with pytest.raises(KeyError):
        artifacts.get_dataset(dataset_id=dataset_id, account_id=other)


@mock_aws
def test_describe_is_bounded_and_carries_no_row_data(run, account_id):
    """Tool returns cross the model boundary — they carry no rows (spec 2.1)."""
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="reconops-test-bucket")
    dataset_id = artifacts.put_dataset(
        run_id=run.id, account_id=account_id, df=_frame(), label="orders",
    )
    desc = artifacts.describe(dataset_id=dataset_id, account_id=account_id)

    assert desc["row_count"] == 3
    assert desc["columns"] == ["order_id", "gross_total", "placed_at"]
    assert desc["label"] == "orders"
    serialized = str(desc)
    assert "A-1" not in serialized
    assert "20.25" not in serialized


@mock_aws
def test_storage_key_sits_under_the_account_purge_prefix(run, account_id):
    """delete_prefix purges accounts/{id}/ — artifacts must be inside it."""
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="reconops-test-bucket")
    dataset_id = artifacts.put_dataset(
        run_id=run.id, account_id=account_id, df=_frame(), label="orders",
    )
    key = artifacts.storage_key(dataset_id=dataset_id, account_id=account_id)
    assert key.startswith("accounts/" + account_id + "/")
