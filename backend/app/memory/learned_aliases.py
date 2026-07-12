"""Per-account learned column→concept aliases.

This is the highest-leverage piece of memory for column binding. When a
user confirms a binding (`Order Total → order.gross_total`), we upsert
both:

  - the literal column name (case-folded, whitespace-normalized) → concept_id
  - a small set of normalized variants so near-matches benefit too

On the next file from the same source, `bind_columns` consults this store
*before* the global ontology aliases — so the account's vocabulary takes
priority over our seeded one.

The file shape (JSON):

  {
    "aliases": {
      "<normalized_column_name>": {
        "concept_id": "order.gross_total",
        "confirmed_count": 3,
        "last_confirmed_at": "2026-05-12T12:00:00",
        "sample_columns": ["Order Total", "Total Price", "Grand Total ($)"]
      }
    }
  }
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

DATA_DIR = Path(os.getenv("RECONOPS_DATA_DIR", "data"))


def _path(account_id: str) -> Path:
    return DATA_DIR / "accounts" / account_id / "learned_aliases.json"


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _load(account_id: str) -> Dict[str, Dict]:
    p = _path(account_id)
    if not p.exists():
        return {"aliases": {}}
    return json.loads(p.read_text(encoding="utf-8"))


def _save(account_id: str, data: Dict) -> None:
    p = _path(account_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, default=str, indent=2), encoding="utf-8")


def lookup(account_id: str, column_name: str) -> Optional[str]:
    """Return a learned concept_id for this column name, or None."""
    data = _load(account_id)
    aliases = data.get("aliases", {})
    return (aliases.get(_norm(column_name)) or {}).get("concept_id")


def upsert(account_id: str, column_name: str, concept_id: str) -> None:
    """Record (or strengthen) a user-confirmed binding."""
    data = _load(account_id)
    aliases = data.setdefault("aliases", {})
    key = _norm(column_name)
    if not key:
        return
    entry = aliases.get(key, {
        "concept_id": concept_id,
        "confirmed_count": 0,
        "sample_columns": [],
    })
    entry["concept_id"] = concept_id   # latest wins; user can override
    entry["confirmed_count"] = entry.get("confirmed_count", 0) + 1
    entry["last_confirmed_at"] = datetime.utcnow().isoformat()
    samples = entry.setdefault("sample_columns", [])
    if column_name not in samples:
        samples.append(column_name)
        # keep small
        entry["sample_columns"] = samples[-5:]
    aliases[key] = entry
    _save(account_id, data)


def all_aliases(account_id: str) -> Dict[str, Dict]:
    return _load(account_id).get("aliases", {})
