from datetime import date
from unittest.mock import MagicMock

import httpx
import pytest

from app.integrations import shopify
from app.tools.binding import bind_columns
from app.ontology import concept_by_id


@pytest.fixture(autouse=True)
def _clean_db():
    """Override the conftest autouse _clean_db fixture — these tests don't need the database."""
    yield


def _resp(json_data, headers=None, status_code=200):
    r = MagicMock(spec=httpx.Response)
    r.status_code = status_code
    r.headers = headers or {}
    r.json.return_value = json_data
    r.raise_for_status.return_value = None
    return r


def test_validate_credentials_raises_on_401(monkeypatch):
    resp = _resp({}, status_code=401)
    monkeypatch.setattr(httpx, "get", lambda url, headers=None, timeout=None: resp)
    with pytest.raises(shopify.ShopifyAuthError):
        shopify.validate_credentials("test-shop.myshopify.com", "bad-token")


def test_validate_credentials_passes_on_200(monkeypatch):
    resp = _resp({"shop": {"name": "Test Shop"}}, status_code=200)
    monkeypatch.setattr(httpx, "get", lambda url, headers=None, timeout=None: resp)
    shopify.validate_credentials("test-shop.myshopify.com", "good-token")  # must not raise


def test_fetch_orders_single_page(monkeypatch):
    order = {
        "name": "#1001", "created_at": "2026-07-01T00:00:00-04:00",
        "total_price": "99.50", "total_tax": "8.20",
        "currency": "USD", "financial_status": "paid",
    }
    resp = _resp({"orders": [order]})
    monkeypatch.setattr(httpx.Client, "get", lambda self, url, params=None: resp)

    df = shopify.fetch_orders("test-shop.myshopify.com", "token", date(2026, 7, 1), date(2026, 7, 31))

    assert list(df.columns) == ["order_id", "created_at", "order_total", "tax", "currency", "order_status"]
    assert df.iloc[0]["order_id"] == "#1001"
    assert df.iloc[0]["order_total"] == "99.50"


def test_fetch_orders_follows_pagination_link(monkeypatch):
    page1 = _resp(
        {"orders": [{"name": "#1"}]},
        headers={"Link": '<https://test-shop.myshopify.com/admin/api/2024-01/orders.json?page_info=abc>; rel="next"'},
    )
    page2 = _resp({"orders": [{"name": "#2"}]})
    calls = {"n": 0}

    def fake_get(self, url, params=None):
        calls["n"] += 1
        return page1 if calls["n"] == 1 else page2

    monkeypatch.setattr(httpx.Client, "get", fake_get)

    df = shopify.fetch_orders("test-shop.myshopify.com", "token", date(2026, 7, 1), date(2026, 7, 31))

    assert calls["n"] == 2
    assert len(df) == 2


def test_fetch_orders_retries_on_rate_limit_then_succeeds(monkeypatch):
    rate_limited = _resp({}, headers={"Retry-After": "0"}, status_code=429)
    ok = _resp({"orders": [{"name": "#1"}]})
    calls = {"n": 0}

    def fake_get(self, url, params=None):
        calls["n"] += 1
        return rate_limited if calls["n"] == 1 else ok

    monkeypatch.setattr(httpx.Client, "get", fake_get)
    monkeypatch.setattr(shopify.time, "sleep", lambda s: None)

    df = shopify.fetch_orders("test-shop.myshopify.com", "token", date(2026, 7, 1), date(2026, 7, 31))

    assert calls["n"] == 2
    assert len(df) == 1


def test_fetch_orders_raises_after_exhausting_retries(monkeypatch):
    rate_limited = _resp({}, headers={"Retry-After": "0"}, status_code=429)
    monkeypatch.setattr(httpx.Client, "get", lambda self, url, params=None: rate_limited)
    monkeypatch.setattr(shopify.time, "sleep", lambda s: None)

    with pytest.raises(shopify.ShopifyRateLimitError):
        shopify.fetch_orders("test-shop.myshopify.com", "token", date(2026, 7, 1), date(2026, 7, 31))


def test_fetch_orders_raises_auth_error_on_401(monkeypatch):
    resp = _resp({}, status_code=401)
    monkeypatch.setattr(httpx.Client, "get", lambda self, url, params=None: resp)

    with pytest.raises(shopify.ShopifyAuthError):
        shopify.fetch_orders("test-shop.myshopify.com", "token", date(2026, 7, 1), date(2026, 7, 31))


def test_mapped_columns_bind_to_expected_ontology_roles():
    orders = [
        {
            "name": f"#{1000 + i}", "created_at": "2026-07-01T00:00:00-04:00",
            "total_price": f"{10 + i}.00", "total_tax": "1.00",
            "currency": "USD", "financial_status": "paid",
        }
        for i in range(3)
    ]
    df = shopify._rows_to_dataframe(orders)
    bindings = bind_columns(df)
    role_by_column = {}
    for b in bindings:
        c = concept_by_id(b.concept_id)
        if c:
            role_by_column[b.column_name] = c.role
    assert role_by_column.get("order_id") == "primary_key"
    assert role_by_column.get("order_total") == "primary_amount"
