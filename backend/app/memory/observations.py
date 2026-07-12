"""Agent-written durable notes about an account.

These are "the system telling itself what it has noticed" — separate from
brand_notes (user-authored). Examples:

  - "Stripe payouts on this account run 2.7% on average, not the
    seeded 2.9%. Consider proposing a custom rule."
  - "Three consecutive jobs show wholesale invoices arriving 5+ days late
    on Acme orders."

The agent writes them; the UI surfaces them on /observations (Phase 5)
with a "mark wrong" feedback button that appends to the decision log.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from ..config import data_dir
from .fsutil import account_lock

DATA_DIR = data_dir()


def _path(account_id: str) -> Path:
    return DATA_DIR / "accounts" / account_id / "observations.jsonl"


def append(
    account_id: str,
    text: str,
    *,
    category: str = "general",
    job_id: Optional[str] = None,
    evidence: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    p = _path(account_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "at": datetime.utcnow().isoformat(),
        "text": text,
        "category": category,
        "job_id": job_id,
        "evidence": evidence or {},
    }
    with account_lock(account_id):
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    return entry


def recent(account_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    p = _path(account_id)
    if not p.exists():
        return []
    entries: List[Dict[str, Any]] = []
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries[-limit:][::-1]   # newest first
