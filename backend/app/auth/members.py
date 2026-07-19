"""Account membership: per-account members.json + global reverse index.

Org == Account (1:1). Roles: "owner" (settings, rules accept/revoke,
member management) and "analyst" (everything else). Paths resolve via
data_dir() at call time — no module reloads needed in tests.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from ..config import data_dir
from ..memory.fsutil import atomic_write_json, named_lock


def _members_path(account_id: str):
    return data_dir() / "accounts" / account_id / "members.json"


def _index_path():
    return data_dir() / "auth" / "memberships.json"


def _load(p) -> Dict[str, Any]:
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def add_member(account_id: str, user_id: str, email: str, role: str) -> None:
    with named_lock("auth"):
        mp = _members_path(account_id)
        members = _load(mp)
        members[user_id] = {"role": role, "email": email, "added_at": time.time()}
        atomic_write_json(mp, members, indent=2)
        ip = _index_path()
        index = _load(ip)
        ids = set(index.get(user_id, []))
        ids.add(account_id)
        index[user_id] = sorted(ids)
        atomic_write_json(ip, index, indent=2)


def role_of(account_id: str, user_id: str) -> Optional[str]:
    return (_load(_members_path(account_id)).get(user_id) or {}).get("role")


def has_members(account_id: str) -> bool:
    return bool(_load(_members_path(account_id)))


def members_of(account_id: str) -> Dict[str, Any]:
    return _load(_members_path(account_id))


def accounts_for_user(user_id: str) -> List[dict]:
    out = []
    for aid in _load(_index_path()).get(user_id, []):
        role = role_of(aid, user_id)
        if role:
            out.append({"account_id": aid, "role": role})
    return out
