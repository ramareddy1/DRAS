import importlib

import pytest


def test_token_roundtrip_and_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("RECONOPS_DATA_DIR", str(tmp_path))
    from app.auth import tokens
    t = tokens.make_export_token("job-1", "acc-1", ttl=300)
    assert tokens.check_export_token(t, "job-1") == "acc-1"
    assert tokens.check_export_token(t, "job-2") is None       # wrong job
    assert tokens.check_export_token(t + "x", "job-1") is None  # tampered


def test_token_expires(tmp_path, monkeypatch):
    monkeypatch.setenv("RECONOPS_DATA_DIR", str(tmp_path))
    from app.auth import tokens
    t = tokens.make_export_token("job-1", "acc-1", ttl=-1)
    assert tokens.check_export_token(t, "job-1") is None


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RECONOPS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RECONOPS_AUTH_DEV", "1")
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


def test_export_endpoints(client):
    _login(client)
    acc = client.post("/api/accounts", json={}).json()
    h = {"X-Account-Id": acc["id"]}
    # Token for a job that doesn't exist -> 404
    assert client.post("/api/results/nope/export-token", headers=h).status_code == 404
    # Export with garbage token -> 401
    assert client.get("/api/results/nope/export?token=garbage").status_code == 401
    # The old account_id query param is gone
    assert client.get(f"/api/results/nope/export?account_id={acc['id']}").status_code == 401
