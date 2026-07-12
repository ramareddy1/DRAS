"""Account-scoped storage.

Each account is one directory under data/accounts/{id}/. This module owns
the lifecycle (create, load, update) for the Account entity and bootstraps
the directory layout that later memory modules (rules, decisions, triage,
metrics, etc.) write into.

The pilot has no auth — the UUID returned at creation is the access token.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..models import Account, AccountProfile
from .fsutil import account_lock, atomic_write_json

DATA_DIR = Path(os.getenv("RECONOPS_DATA_DIR", "data"))
ACCOUNTS_DIR = DATA_DIR / "accounts"

# UUID v4 with dashes, lowercase hex
_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _is_valid_id(account_id: str) -> bool:
    return bool(_ID_RE.match(account_id or ""))


def _account_dir(account_id: str) -> Path:
    return ACCOUNTS_DIR / account_id


def _profile_path(account_id: str) -> Path:
    return _account_dir(account_id) / "profile.json"


def _bootstrap_dirs(account_id: str) -> None:
    """Create the account's directory so later phases can write into it."""
    d = _account_dir(account_id)
    d.mkdir(parents=True, exist_ok=True)


def create_account(display_name: Optional[str] = None) -> Account:
    acc = Account(display_name=display_name)
    _bootstrap_dirs(acc.id)
    atomic_write_json(_profile_path(acc.id), json.loads(acc.model_dump_json()), indent=2)
    return acc


def load_account(account_id: str) -> Optional[Account]:
    if not _is_valid_id(account_id):
        return None
    p = _profile_path(account_id)
    if not p.exists():
        return None
    data = json.loads(p.read_text(encoding="utf-8"))
    return Account.model_validate(data)


def update_profile(account_id: str, partial: dict) -> Account:
    with account_lock(account_id):
        acc = load_account(account_id)
        if acc is None:
            raise ValueError(f"Account {account_id} not found")
        merged = acc.profile.model_dump()
        merged.update({k: v for k, v in partial.items() if v is not None})
        acc.profile = AccountProfile.model_validate(merged)
        atomic_write_json(_profile_path(account_id), json.loads(acc.model_dump_json()), indent=2)
    return acc


def account_exists(account_id: str) -> bool:
    return _is_valid_id(account_id) and _profile_path(account_id).exists()
