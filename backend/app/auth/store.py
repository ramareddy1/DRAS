"""Users, login codes, sessions — JSON-on-disk, secrets sha256-hashed at rest.

A leaked data directory must not let anyone mint a session or replay a
login code. Codes are single-use, expire in 10 minutes, rate-limited per
email; sessions are opaque 256-bit tokens with a 30-day TTL.

All paths resolve via data_dir() at call time (not import time), so tests
that point RECONOPS_DATA_DIR at a temp dir need no module reloads.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from ..config import data_dir
from ..memory.fsutil import atomic_write_json, named_lock

CODE_TTL_S = 10 * 60
SESSION_TTL_S = 30 * 24 * 3600
MAX_CODES_PER_HOUR = 5
MAX_VERIFY_ATTEMPTS = 5


class RateLimited(Exception):
    pass


def _auth_dir() -> Path:
    return data_dir() / "auth"


def _load(p: Path) -> Dict[str, Any]:
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _norm_email(e: str) -> str:
    return (e or "").strip().lower()


# --- users -----------------------------------------------------------------

def get_or_create_user(email: str) -> Dict[str, Any]:
    email = _norm_email(email)
    with named_lock("auth"):
        p = _auth_dir() / "users.json"
        users = _load(p)
        u = users.get(email)
        if u is None:
            u = {"id": str(uuid.uuid4()), "email": email, "created_at": time.time()}
            users[email] = u
            atomic_write_json(p, users, indent=2)
        return u


def user_by_id(user_id: str) -> Optional[Dict[str, Any]]:
    for u in _load(_auth_dir() / "users.json").values():
        if u.get("id") == user_id:
            return u
    return None


# --- login codes -----------------------------------------------------------

def issue_code(email: str) -> str:
    email = _norm_email(email)
    code = f"{secrets.randbelow(1_000_000):06d}"
    now = time.time()
    with named_lock("auth"):
        p = _auth_dir() / "codes.json"
        codes = _load(p)
        entry = codes.get(email) or {}
        issued = [t for t in entry.get("issued", []) if now - t < 3600]
        if len(issued) >= MAX_CODES_PER_HOUR:
            raise RateLimited(f"Too many codes for {email}; try again later.")
        issued.append(now)
        codes[email] = {"hash": _sha(code), "exp": now + CODE_TTL_S,
                        "attempts": 0, "issued": issued}
        atomic_write_json(p, codes, indent=2)
    return code


def verify_code(email: str, code: str) -> bool:
    email = _norm_email(email)
    now = time.time()
    with named_lock("auth"):
        p = _auth_dir() / "codes.json"
        codes = _load(p)
        entry = codes.get(email)
        if not entry or not entry.get("hash") or now > entry.get("exp", 0):
            return False
        if entry.get("attempts", 0) >= MAX_VERIFY_ATTEMPTS:
            return False
        if _sha(code or "") != entry["hash"]:
            entry["attempts"] = entry.get("attempts", 0) + 1
            codes[email] = entry
            atomic_write_json(p, codes, indent=2)
            return False
        entry.pop("hash", None)   # single-use; keep rate counters
        entry.pop("exp", None)
        codes[email] = entry
        atomic_write_json(p, codes, indent=2)
        return True


# --- sessions --------------------------------------------------------------

def create_session(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with named_lock("auth"):
        p = _auth_dir() / "sessions.json"
        sessions = {h: s for h, s in _load(p).items() if s.get("exp", 0) > now}
        sessions[_sha(token)] = {"user_id": user_id, "exp": now + SESSION_TTL_S,
                                 "created_at": now}
        atomic_write_json(p, sessions, indent=2)
    return token


def session_user_id(token: str) -> Optional[str]:
    if not token:
        return None
    s = _load(_auth_dir() / "sessions.json").get(_sha(token))
    if not s or time.time() > s.get("exp", 0):
        return None
    return s.get("user_id")


def delete_session(token: str) -> None:
    with named_lock("auth"):
        p = _auth_dir() / "sessions.json"
        sessions = _load(p)
        sessions.pop(_sha(token), None)
        atomic_write_json(p, sessions, indent=2)
