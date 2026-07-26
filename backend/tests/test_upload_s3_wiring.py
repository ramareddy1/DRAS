import importlib

import boto3
import pandas as pd
from moto import mock_aws


@mock_aws
def test_upload_persists_files_to_s3_and_records_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("RECONOPS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RECONOPS_AUTH_DEV", "1")
    monkeypatch.setenv("RECONOPS_S3_BUCKET", "reconops-test-bucket")
    monkeypatch.setenv("RECONOPS_S3_REGION", "us-east-1")
    monkeypatch.delenv("RECONOPS_S3_ENDPOINT_URL", raising=False)
    monkeypatch.setenv("RECONOPS_S3_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("RECONOPS_S3_SECRET_ACCESS_KEY", "testing")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("RECONOPS_STUB_LLM", raising=False)

    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="reconops-test-bucket")

    from app.memory import accounts as accounts_memory, rules_store
    importlib.reload(accounts_memory); importlib.reload(rules_store)
    from app import storage
    importlib.reload(storage)
    from app import main
    importlib.reload(main)

    from fastapi.testclient import TestClient
    client = TestClient(main.app)

    signup = client.post("/api/auth/request-code", json={"email": "s3test@example.com"})
    code = signup.json()["dev_code"]
    # NOTE: the plan brief names this endpoint "/api/auth/verify-code", but the
    # actual route (backend/app/auth/routes.py) is "/api/auth/verify" — every
    # other test in this repo (test_async_jobs.py, test_export_tokens.py, ...)
    # agrees. Using the real route name here; a 404 would otherwise leave the
    # session cookie unset and every call below would 401.
    client.post("/api/auth/verify", json={"email": "s3test@example.com", "code": code})
    account_id = client.post("/api/accounts", json={}).json()["id"]

    # NOTE: the plan brief's `config` JSON uses `recon_type: "orders"` (not a
    # valid ReconType literal) and `bindings` as a flat dict (the schema is
    # actually `List[SemanticBinding]`). Building the config via bind_columns()
    # — the same helper production code and every other upload test use —
    # keeps this test aligned with the real app/models.py schema.
    from app.models import BindingSet, ReconcileConfig
    from app.tools.binding import bind_columns

    df_a = pd.DataFrame({"order_id": ["#1"], "order_total": [10.0]})
    df_b = pd.DataFrame({"order_reference": ["#1"], "amount": [10.0]})
    cfg = ReconcileConfig(
        source_a=BindingSet(bindings=bind_columns(df_a)),
        source_b=BindingSet(bindings=bind_columns(df_b)),
    )
    csv_a = df_a.to_csv(index=False)
    csv_b = df_b.to_csv(index=False)

    resp = client.post(
        "/api/upload",
        headers={"X-Account-Id": account_id},
        files={"file_a": ("a.csv", csv_a.encode(), "text/csv"),
               "file_b": ("b.csv", csv_b.encode(), "text/csv")},
        data={"config": cfg.model_dump_json()},
    )
    assert resp.status_code == 200
    job_id = resp.json()["job_id"]

    job = storage.load_job(job_id)
    uploaded = job["uploaded_files"]
    assert uploaded["a"]["bucket"] == "reconops-test-bucket"
    assert uploaded["b"]["bucket"] == "reconops-test-bucket"

    s3 = boto3.client("s3", region_name="us-east-1")
    body_a = s3.get_object(Bucket="reconops-test-bucket", Key=uploaded["a"]["key"])["Body"].read()
    assert body_a.decode() == csv_a
