import importlib

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RECONOPS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RECONOPS_AUTH_DEV", "1")
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


def test_request_code_dev_mode_returns_code(client):
    r = client.post("/api/auth/request-code", json={"email": "a@b.co"})
    assert r.status_code == 200
    assert len(r.json()["dev_code"]) == 6


def test_request_code_rejects_bad_email(client):
    assert client.post("/api/auth/request-code", json={"email": "nope"}).status_code == 400


def test_verify_sets_cookie_and_me_works(client):
    _login(client)
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["email"] == "me@x.co"


def test_wrong_code_401(client):
    client.post("/api/auth/request-code", json={"email": "w@x.co"})
    r = client.post("/api/auth/verify", json={"email": "w@x.co", "code": "000000"})
    assert r.status_code == 401


def test_logout_kills_session(client):
    _login(client)
    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").status_code == 401


def test_rate_limit_429(client):
    for _ in range(5):
        client.post("/api/auth/request-code", json={"email": "r@x.co"})
    assert client.post("/api/auth/request-code", json={"email": "r@x.co"}).status_code == 429
