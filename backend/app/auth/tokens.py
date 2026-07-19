"""HMAC-signed, short-lived export tokens.

<a href> downloads can't carry headers or need long-lived credentials in
the URL. Instead the frontend asks for a 5-minute token scoped to one job,
and the export endpoint accepts only that.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from typing import Optional

from ..config import data_dir
from ..memory.fsutil import named_lock


def _secret() -> bytes:
    env = os.getenv("RECONOPS_SECRET", "").strip()
    if env:
        return env.encode("utf-8")
    p = data_dir() / "auth" / "secret"
    if not p.exists():
        with named_lock("auth"):
            if not p.exists():
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(secrets.token_hex(32), encoding="utf-8")
    return p.read_text(encoding="utf-8").strip().encode("utf-8")


def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def make_export_token(job_id: str, account_id: str, ttl: int = 300) -> str:
    payload = f"{job_id}|{account_id}|{int(time.time()) + ttl}"
    b64 = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    return f"{b64}.{_sign(payload)}"


def check_export_token(token: str, job_id: str) -> Optional[str]:
    try:
        b64, sig = token.rsplit(".", 1)
        payload = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)).decode("utf-8")
        t_job, t_account, t_exp = payload.split("|")
    except Exception:
        return None
    if not hmac.compare_digest(sig, _sign(payload)):
        return None
    if t_job != job_id or time.time() > int(t_exp):
        return None
    return t_account
