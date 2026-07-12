"""Per-account TriageItem store + signature-based dedup.

A reconciliation job emits triage items by **signature**, not by row index.
Recurring items (same kind of gap, week after week) accumulate
`source_job_ids` on a single TriageItem instead of cluttering the inbox.

Storage: single JSON file `data/accounts/{id}/triage.json` keyed by item
id. For the pilot we keep both active and resolved items in one file —
queries filter on `state`. The file stays manageable because dedup
prevents linear growth.

Signature shape
---------------
We hash a tuple that groups items the user could plausibly want to apply
the same rule to:

  - `status`                 — match / minor / major / fee_offset / unmatched_a / unmatched_b
  - `fee_rule_id`            — for fee_offset items, which processor's pattern fired
  - `side`                   — for unmatched items, which file
  - `key_prefix`             — short stable token from row_key ("#", "SUB-", "pi_", "" )
  - `sign_bucket`            — "pos" / "neg" / "zero" / "n/a"

This intentionally drops the row-level numbers — two Stripe fee
discrepancies in different months on different orders share a signature.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from ..config import data_dir
from ..models import Rationale, TriageItem, TriageState
from .fsutil import account_lock, atomic_write_json

DATA_DIR = data_dir()


def _path(account_id: str) -> Path:
    return DATA_DIR / "accounts" / account_id / "triage.json"


# ---------------------------------------------------------------------------
# Signature derivation
# ---------------------------------------------------------------------------

_KEY_PREFIX_RE = re.compile(r"^([#A-Za-z]+[-_]?|[#])")


def _key_prefix(key: Optional[str]) -> str:
    if not key:
        return ""
    m = _KEY_PREFIX_RE.match(str(key))
    return (m.group(1) if m else "")[:8].lower()


def _sign_bucket(diff_abs: Optional[float]) -> str:
    if diff_abs is None:
        return "na"
    if diff_abs > 0.005:
        return "pos"
    if diff_abs < -0.005:
        return "neg"
    return "zero"


def _fee_rule_id_from_rationale(rationale: Optional[Rationale]) -> str:
    if rationale is None:
        return ""
    for e in rationale.rationale:
        src = e.source
        if src.startswith(("stripe_fee", "paypal_fee", "rule:")):
            return src
    return ""


def signature_for_matched(rationale: Rationale, row_ctx: Dict[str, Any]) -> str:
    parts = (
        rationale.status,
        _fee_rule_id_from_rationale(rationale),
        "",  # side empty for matched
        _key_prefix(row_ctx.get("key") or rationale.row_key),
        _sign_bucket(row_ctx.get("diff_abs")),
    )
    return _hash(parts)


def signature_for_unmatched(side: str, row_ctx: Dict[str, Any]) -> str:
    """side ∈ {'a', 'b'}. row_ctx must include the original key column or row dict."""
    # Best-effort key extraction — unmatched rows are raw dicts
    candidate_keys = [
        row_ctx.get(k) for k in ("key", "order_id", "transaction_id", "sku", "name", "id")
    ]
    key = next((str(k) for k in candidate_keys if k), "")
    parts = (
        f"unmatched_{side}",
        "",
        side,
        _key_prefix(key),
        "na",
    )
    return _hash(parts)


def _hash(parts: Iterable[str]) -> str:
    raw = "|".join(parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _load_raw(account_id: str) -> Dict[str, Any]:
    p = _path(account_id)
    if not p.exists():
        return {"items": []}
    return json.loads(p.read_text(encoding="utf-8"))


def _save_raw(account_id: str, payload: Dict[str, Any]) -> None:
    atomic_write_json(_path(account_id), payload, indent=2)


def load_all(account_id: str) -> List[TriageItem]:
    raw = _load_raw(account_id)
    return [TriageItem.model_validate(r) for r in raw.get("items", [])]


def save_all(account_id: str, items: List[TriageItem]) -> None:
    _save_raw(account_id, {"items": [i.model_dump(mode="json") for i in items]})


def list_open(account_id: str) -> List[TriageItem]:
    return [i for i in load_all(account_id) if i.state in ("open", "recurring", "deferred")]


def find_by_signature(items: List[TriageItem], signature: str) -> Optional[TriageItem]:
    for i in items:
        if i.signature == signature and i.state in ("open", "recurring", "deferred"):
            return i
    return None


def get(account_id: str, item_id: str) -> Optional[TriageItem]:
    for i in load_all(account_id):
        if i.id == item_id:
            return i
    return None


def resolved_expected_signatures(account_id: str, items: Optional[List[TriageItem]] = None) -> set:
    """Signatures the user has explicitly marked expected (or accepted).

    Once a recurring gap is resolved as 'expected', it should stop cluttering
    the inbox on subsequent jobs. The next job's `emit_for_job` consults this
    set and suppresses those signatures — which is what makes insight density
    climb after the user teaches the system.
    """
    items = items if items is not None else load_all(account_id)
    out = set()
    for i in items:
        if i.state == "resolved" and (i.resolution or {}).get("action") in ("mark_expected", "accept"):
            out.add(i.signature)
    return out


# ---------------------------------------------------------------------------
# Emit during a job
# ---------------------------------------------------------------------------


def emit_for_job(
    account_id: str,
    job_id: str,
    *,
    rationales: List[Dict[str, Any]],   # serialized matched rows (with .rationale dict)
    unmatched_a: List[Dict[str, Any]],
    unmatched_b: List[Dict[str, Any]],
    rules_suppressed_signatures: Optional[Iterable[str]] = None,
) -> List[TriageItem]:
    """Materialize TriageItems for everything that would land in the inbox.

    `rules_suppressed_signatures` is the set of signatures the rule engine
    already marked as expected — those should NOT produce inbox items.

    Returns the items emitted (whether new or recurring).
    """
    with account_lock(account_id):
        return _emit_for_job(
            account_id, job_id,
            rationales=rationales,
            unmatched_a=unmatched_a,
            unmatched_b=unmatched_b,
            rules_suppressed_signatures=rules_suppressed_signatures,
        )


def _emit_for_job(
    account_id: str,
    job_id: str,
    *,
    rationales: List[Dict[str, Any]],
    unmatched_a: List[Dict[str, Any]],
    unmatched_b: List[Dict[str, Any]],
    rules_suppressed_signatures: Optional[Iterable[str]] = None,
) -> List[TriageItem]:
    items = load_all(account_id)
    suppressed = set(rules_suppressed_signatures or [])
    # Anything the user has already resolved as "expected" should not re-surface.
    suppressed |= resolved_expected_signatures(account_id, items)
    emitted: List[TriageItem] = []
    now = datetime.utcnow()

    def _bump_or_create(sig: str, **fields) -> TriageItem:
        existing = find_by_signature(items, sig)
        if existing:
            if job_id not in existing.source_job_ids:
                existing.source_job_ids.append(job_id)
            existing.last_seen_at = now
            existing.state = "recurring"
            # refresh display payload to most recent occurrence
            for k, v in fields.items():
                setattr(existing, k, v)
            return existing
        ti = TriageItem(
            account_id=account_id,
            signature=sig,
            source_job_ids=[job_id],
            state="open",
            **fields,
        )
        items.append(ti)
        return ti

    # Matched rows that aren't `match` status
    for row in rationales:
        rat_dict = row.get("rationale") or {}
        if not rat_dict:
            continue
        if rat_dict.get("status") == "match":
            continue
        rat = Rationale.model_validate(rat_dict)
        sig = signature_for_matched(rat, row)
        if sig in suppressed:
            continue
        emitted.append(_bump_or_create(
            sig,
            row_key=row.get("key"),
            status=rat.status,
            side="both",
            amount_a=row.get("amount_a"),
            amount_b=row.get("amount_b"),
            diff_abs=row.get("diff_abs"),
            fee_pattern=row.get("fee_pattern"),
            rationale=rat,
        ))

    for row in unmatched_a:
        sig = signature_for_unmatched("a", row)
        if sig in suppressed:
            continue
        emitted.append(_bump_or_create(
            sig,
            row_key=str(_first_key_value(row)),
            status="unmatched_a",
            side="a",
        ))

    for row in unmatched_b:
        sig = signature_for_unmatched("b", row)
        if sig in suppressed:
            continue
        emitted.append(_bump_or_create(
            sig,
            row_key=str(_first_key_value(row)),
            status="unmatched_b",
            side="b",
        ))

    save_all(account_id, items)
    return emitted


def _first_key_value(row: Dict[str, Any]) -> Any:
    for k in ("order_id", "transaction_id", "sku", "name", "id",
              "invoice_number", "po_number"):
        if k in row and row[k] is not None:
            return row[k]
    # fallback: first non-null value
    for v in row.values():
        if v is not None:
            return v
    return ""


# ---------------------------------------------------------------------------
# Resolve (called by Phase 5 HITL endpoints)
# ---------------------------------------------------------------------------


def resolve(
    account_id: str, item_id: str,
    *, action: str, user_reason: Optional[str] = None,
    rule_id: Optional[str] = None,
) -> Optional[TriageItem]:
    with account_lock(account_id):
        items = load_all(account_id)
        for i in items:
            if i.id == item_id:
                i.state = "resolved" if action in ("mark_expected", "accept") else "deferred"
                i.resolution = {
                    "action": action,
                    "user_reason": user_reason,
                    "rule_id": rule_id,
                    "at": datetime.utcnow().isoformat(),
                }
                save_all(account_id, items)
                return i
    return None
