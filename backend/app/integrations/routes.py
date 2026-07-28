"""Third-party connection endpoints: connect, list, disconnect, and pull data."""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import List

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response

from ..deps import require_account, require_owner
from ..models import Account, Connection
from . import connections_store, crypto, shopify

router = APIRouter(prefix="/api/connections", tags=["connections"])

_SHOP_DOMAIN_RE = re.compile(r"^[a-z0-9-]+\.myshopify\.com$")
MAX_RANGE_DAYS = 180


@router.post("/shopify", response_model=Connection)
def connect_shopify(payload: dict, account: Account = Depends(require_owner)):
    shop_domain = ((payload or {}).get("shop_domain") or "").strip().lower()
    access_token = ((payload or {}).get("access_token") or "").strip()
    if not _SHOP_DOMAIN_RE.match(shop_domain):
        raise HTTPException(status_code=400, detail="Enter a valid *.myshopify.com domain.")
    if not access_token:
        raise HTTPException(status_code=400, detail="Access token is required.")
    if not crypto.encryption_configured():
        raise HTTPException(status_code=500, detail="Integrations are not configured on this server.")
    try:
        shopify.validate_credentials(shop_domain, access_token)
    except shopify.ShopifyAuthError:
        raise HTTPException(status_code=400, detail="Shopify rejected this access token.")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Could not reach Shopify: {e}")
    return connections_store.upsert_connection(account.id, "shopify", shop_domain, access_token)


@router.get("", response_model=List[Connection])
def list_connections(account: Account = Depends(require_account)):
    return connections_store.list_connections(account.id)


@router.delete("/{provider}")
def disconnect(provider: str, account: Account = Depends(require_owner)):
    connections_store.delete_connection(account.id, provider)
    return {"ok": True}


@router.post("/shopify/orders")
def pull_shopify_orders(payload: dict, account: Account = Depends(require_account)):
    if not crypto.encryption_configured():
        raise HTTPException(status_code=500, detail="Integrations are not configured on this server.")
    conn = connections_store.get_connection(account.id, "shopify")
    if conn is None:
        raise HTTPException(status_code=404, detail="No Shopify connection for this workspace.")

    try:
        start = date.fromisoformat((payload or {}).get("start_date", ""))
        end = date.fromisoformat((payload or {}).get("end_date", ""))
    except ValueError:
        raise HTTPException(status_code=400, detail="start_date and end_date must be YYYY-MM-DD.")
    if end < start:
        raise HTTPException(status_code=400, detail="end_date must be on or after start_date.")
    if (end - start) > timedelta(days=MAX_RANGE_DAYS):
        raise HTTPException(status_code=400, detail=f"Date range cannot exceed {MAX_RANGE_DAYS} days.")

    access_token = connections_store.get_decrypted_token(account.id, "shopify")
    try:
        df = shopify.fetch_orders(conn.shop_domain, access_token, start, end)
    except shopify.ShopifyAuthError:
        connections_store.mark_error(account.id, "shopify")
        raise HTTPException(status_code=502,
                             detail="Shopify rejected the stored credential — reconnect your store.")
    except shopify.ShopifyRateLimitError:
        raise HTTPException(status_code=502,
                             detail="Shopify rate-limited this request — try a smaller date range or retry shortly.")
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"Could not reach Shopify: {e}")

    connections_store.mark_synced(account.id, "shopify")
    csv_bytes = df.to_csv(index=False).encode()
    filename = f"shopify-orders-{start.isoformat()}_{end.isoformat()}.csv"
    return Response(
        content=csv_bytes, media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
