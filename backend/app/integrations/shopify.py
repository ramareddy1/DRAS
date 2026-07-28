"""Shopify Admin REST API connector.

Pure HTTP + pandas — no FastAPI or DB imports — so it's testable in
isolation from the web layer. Its only job is: given a shop + token + date
range, return a DataFrame whose columns already match the aliases in
app/ontology/concepts.yaml, so the existing bind_columns() picks them up
with zero ontology changes.
"""
from __future__ import annotations

import time
from datetime import date
from typing import Any, Dict, List, Optional

import httpx
import pandas as pd

SHOPIFY_API_VERSION = "2024-01"
PAGE_SIZE = 250
MAX_RETRIES = 3


class ShopifyAuthError(Exception):
    """Shopify rejected the access token (401/403)."""


class ShopifyRateLimitError(Exception):
    """Shopify kept rate-limiting the request past MAX_RETRIES."""


_COLUMN_MAP = {
    "name": "order_id",
    "created_at": "created_at",
    "total_price": "order_total",
    "total_tax": "tax",
    "currency": "currency",
    "financial_status": "order_status",
}


def _headers(access_token: str) -> Dict[str, str]:
    return {"X-Shopify-Access-Token": access_token, "Content-Type": "application/json"}


def validate_credentials(shop_domain: str, access_token: str) -> None:
    """Raises ShopifyAuthError if this token doesn't work against this shop."""
    url = f"https://{shop_domain}/admin/api/{SHOPIFY_API_VERSION}/shop.json"
    resp = httpx.get(url, headers=_headers(access_token), timeout=15)
    if resp.status_code in (401, 403):
        raise ShopifyAuthError("Shopify rejected this access token.")
    resp.raise_for_status()


def _get_with_retries(client: httpx.Client, url: str, params: Optional[Dict[str, Any]]) -> httpx.Response:
    for attempt in range(MAX_RETRIES + 1):
        resp = client.get(url, params=params)
        if resp.status_code == 429:
            if attempt == MAX_RETRIES:
                raise ShopifyRateLimitError("Shopify rate-limited this request.")
            time.sleep(float(resp.headers.get("Retry-After", "1")))
            continue
        if resp.status_code in (401, 403):
            raise ShopifyAuthError("Shopify rejected this access token.")
        resp.raise_for_status()
        return resp
    raise ShopifyRateLimitError("Shopify rate-limited this request.")


def _next_link(link_header: Optional[str]) -> Optional[str]:
    """Parse the Link header for rel="next" (Shopify's cursor pagination)."""
    if not link_header:
        return None
    for part in link_header.split(","):
        segment = part.strip()
        if 'rel="next"' in segment:
            start, end = segment.find("<") + 1, segment.find(">")
            if start > 0 and end > start:
                return segment[start:end]
    return None


def _rows_to_dataframe(orders: List[Dict[str, Any]]) -> pd.DataFrame:
    rows = [{col: order.get(field) for field, col in _COLUMN_MAP.items()} for order in orders]
    return pd.DataFrame(rows, columns=list(_COLUMN_MAP.values()))


def fetch_orders(shop_domain: str, access_token: str, start_date: date, end_date: date) -> pd.DataFrame:
    """Pull every order in [start_date, end_date] and return them as a
    DataFrame with ontology-recognized column names."""
    url = f"https://{shop_domain}/admin/api/{SHOPIFY_API_VERSION}/orders.json"
    params: Optional[Dict[str, Any]] = {
        "status": "any",
        "created_at_min": start_date.isoformat(),
        "created_at_max": end_date.isoformat(),
        "limit": PAGE_SIZE,
    }
    orders: List[Dict[str, Any]] = []
    with httpx.Client(headers=_headers(access_token), timeout=30) as client:
        next_url = url
        while next_url:
            resp = _get_with_retries(client, next_url, params)
            orders.extend(resp.json().get("orders", []))
            next_url = _next_link(resp.headers.get("Link"))
            params = None  # the cursor URL already carries all query params

    return _rows_to_dataframe(orders)
