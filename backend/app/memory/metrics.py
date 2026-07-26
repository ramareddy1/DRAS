"""Insight-density metric stack — per-account, per-job.

Headline metric (the one we trust):

    trust_adjusted_density = insight_density × (1 − override_rate)

Three counter-metrics keep the headline honest:

  * `override_rate`     — fraction of auto-handled rows the user later
                          corrected. Rising = system is being too confident.
  * `revocation_rate`   — fraction of accepted rules that were later
                          turned off. Rising = the system proposed rules
                          too eagerly.
  * `insight_density`   — raw fraction of rows the system handled silently.

This module computes a snapshot per job and appends it to the `metrics`
Postgres table (one row per snapshot, scoped by `account_id`) so Phase 5's
header trendline can chart it without recomputing history.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from ..models import AccountMetrics, Rationale
from . import decision_log, rules_store
from ..db.base import session_scope
from ..db.models import MetricORM


def _is_auto_handled(rationale: Optional[Rationale]) -> bool:
    """A row is 'auto-handled' if its verdict was reached without LLM
    escalation OR explicit user attention. Concretely: status=match, OR
    a rule with `rule:` / `*_fee_*` source fired, OR a deterministic
    classifier reached high confidence.

    The user can still drill in and audit — but the row didn't surface in
    triage and didn't need a second opinion.
    """
    if rationale is None:
        return True
    if rationale.status == "match":
        return True
    # Rule-applied (account rule or seeded fee pattern)
    for e in rationale.rationale:
        s = e.source
        if s.startswith(("rule:", "stripe_fee", "paypal_fee")):
            return True
    return False


def compute_density(
    *,
    matched_rationales: List[Dict[str, Any]],
    triage_items_emitted: int,
    account_id: str,
    job_id: str,
    llm_calls: int = 0,
) -> AccountMetrics:
    total = len(matched_rationales)
    auto = 0
    for row in matched_rationales:
        rat_dict = row.get("rationale") or {}
        try:
            rat = Rationale.model_validate(rat_dict) if rat_dict else None
        except Exception:
            rat = None
        if _is_auto_handled(rat):
            auto += 1

    needed = triage_items_emitted
    denom = max(1, auto + needed)
    density = auto / denom

    override_rate = _override_rate(account_id, matched_rationales)
    revocation_rate = _revocation_rate(account_id)

    trust_adj = density * (1.0 - override_rate)

    return AccountMetrics(
        job_id=job_id,
        at=datetime.utcnow(),
        total_rows=total,
        auto_handled=auto,
        needed_user=needed,
        insight_density=round(density, 4),
        override_rate=round(override_rate, 4),
        revocation_rate=round(revocation_rate, 4),
        trust_adjusted_density=round(trust_adj, 4),
        llm_calls=llm_calls,
    )


def _override_rate(account_id: str, matched_rationales: List[Dict[str, Any]]) -> float:
    """How often does the user later disagree with what the system handled silently?

    Rolling — looks at the last 200 decision log entries for this account
    and counts how many were corrections of an originally-auto-handled
    classification.
    """
    entries = list(decision_log.replay(account_id))
    if not entries:
        return 0.0
    # Use the last 200 to keep the metric responsive
    window = entries[-200:]
    overrides = sum(
        1 for e in window
        if e.user_status and e.original_status and e.user_status != e.original_status
        and e.original_status in ("match",)  # auto-handled set
    )
    # Also count corrections of fee_offset (silent rule-application)
    overrides += sum(
        1 for e in window
        if e.user_status and e.original_status == "fee_offset"
        and e.user_status not in ("fee_offset", "expected")
    )
    return min(1.0, overrides / max(1, len(window)))


def _revocation_rate(account_id: str) -> float:
    rules = rules_store.load_rules(account_id)
    if not rules:
        return 0.0
    revoked = sum(1 for r in rules if r.state == "revoked")
    accepted = sum(1 for r in rules if r.state in ("active", "revoked") and r.origin != "system")
    if accepted == 0:
        return 0.0
    return min(1.0, revoked / accepted)


def snapshot(account_id: str, metrics: AccountMetrics) -> None:
    with session_scope() as s:
        s.add(MetricORM(account_id=account_id, payload=metrics.model_dump(mode="json")))


def series(account_id: str, limit: int = 100) -> List[AccountMetrics]:
    with session_scope() as s:
        rows = (
            s.query(MetricORM)
            .filter(MetricORM.account_id == account_id)
            .order_by(MetricORM.id.desc())
            .limit(limit)
            .all()
        )
        return [AccountMetrics.model_validate(row.payload) for row in reversed(rows)]
