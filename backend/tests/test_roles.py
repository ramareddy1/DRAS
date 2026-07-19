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


def _owner_with_analyst(client):
    _login(client, "owner@x.co")
    acc = client.post("/api/accounts", json={}).json()
    client.post("/api/accounts/me/members", json={"email": "analyst@x.co"},
                headers={"X-Account-Id": acc["id"]})
    client.post("/api/auth/logout")
    _login(client, "analyst@x.co")
    return acc


def test_analyst_can_work_but_not_administer(client):
    acc = _owner_with_analyst(client)
    h = {"X-Account-Id": acc["id"]}
    assert client.get("/api/rules", headers=h).status_code == 200
    assert client.get("/api/inbox", headers=h).status_code == 200
    # Owner-only surfaces:
    assert client.patch("/api/accounts/me/profile", json={"materiality_abs": 5},
                        headers=h).status_code == 403
    assert client.post("/api/rules/nonexistent/accept", headers=h).status_code == 403
    assert client.post("/api/rules/nonexistent/revoke", headers=h).status_code == 403
    assert client.post("/api/accounts/me/members", json={"email": "c@x.co"},
                       headers=h).status_code == 403


def test_owner_sees_member_list(client):
    acc = _owner_with_analyst(client)
    client.post("/api/auth/logout")
    _login(client, "owner@x.co")
    r = client.get("/api/accounts/me/members", headers={"X-Account-Id": acc["id"]})
    emails = sorted(m["email"] for m in r.json()["members"].values())
    assert emails == ["analyst@x.co", "owner@x.co"]


def test_decisions_carry_user_identity(client):
    _login(client, "who@x.co")
    acc = client.post("/api/accounts", json={}).json()
    client.post("/api/decisions", json={"signature": "sig123", "user_status": "expected"},
                headers={"X-Account-Id": acc["id"]})
    from app.memory import decision_log
    entries = list(decision_log.replay(acc["id"]))
    assert entries[-1].user_email == "who@x.co"
    assert entries[-1].user_id
