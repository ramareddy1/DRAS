import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws


@pytest.fixture(autouse=True)
def _s3_env(monkeypatch):
    monkeypatch.setenv("RECONOPS_S3_BUCKET", "reconops-test-bucket")
    monkeypatch.setenv("RECONOPS_S3_REGION", "us-east-1")
    monkeypatch.delenv("RECONOPS_S3_ENDPOINT_URL", raising=False)
    monkeypatch.setenv("RECONOPS_S3_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("RECONOPS_S3_SECRET_ACCESS_KEY", "testing")


@mock_aws
def test_upload_key_for_is_deterministic_and_path_safe():
    from app import storage_s3

    key = storage_s3.upload_key_for("acc-1", "job-1", "a", "../../etc/passwd")
    assert key == "accounts/acc-1/jobs/job-1/a_.._.._etc_passwd"


@mock_aws
def test_put_object_round_trips_with_server_side_encryption():
    from app import storage_s3

    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="reconops-test-bucket")
    key = storage_s3.upload_key_for("acc-1", "job-1", "a", "orders.csv")

    storage_s3.put_object(key, b"col_a,col_b\n1,2\n")

    client = boto3.client("s3", region_name="us-east-1")
    obj = client.get_object(Bucket="reconops-test-bucket", Key=key)
    assert obj["Body"].read() == b"col_a,col_b\n1,2\n"
    assert obj["ServerSideEncryption"] == "AES256"


@mock_aws
def test_delete_object_removes_it():
    from app import storage_s3

    client = boto3.client("s3", region_name="us-east-1")
    client.create_bucket(Bucket="reconops-test-bucket")
    key = storage_s3.upload_key_for("acc-1", "job-1", "a", "orders.csv")
    storage_s3.put_object(key, b"data")

    storage_s3.delete_object(key)

    with pytest.raises(ClientError):
        client.get_object(Bucket="reconops-test-bucket", Key=key)
