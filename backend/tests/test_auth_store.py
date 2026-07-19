import json

import pytest


@pytest.fixture()
def auth_env(tmp_path, monkeypatch):
    monkeypatch.setenv("RECONOPS_DATA_DIR", str(tmp_path))
    from app.auth import store
    return store, tmp_path


def test_user_created_once_per_email(auth_env):
    store, _ = auth_env
    u1 = store.get_or_create_user("Alice@Example.COM ")
    u2 = store.get_or_create_user("alice@example.com")
    assert u1["id"] == u2["id"]
    assert u1["email"] == "alice@example.com"


def test_code_roundtrip_and_secrets_hashed(auth_env):
    store, tmp = auth_env
    code = store.issue_code("a@b.co")
    assert len(code) == 6 and code.isdigit()
    raw = (tmp / "auth" / "codes.json").read_text(encoding="utf-8")
    assert code not in raw                      # hashed at rest
    assert store.verify_code("a@b.co", "000000") is False
    assert store.verify_code("a@b.co", code) is True
    assert store.verify_code("a@b.co", code) is False   # single-use


def test_code_rate_limit(auth_env):
    store, _ = auth_env
    for _ in range(store.MAX_CODES_PER_HOUR):
        store.issue_code("spam@x.co")
    with pytest.raises(store.RateLimited):
        store.issue_code("spam@x.co")


def test_verify_attempts_capped(auth_env):
    store, _ = auth_env
    code = store.issue_code("brute@x.co")
    for _ in range(store.MAX_VERIFY_ATTEMPTS):
        assert store.verify_code("brute@x.co", "999999") is False
    assert store.verify_code("brute@x.co", code) is False  # burned by attempts


def test_session_lifecycle(auth_env):
    store, tmp = auth_env
    u = store.get_or_create_user("s@x.co")
    token = store.create_session(u["id"])
    raw = (tmp / "auth" / "sessions.json").read_text(encoding="utf-8")
    assert token not in raw                     # hashed at rest
    assert store.session_user_id(token) == u["id"]
    store.delete_session(token)
    assert store.session_user_id(token) is None
    assert store.session_user_id("garbage") is None
