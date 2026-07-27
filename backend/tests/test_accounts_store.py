import pytest

from app import models
from app.memory import accounts


def test_create_and_load_account_round_trips():
    acc = accounts.create_account(display_name="Acme Co")
    loaded = accounts.load_account(acc.id)
    assert loaded is not None
    assert loaded.id == acc.id
    assert loaded.display_name == "Acme Co"
    assert isinstance(loaded.profile, models.AccountProfile)


def test_load_account_missing_returns_none():
    assert accounts.load_account("00000000-0000-4000-8000-000000000000") is None


def test_load_account_rejects_malformed_id():
    assert accounts.load_account("not-a-uuid") is None


def test_update_profile_merges_partial_and_ignores_none():
    acc = accounts.create_account()
    updated = accounts.update_profile(acc.id, {"time_zone": "America/New_York", "materiality_abs": None})
    assert updated.profile.time_zone == "America/New_York"
    assert updated.profile.materiality_abs == 100.0  # unchanged (None ignored)


def test_update_profile_missing_account_raises():
    with pytest.raises(ValueError):
        accounts.update_profile("00000000-0000-4000-8000-000000000000", {"time_zone": "UTC"})


def test_account_exists():
    acc = accounts.create_account()
    assert accounts.account_exists(acc.id) is True
    assert accounts.account_exists("00000000-0000-4000-8000-000000000000") is False


def test_account_profile_retention_days_default_and_bounds():
    from pydantic import ValidationError

    assert models.AccountProfile().retention_days == 7
    with pytest.raises(ValidationError):
        models.AccountProfile(retention_days=0)
    with pytest.raises(ValidationError):
        models.AccountProfile(retention_days=366)


def test_delete_account_removes_postgres_row_and_local_json_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("RECONOPS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RECONOPS_S3_BUCKET", "reconops-test-bucket")
    monkeypatch.setenv("RECONOPS_S3_REGION", "us-east-1")
    monkeypatch.delenv("RECONOPS_S3_ENDPOINT_URL", raising=False)
    monkeypatch.setenv("RECONOPS_S3_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("RECONOPS_S3_SECRET_ACCESS_KEY", "testing")
    import boto3
    from moto import mock_aws

    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="reconops-test-bucket")

        from app.auth import members as members_store
        acc = accounts.create_account()
        members_store.add_member(acc.id, "u1", "u1@x.co", "owner")
        members_path = tmp_path / "accounts" / acc.id / "members.json"
        assert members_path.exists()

        accounts.delete_account(acc.id)

        assert accounts.load_account(acc.id) is None
        assert not (tmp_path / "accounts" / acc.id).exists()


def test_delete_account_purges_s3_uploads(tmp_path, monkeypatch):
    monkeypatch.setenv("RECONOPS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RECONOPS_S3_BUCKET", "reconops-test-bucket")
    monkeypatch.setenv("RECONOPS_S3_REGION", "us-east-1")
    monkeypatch.delenv("RECONOPS_S3_ENDPOINT_URL", raising=False)
    monkeypatch.setenv("RECONOPS_S3_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("RECONOPS_S3_SECRET_ACCESS_KEY", "testing")
    import boto3
    from moto import mock_aws

    with mock_aws():
        from app import storage_s3

        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="reconops-test-bucket")
        acc = accounts.create_account()
        key = storage_s3.upload_key_for(acc.id, "job-1", "a", "orders.csv")
        storage_s3.put_object(key, b"data")

        accounts.delete_account(acc.id)

        client_s3 = boto3.client("s3", region_name="us-east-1")
        listing = client_s3.list_objects_v2(Bucket="reconops-test-bucket", Prefix=f"accounts/{acc.id}/")
        assert listing.get("KeyCount", 0) == 0


def test_remove_account_scrubs_global_membership_index(tmp_path, monkeypatch):
    monkeypatch.setenv("RECONOPS_DATA_DIR", str(tmp_path))
    from app.auth import members as members_store

    acc = accounts.create_account()
    other = accounts.create_account()
    members_store.add_member(acc.id, "u1", "u1@x.co", "owner")
    members_store.add_member(other.id, "u1", "u1@x.co", "owner")

    members_store.remove_account(acc.id)

    assert members_store.accounts_for_user("u1") == [{"account_id": other.id, "role": "owner"}]
