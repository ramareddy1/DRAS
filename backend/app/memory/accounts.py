"""Account-scoped storage.

Each account is one row in the `accounts` Postgres table, keyed by its UUID.
This module owns the lifecycle (create, load, update) for the Account
entity — later memory modules (rules, decisions, triage, metrics) reference
the same id as their own `account_id` foreign key.

The pilot has no auth beyond the UUID/session pairing set up in Phase 2.1.
"""
from __future__ import annotations

import re
import shutil
from typing import Optional

from ..models import Account, AccountProfile
from ..db.base import session_scope
from ..db.models import AccountORM
from .fsutil import account_lock

# UUID v4 with dashes, lowercase hex
_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _is_valid_id(account_id: str) -> bool:
    return bool(_ID_RE.match(account_id or ""))


def create_account(display_name: Optional[str] = None) -> Account:
    acc = Account(display_name=display_name)
    with session_scope() as s:
        s.add(AccountORM(id=acc.id, payload=acc.model_dump(mode="json")))
    return acc


def load_account(account_id: str) -> Optional[Account]:
    if not _is_valid_id(account_id):
        return None
    with session_scope() as s:
        row = s.get(AccountORM, account_id)
        if row is None:
            return None
        return Account.model_validate(row.payload)


def update_profile(account_id: str, partial: dict) -> Account:
    with account_lock(account_id):
        acc = load_account(account_id)
        if acc is None:
            raise ValueError(f"Account {account_id} not found")
        merged = acc.profile.model_dump()
        merged.update({k: v for k, v in partial.items() if v is not None})
        acc.profile = AccountProfile.model_validate(merged)
        with session_scope() as s:
            row = s.get(AccountORM, account_id)
            row.payload = acc.model_dump(mode="json")
    return acc


def account_exists(account_id: str) -> bool:
    if not _is_valid_id(account_id):
        return False
    with session_scope() as s:
        return s.get(AccountORM, account_id) is not None


def delete_account(account_id: str) -> None:
    """Full purge of one workspace: the Postgres row (cascades to jobs,
    rules, triage items, decisions, and metrics via ON DELETE CASCADE), its
    S3 uploads, and its local JSON directory (members.json, learned
    aliases, observations, notes). Does not touch the global membership
    index (see app.auth.members.remove_account) or users.json/sessions.json
    (user-level state, not account-scoped)."""
    from .. import storage_s3
    from ..config import data_dir

    with account_lock(account_id):
        with session_scope() as s:
            row = s.get(AccountORM, account_id)
            if row is not None:
                s.delete(row)
        storage_s3.delete_prefix(account_id)
    shutil.rmtree(data_dir() / "accounts" / account_id, ignore_errors=True)
