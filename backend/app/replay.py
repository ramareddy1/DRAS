"""Replay engine — regression detection for a learning system.

Once the system learns from corrections, "did a new rule break an old verdict?"
becomes a real question. This module replays an account's matched rows
against the *current* rule set and compares the resulting classification to
the user's recorded truth (the decision log).

Pure-deterministic by design: replay never calls the LLM. It re-derives each
row's verdict from current rules + the row's stored deterministic status, so
it can run in CI with no API key. (LLM escalation only ever fires for
genuinely-ambiguous rows; those aren't what rule regressions are about.)

Definitions
-----------
truth(signature) — the latest `user_status` the user recorded for that
                   signature in the decision log.
satisfied        — for a concrete status correction (match/minor/major/
                   fee_offset): current verdict == user_status. For
                   "expected": the row is auto-handled now (match or a fee/
                   force rule fired), i.e. it wouldn't surface in triage.

A *regression* is a signature that was satisfied before (by the original
verdict) but is not satisfied under the current rules.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .memory import decision_log, rules_store, triage as triage_store
from .models import Rationale

ROW_STATUSES = ("match", "minor", "major", "fee_offset")
AUTO_HANDLED = ("match", "fee_offset")


@dataclass
class ReplayReport:
    account_id: str
    evaluated: int = 0
    accuracy_before: float = 1.0
    accuracy_after: float = 1.0
    regressions: List[Dict[str, Any]] = field(default_factory=list)
    override_rate: float = 0.0
    notes: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "account_id": self.account_id,
            "evaluated": self.evaluated,
            "accuracy_before": round(self.accuracy_before, 4),
            "accuracy_after": round(self.accuracy_after, 4),
            "regressions": self.regressions,
            "override_rate": round(self.override_rate, 4),
            "notes": self.notes,
        }


def _satisfied(user_status: str, status: str) -> Optional[bool]:
    if user_status in ROW_STATUSES:
        return status == user_status
    if user_status == "expected":
        return status in AUTO_HANDLED
    return None  # investigate / observation_wrong / etc. — not an accuracy signal


def _current_status(rules, row_ctx: Dict[str, Any], stored_status: str) -> str:
    """Re-derive a row's verdict under the current rules (no LLM)."""
    rat = rules_store.apply_rules_to_matched(rules, row_ctx)
    status = rat.status if rat is not None else stored_status
    # force_status pass mirrors the agent: signature is computed from the verdict.
    sig = triage_store.signature_for_matched(
        Rationale(row_key=str(row_ctx.get("key", "")), status=status, confidence=1.0), row_ctx
    )
    forced = rules_store.apply_force_status_rules(rules, sig, str(row_ctx.get("key", "")))
    if forced is not None:
        status = forced.status
    return status


def _row_ctx_from_matched(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "key": row.get("key"),
        "amount_a": row.get("amount_a"),
        "amount_b": row.get("amount_b"),
        "diff_abs": row.get("diff_abs"),
        "diff_pct": row.get("diff_pct"),
    }


def replay(account_id: str, matched_rows: List[Dict[str, Any]]) -> ReplayReport:
    """Replay matched rows against current rules vs. the decision-log truth.

    `matched_rows` is the shape produced by the agent (AgentOutput.matched):
    each dict has key/amount_a/amount_b/diff_abs and a nested `rationale` dict.
    """
    report = ReplayReport(account_id=account_id)

    # truth map: signature -> (user_status, original_status)
    truth: Dict[str, Dict[str, Any]] = {}
    for e in decision_log.replay(account_id):
        if not e.signature or not e.user_status:
            continue
        truth[e.signature] = {"user_status": e.user_status, "original_status": e.original_status}

    if not truth:
        report.notes.append("no decision-log truth for this account; nothing to evaluate")
        return report

    rules = rules_store.load_rules(account_id)

    # Map each truth signature to a representative row.
    rep: Dict[str, Dict[str, Any]] = {}
    for row in matched_rows:
        rat = row.get("rationale") or {}
        try:
            sig = triage_store.signature_for_matched(Rationale.model_validate(rat), row)
        except Exception:
            continue
        if sig in truth and sig not in rep:
            rep[sig] = row

    before_hits = after_hits = total = 0
    for sig, t in truth.items():
        row = rep.get(sig)
        if row is None:
            continue  # the corrected signature isn't present in the supplied rows
        stored_status = (row.get("rationale") or {}).get("status", "match")
        before_status = t.get("original_status") or stored_status
        cur_status = _current_status(rules, _row_ctx_from_matched(row), stored_status)

        sb = _satisfied(t["user_status"], before_status)
        sa = _satisfied(t["user_status"], cur_status)
        if sa is None:
            continue
        total += 1
        if sb:
            before_hits += 1
        if sa:
            after_hits += 1
        if sb and not sa:
            report.regressions.append({
                "signature": sig, "user_status": t["user_status"],
                "before": before_status, "after": cur_status,
                "row_key": row.get("key"),
            })

    report.evaluated = total
    report.accuracy_before = (before_hits / total) if total else 1.0
    report.accuracy_after = (after_hits / total) if total else 1.0
    report.override_rate = _override_rate(account_id)
    return report


def _override_rate(account_id: str) -> float:
    """Fraction of recorded corrections that flipped an auto-handled verdict.
    Mirrors memory.metrics but computed over the full log for the trend."""
    entries = list(decision_log.replay(account_id))
    if not entries:
        return 0.0
    window = entries[-200:]
    overrides = sum(
        1 for e in window
        if e.user_status and e.original_status
        and e.original_status in AUTO_HANDLED
        and e.user_status not in (e.original_status, "expected")
    )
    return min(1.0, overrides / max(1, len(window)))
