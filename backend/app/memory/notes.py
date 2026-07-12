"""User-authored brand notes (intake answers, drop-in notes, justifications).

Stored as `data/accounts/{id}/notes.jsonl`. Each note's
`parsed_proposals` field is populated by `tools.extract.extract_from_text`
at the time of writing.

Phase 5 wires the UI for /onboarding and /conversation; this module is
the durable store both routes share.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

DATA_DIR = Path(os.getenv("RECONOPS_DATA_DIR", "data"))


def _path(account_id: str) -> Path:
    return DATA_DIR / "accounts" / account_id / "notes.jsonl"


def append(
    account_id: str,
    text: str,
    *,
    kind: str = "note",
    parsed_proposals: Optional[Dict[str, Any]] = None,
    job_id: Optional[str] = None,
    row_key: Optional[str] = None,
) -> Dict[str, Any]:
    p = _path(account_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "at": datetime.utcnow().isoformat(),
        "kind": kind,
        "text": text,
        "parsed_proposals": parsed_proposals or {},
        "job_id": job_id,
        "row_key": row_key,
    }
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")
    return entry


def all_notes(account_id: str) -> List[Dict[str, Any]]:
    p = _path(account_id)
    if not p.exists():
        return []
    out: List[Dict[str, Any]] = []
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out
