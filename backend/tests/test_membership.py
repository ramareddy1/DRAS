import importlib

import pytest


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
    r = client.post("/api/auth/verify", json={"email": email, "code": code})
    assert r.status_code == 200
    return r


def test_everything_401s_without_session(client):
    assert client.get("/api/jobs").status_code == 401
    assert client.get("/api/rules", headers={"X-Account-Id": "x"}).status_code == 401
    assert client.post("/api/accounts", json={}).status_code == 401
    assert client.get("/api/concepts").status_code == 401
    assert client.get("/api/health").status_code == 200   # only health + auth stay open


def test_create_account_grants_ownership(client):
    _login(client)
    acc = client.post("/api/accounts", json={}).json()
    me = client.get("/api/auth/me").json()
    assert me["accounts"][0]["account_id"] == acc["id"]
    assert me["accounts"][0]["role"] == "owner"
    r = client.get("/api/rules", headers={"X-Account-Id": acc["id"]})
    assert r.status_code == 200


def test_membership_enforced_cross_account(client):
    _login(client, "a@x.co")
    acc_a = client.post("/api/accounts", json={}).json()
    client.post("/api/auth/logout")
    _login(client, "b@x.co")
    r = client.get("/api/rules", headers={"X-Account-Id": acc_a["id"]})
    assert r.status_code == 403


def test_legacy_account_claim_once(client):
    # A pre-auth account (no members) can be claimed by presenting its UUID
    from app.memory import accounts as accounts_memory, rules_store
    legacy = accounts_memory.create_account()
    rules_store.seed_defaults(legacy.id)

    _login(client, "owner@x.co")
    r = client.post("/api/accounts/claim", json={"account_id": legacy.id})
    assert r.status_code == 200
    assert client.get("/api/rules", headers={"X-Account-Id": legacy.id}).status_code == 200
    # Second claim by someone else -> 409
    client.post("/api/auth/logout")
    _login(client, "intruder@x.co")
    assert client.post("/api/accounts/claim",
                       json={"account_id": legacy.id}).status_code == 409
