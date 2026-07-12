"""Per-account decision log — append-only JSONL.

Every time the user disagrees with the system (mark expected, override
classification, rebind a column, override a binding, etc.) one line is
appended here. This is the training signal for rule proposals, the
override-rate metric, and Phase 6's replay-eval.

Append-only by design — corrections to the corrections are themselves
new entries. We never edit history.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator, List, Optional

from ..models import DecisionLogEntry
from .fsutil import account_lock

DATA_DIR = Path(os.getenv("RECONOPS_DATA_DIR", "data"))


def _path(account_id: str) -> Path:
    return DATA_DIR / "accounts" / account_id / "decisions.jsonl"


def append(account_id: str, entry: DecisionLogEntry) -> None:
    p = _path(account_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    with account_lock(account_id):
        with p.open("a", encoding="utf-8") as f:
            f.write(entry.model_dump_json() + "\n")


def replay(account_id: str) -> Iterator[DecisionLogEntry]:
    p = _path(account_id)
    if not p.exists():
        return
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield DecisionLogEntry.model_validate_json(line)
            except Exception:
                # Tolerate malformed lines — log corruption shouldn't break reads
                continue


def all_entries(account_id: str) -> List[DecisionLogEntry]:
    return list(replay(account_id))
