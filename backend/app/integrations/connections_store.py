"""Per-account third-party connection store (Shopify, and future providers).

One row per (account_id, provider), enforced by a DB unique constraint —
connecting again replaces the existing row entirely. The access token is
Fernet-encrypted before it ever reaches the payload column; this module is
the only place code should reach for a decrypted token.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

from ..db.base import session_scope
from ..db.models import ConnectionORM
from ..models import Connection
from . import crypto


def upsert_connection(account_id: str, provider: str, shop_domain: str, access_token: str) -> Connection:
    encrypted = crypto.encrypt_token(access_token)
    now = datetime.utcnow().isoformat() + "Z"
    payload = {
        "shop_domain": shop_domain,
        "encrypted_token": encrypted,
        "connected_at": now,
        "last_synced_at": None,
    }
    with session_scope() as s:
        row = (
            s.query(ConnectionORM)
            .filter(ConnectionORM.account_id == account_id, ConnectionORM.provider == provider)
            .first()
        )
        if row is None:
            row = ConnectionORM(id=str(uuid.uuid4()), account_id=account_id, provider=provider,
                                 status="connected", payload=payload)
            s.add(row)
        else:
            row.status = "connected"
            row.payload = payload
        connection_id = row.id
    return _to_connection(connection_id, provider, "connected", payload)


def get_connection(account_id: str, provider: str) -> Optional[Connection]:
    with session_scope() as s:
        row = (
            s.query(ConnectionORM)
            .filter(ConnectionORM.account_id == account_id, ConnectionORM.provider == provider)
            .first()
        )
        if row is None:
            return None
        return _to_connection(row.id, row.provider, row.status, row.payload)


def get_decrypted_token(account_id: str, provider: str) -> Optional[str]:
    with session_scope() as s:
        row = (
            s.query(ConnectionORM)
            .filter(ConnectionORM.account_id == account_id, ConnectionORM.provider == provider)
            .first()
        )
        if row is None:
            return None
        return crypto.decrypt_token(row.payload["encrypted_token"])


def list_connections(account_id: str) -> List[Connection]:
    with session_scope() as s:
        rows = s.query(ConnectionORM).filter(ConnectionORM.account_id == account_id).all()
        return [_to_connection(r.id, r.provider, r.status, r.payload) for r in rows]


def delete_connection(account_id: str, provider: str) -> None:
    with session_scope() as s:
        s.query(ConnectionORM).filter(
            ConnectionORM.account_id == account_id, ConnectionORM.provider == provider,
        ).delete()


def mark_synced(account_id: str, provider: str) -> None:
    with session_scope() as s:
        row = (
            s.query(ConnectionORM)
            .filter(ConnectionORM.account_id == account_id, ConnectionORM.provider == provider)
            .first()
        )
        if row is not None:
            row.payload = {**row.payload, "last_synced_at": datetime.utcnow().isoformat() + "Z"}


def mark_error(account_id: str, provider: str) -> None:
    with session_scope() as s:
        row = (
            s.query(ConnectionORM)
            .filter(ConnectionORM.account_id == account_id, ConnectionORM.provider == provider)
            .first()
        )
        if row is not None:
            row.status = "error"


def _to_connection(id_: str, provider: str, status: str, payload: dict) -> Connection:
    return Connection(
        id=id_, provider=provider, status=status,
        shop_domain=payload.get("shop_domain", ""),
        connected_at=payload.get("connected_at"),
        last_synced_at=payload.get("last_synced_at"),
    )
