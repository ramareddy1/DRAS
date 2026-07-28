import importlib

import pandas as pd
import pytest
from cryptography.fernet import Fernet


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RECONOPS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RECONOPS_AUTH_DEV", "1")
    monkeypatch.setenv("RECONOPS_ENCRYPTION_KEY", Fernet.generate_key().decode())
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


def _account(client, email="owner@x.co"):
    _login(client, email)
    return client.post("/api/accounts", json={}).json()


def test_connect_requires_owner(client, monkeypatch):
    acc = _account(client, "owner@x.co")
    client.post("/api/accounts/me/members", json={"email": "analyst@x.co"},
                headers={"X-Account-Id": acc["id"]})
    client.post("/api/auth/logout")
    _login(client, "analyst@x.co")
    h = {"X-Account-Id": acc["id"]}

    from app.integrations import shopify
    monkeypatch.setattr(shopify, "validate_credentials", lambda *a, **k: None)
    r = client.post("/api/connections/shopify",
                     json={"shop_domain": "test-shop.myshopify.com", "access_token": "tok"},
                     headers=h)
    assert r.status_code == 403


def test_connect_rejects_bad_domain(client):
    acc = _account(client)
    h = {"X-Account-Id": acc["id"]}
    r = client.post("/api/connections/shopify",
                     json={"shop_domain": "not-a-shop", "access_token": "tok"},
                     headers=h)
    assert r.status_code == 400


def test_connect_rejects_invalid_token(client, monkeypatch):
    acc = _account(client)
    h = {"X-Account-Id": acc["id"]}

    from app.integrations import shopify
    def _raise(*a, **k):
        raise shopify.ShopifyAuthError("nope")
    monkeypatch.setattr(shopify, "validate_credentials", _raise)

    r = client.post("/api/connections/shopify",
                     json={"shop_domain": "test-shop.myshopify.com", "access_token": "bad"},
                     headers=h)
    assert r.status_code == 400


def test_connect_then_list_then_disconnect(client, monkeypatch):
    acc = _account(client)
    h = {"X-Account-Id": acc["id"]}

    from app.integrations import shopify
    monkeypatch.setattr(shopify, "validate_credentials", lambda *a, **k: None)

    r = client.post("/api/connections/shopify",
                     json={"shop_domain": "test-shop.myshopify.com", "access_token": "tok"},
                     headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["shop_domain"] == "test-shop.myshopify.com"
    assert "access_token" not in body
    assert "encrypted_token" not in body

    r = client.get("/api/connections", headers=h)
    assert len(r.json()) == 1

    r = client.delete("/api/connections/shopify", headers=h)
    assert r.status_code == 200

    r = client.get("/api/connections", headers=h)
    assert r.json() == []


def test_disconnect_is_noop_when_nothing_connected(client):
    acc = _account(client)
    h = {"X-Account-Id": acc["id"]}
    r = client.delete("/api/connections/shopify", headers=h)
    assert r.status_code == 200


def test_orders_404_without_connection(client):
    acc = _account(client)
    h = {"X-Account-Id": acc["id"]}
    r = client.post("/api/connections/shopify/orders",
                     json={"start_date": "2026-01-01", "end_date": "2026-01-31"},
                     headers=h)
    assert r.status_code == 404


def test_orders_rejects_oversized_range(client, monkeypatch):
    acc = _account(client)
    h = {"X-Account-Id": acc["id"]}
    from app.integrations import shopify
    monkeypatch.setattr(shopify, "validate_credentials", lambda *a, **k: None)
    client.post("/api/connections/shopify",
                json={"shop_domain": "test-shop.myshopify.com", "access_token": "tok"}, headers=h)

    r = client.post("/api/connections/shopify/orders",
                     json={"start_date": "2026-01-01", "end_date": "2026-12-31"},
                     headers=h)
    assert r.status_code == 400


def test_orders_returns_csv(client, monkeypatch):
    acc = _account(client)
    h = {"X-Account-Id": acc["id"]}
    from app.integrations import shopify
    monkeypatch.setattr(shopify, "validate_credentials", lambda *a, **k: None)
    client.post("/api/connections/shopify",
                json={"shop_domain": "test-shop.myshopify.com", "access_token": "tok"}, headers=h)

    monkeypatch.setattr(shopify, "fetch_orders",
                         lambda *a, **k: pd.DataFrame([{"order_id": "#1", "order_total": "9.99"}]))

    r = client.post("/api/connections/shopify/orders",
                     json={"start_date": "2026-01-01", "end_date": "2026-01-31"},
                     headers=h)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "#1" in r.text


def test_connections_are_account_isolated(client, monkeypatch):
    acc_a = _account(client, "a@x.co")
    from app.integrations import shopify
    monkeypatch.setattr(shopify, "validate_credentials", lambda *a, **k: None)
    client.post("/api/connections/shopify",
                json={"shop_domain": "shop-a.myshopify.com", "access_token": "tok"},
                headers={"X-Account-Id": acc_a["id"]})

    client.post("/api/auth/logout")
    acc_b = _account(client, "b@x.co")
    r = client.get("/api/connections", headers={"X-Account-Id": acc_b["id"]})
    assert r.json() == []
