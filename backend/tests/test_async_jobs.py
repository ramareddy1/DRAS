import importlib
import time

import pandas as pd
import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RECONOPS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RECONOPS_AUTH_DEV", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("RECONOPS_STUB_LLM", raising=False)
    from app.memory import accounts as accounts_memory, rules_store
    importlib.reload(accounts_memory); importlib.reload(rules_store)
    from fastapi.testclient import TestClient
    from app import main
    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c


def _login(client, email="me@x.co"):
    code = client.post("/api/auth/request-code", json={"email": email}).json()["dev_code"]
    assert client.post("/api/auth/verify", json={"email": email, "code": code}).status_code == 200


def _upload(client, account_id):
    from app.models import BindingSet, ReconcileConfig
    from app.tools.binding import bind_columns

    da = pd.DataFrame({"order_id": ["#1", "#2"], "order_total": [10.0, 20.0]})
    db = pd.DataFrame({"order_reference": ["#1", "#2"], "amount": [10.0, 20.0]})
    cfg = ReconcileConfig(
        source_a=BindingSet(bindings=bind_columns(da)),
        source_b=BindingSet(bindings=bind_columns(db)),
    )
    return client.post(
        "/api/upload",
        headers={"X-Account-Id": account_id},
        data={"config": cfg.model_dump_json()},
        files={
            "file_a": ("a.csv", da.to_csv(index=False).encode(), "text/csv"),
            "file_b": ("b.csv", db.to_csv(index=False).encode(), "text/csv"),
        },
    )


def test_upload_returns_processing_immediately_and_completes_in_background(client):
    _login(client)
    acc = client.post("/api/accounts", json={}).json()

    r = _upload(client, acc["id"])
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "processing"
    job_id = body["job_id"]

    # Starlette awaits BackgroundTasks before the ASGI call returns, so by the
    # time client.post() is back the job has already finished executing.
    from app import storage
    job = storage.load_job(job_id)
    assert job["status"] == "complete"
    assert job["summary"]["matched"] == 2


def test_upload_persists_processing_status_before_response(client, monkeypatch):
    from app import main

    def boom(**kwargs):
        raise ValueError("bad file")
    monkeypatch.setattr(main, "run_job", boom)

    _login(client)
    acc = client.post("/api/accounts", json={}).json()
    r = _upload(client, acc["id"])
    assert r.json()["status"] == "processing"

    from app import storage
    job = storage.load_job(r.json()["job_id"])
    assert job["status"] == "error"
    assert "bad file" in job["error"]


def test_job_progress_reaches_final_value(client):
    _login(client)
    acc = client.post("/api/accounts", json={}).json()
    r = _upload(client, acc["id"])
    job_id = r.json()["job_id"]

    from app import storage
    job = storage.load_job(job_id)
    assert job["progress"] == {"done": 2, "total": 2}


def test_job_times_out_and_is_marked_error(client, monkeypatch):
    monkeypatch.setenv("RECONOPS_JOB_TIMEOUT_SECONDS", "0")
    import importlib
    from app import main
    importlib.reload(main)

    def slow_run_job(**kwargs):
        time.sleep(0.2)
        raise AssertionError("should have timed out before this returns")
    monkeypatch.setattr(main, "run_job", slow_run_job)

    from fastapi.testclient import TestClient
    with TestClient(main.app) as client2:
        _login(client2)
        acc = client2.post("/api/accounts", json={}).json()
        r = _upload(client2, acc["id"])
        job_id = r.json()["job_id"]

    from app import storage
    job = storage.load_job(job_id)
    assert job["status"] == "error"
    assert "processing limit" in job["error"]
