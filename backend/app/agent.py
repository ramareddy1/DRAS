"""Agent orchestrator — Phase 3.

This module replaces the v1 `reconciler.reconcile()` pipeline. The agent
composes deterministic primitives from `tools/` and escalates to the LLM
only when needed.

Per-job loop (sketch):
  1. ingest both files
  2. resolve primary_key pair via binding.pick_key_pair (raise to ask_user
     if no high-confidence candidate)
  3. resolve amount/date columns per side
  4. match_by_key
  5. per matched row: propose_classification (LLM escalation on ambiguity)
  6. per unmatched row: build a "no partner" rationale
  7. timing_stats
  8. synthesize_insights via LLM
  9. assemble the result

ask_user protocol:
  When a step needs human input, the agent raises AskUser(question, …).
  The caller (main.py) catches this, persists the job as
  status='awaiting_user' with the question payload, and returns. The user
  answers via POST /api/jobs/{id}/answer, which calls back into the agent
  with the answer applied and the job re-runs from scratch.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from .llm import LLMUnavailable, call_claude
from .memory import (
    metrics as metrics_store,
    rule_proposer,
    rules_store,
    triage as triage_store,
)
from .models import (
    Account, AccountMetrics, BindingSet, Rationale, ReconcileConfig,
    Summary, Evidence, TriageItem,
)
from .tools.amounts import coerce_amount
from .tools.binding import pick_key_pair, resolve_amount_date
from .tools.classify import propose_classification
from .tools.matching import match_by_key
from .tools.timing import coerce_date, timing_stats


# Confidence below which we ask the user to confirm a primary-key binding
ASK_USER_BINDING_THRESHOLD = 0.5


def _first_key_value(row: Dict[str, Any]) -> Any:
    """Best-effort key extraction from an unmatched row dict — used to decide
    whether an expected_unmatched rule applies."""
    for k in ("order_id", "transaction_id", "sku", "name", "id",
              "invoice_number", "po_number"):
        if k in row and row[k] is not None:
            return row[k]
    for v in row.values():
        if v is not None:
            return v
    return ""


# ---------------------------------------------------------------------------
# Pause / resume protocol
# ---------------------------------------------------------------------------

class AskUser(Exception):
    """The agent needs human input to proceed."""
    def __init__(self, question: str, kind: str, context: Dict[str, Any]):
        super().__init__(question)
        self.question = question
        self.kind = kind            # "rebind_key" | "confirm_join" | …
        self.context = context


# ---------------------------------------------------------------------------
# Output container — matches what main.py persists into the job payload
# ---------------------------------------------------------------------------

@dataclass
class AgentOutput:
    summary: Summary
    matched: List[Dict[str, Any]] = field(default_factory=list)
    unmatched_a: List[Dict[str, Any]] = field(default_factory=list)
    unmatched_b: List[Dict[str, Any]] = field(default_factory=list)
    discrepancies: List[Dict[str, Any]] = field(default_factory=list)
    timing: Optional[Dict[str, Any]] = None
    insights: str = ""
    llm_calls: int = 0
    # v3 / Phase 4 additions
    triage_emitted: List[TriageItem] = field(default_factory=list)
    metrics: Optional[AccountMetrics] = None
    rule_applications: int = 0
    expected_unmatched_a: int = 0
    expected_unmatched_b: int = 0


# ---------------------------------------------------------------------------
# Insights synthesis (LLM only in v3 — no template fallback)
# ---------------------------------------------------------------------------

INSIGHTS_SYSTEM = (
    "You are a senior supply chain operations analyst reviewing a "
    "reconciliation between two systems for a small e-commerce brand. "
    "Be concise, specific, and actionable. Speak in plain English. Always "
    "ground claims in the numbers provided. Output 3 short sections:\n"
    "  1) Overall match quality (1-2 sentences).\n"
    "  2) Top patterns detected (bullet list, max 3, each citing counts/$).\n"
    "  3) Suggested actions (bullet list, max 3, prioritized by $ impact).\n"
    "If a timing section is present in the input, append a one-line timing summary."
)


def _synthesize_insights(
    *, summary: Summary, label_a: str, label_b: str,
    discrepancies: List[Dict[str, Any]], timing: Optional[Dict[str, Any]],
    account: Account, job_id: Optional[str],
) -> str:
    # Compact payload — only stats + top discrepancies
    from collections import Counter
    fee_counter: Counter = Counter()
    for d in discrepancies:
        rat = d.get("rationale") or {}
        for ev in rat.get("rationale", []):
            src = ev.get("source", "")
            if src.startswith(("stripe_fee", "paypal_fee")):
                fee_counter[src] += 1
    top_5 = sorted(discrepancies, key=lambda r: abs(r.get("diff_abs") or 0), reverse=True)[:5]
    payload = {
        "source_a_label": label_a,
        "source_b_label": label_b,
        "summary": summary.model_dump(),
        "timing": timing,
        "fee_pattern_counts": dict(fee_counter),
        "top_5_discrepancies": [
            {"key": d["key"], "amount_a": d["amount_a"], "amount_b": d["amount_b"],
             "diff_abs": d["diff_abs"], "diff_pct": d["diff_pct"]}
            for d in top_5
        ],
    }
    return call_claude(
        tool_name="synthesize_insights",
        account_id=account.id, job_id=job_id,
        system=INSIGHTS_SYSTEM,
        messages=[{"role": "user",
                   "content": "Reconciliation results:\n\n" + json.dumps(payload, indent=2, default=str)}],
        max_tokens=600,
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_job(
    *,
    account: Account,
    df_a: pd.DataFrame,
    df_b: pd.DataFrame,
    cfg: ReconcileConfig,
    job_id: Optional[str] = None,
) -> AgentOutput:
    """Run one reconciliation end-to-end for an account.

    Raises:
        AskUser — if a binding is too low-confidence to proceed safely
        LLMUnavailable — if the API key is missing (insights are required)
        ValueError — if files / bindings are structurally incompatible
    """
    label_a = cfg.label_a
    label_b = cfg.label_b
    tol_abs = account.profile.amount_tolerance_abs or cfg.amount_tolerance_abs
    tol_pct = account.profile.amount_tolerance_pct or cfg.amount_tolerance_pct

    # --- Step 0: load account memory ------------------------------------------
    account_rules = rules_store.load_rules(account.id)
    # Account rules may override the tolerances (Phase 4)
    rule_tol_abs, rule_tol_pct = rules_store.applicable_tolerances(account_rules)
    if rule_tol_abs is not None:
        tol_abs = rule_tol_abs
    if rule_tol_pct is not None:
        tol_pct = rule_tol_pct

    # --- Step 1: resolve roles -------------------------------------------------
    key_a, key_b, key_overlap = pick_key_pair(cfg.source_a, cfg.source_b, df_a, df_b)

    # If both side key bindings have very low confidence AND the overlap is
    # not overwhelming, ask the user to confirm.
    if min(key_a.confidence, key_b.confidence) < ASK_USER_BINDING_THRESHOLD and key_overlap < 0.5:
        raise AskUser(
            question=(
                f"I'm not confident about the join columns. I picked "
                f"'{key_a.column_name}' (Source A) and '{key_b.column_name}' (Source B), "
                f"but I'm only {min(key_a.confidence, key_b.confidence)*100:.0f}% sure. "
                f"Their value overlap is {key_overlap*100:.0f}%. Continue, or pick different columns?"
            ),
            kind="confirm_join",
            context={
                "proposed_a": key_a.column_name,
                "proposed_b": key_b.column_name,
                "overlap": round(key_overlap, 3),
                "alternatives_a": [b.column_name for b in cfg.source_a.bindings if b is not key_a],
                "alternatives_b": [b.column_name for b in cfg.source_b.bindings if b is not key_b],
            },
        )

    amt_a_col, date_a_col = resolve_amount_date(cfg.source_a, df_a)
    amt_b_col, date_b_col = resolve_amount_date(cfg.source_b, df_b)

    # --- Step 2: coerce auxiliary columns -------------------------------------
    a = df_a.copy()
    b = df_b.copy()
    a["_amt"] = coerce_amount(a[amt_a_col]) if amt_a_col else np.nan
    b["_amt"] = coerce_amount(b[amt_b_col]) if amt_b_col else np.nan
    a["_date"] = coerce_date(a[date_a_col]) if date_a_col else pd.NaT
    b["_date"] = coerce_date(b[date_b_col]) if date_b_col else pd.NaT

    # --- Step 3: match ---------------------------------------------------------
    mres = match_by_key(a, b, key_a.column_name, key_b.column_name)

    # --- Step 4: per-row classification ---------------------------------------
    matched_rows: List[Dict[str, Any]] = []
    discrepancy_rows: List[Dict[str, Any]] = []
    deltas_days: List[float] = []
    llm_calls_made = 0
    rule_applications = 0

    for m in mres.matches:
        row_a = a.loc[m.idx_a]
        row_b = b.loc[m.idx_b]
        a_amt = float(row_a["_amt"]) if pd.notna(row_a["_amt"]) else None
        b_amt = float(row_b["_amt"]) if pd.notna(row_b["_amt"]) else None

        if a_amt is not None and b_amt is not None:
            diff_abs = a_amt - b_amt
            denom = abs(a_amt) if a_amt != 0 else 1.0
            diff_pct = diff_abs / denom
        else:
            diff_abs = None
            diff_pct = None

        if pd.notna(row_a["_date"]) and pd.notna(row_b["_date"]):
            delta = (row_b["_date"] - row_a["_date"]).total_seconds() / 86400.0
            deltas_days.append(delta)
        else:
            delta = None

        row_ctx = {
            "key": m.key_a,
            "key_b": m.key_b,
            "match_type": m.match_type,
            "amount_a": a_amt,
            "amount_b": b_amt,
            "diff_abs": round(diff_abs, 4) if diff_abs is not None else None,
            "diff_pct": round(diff_pct * 100, 4) if diff_pct is not None else None,
            "delta_days": round(delta, 2) if delta is not None else None,
            "label_a": label_a,
            "label_b": label_b,
        }

        # Phase 4: account rules get first shot. If a rule fires, the LLM
        # is skipped — the rule's verdict (with its origin trail) becomes
        # the rationale.
        rationale = rules_store.apply_rules_to_matched(account_rules, row_ctx)
        if rationale is not None:
            rule_applications += 1
        else:
            rationale = propose_classification(
                row_ctx=row_ctx,
                tol_abs=tol_abs, tol_pct=tol_pct,
                account_id=account.id, job_id=job_id,
            )
            if any(e.source == "llm_second_opinion" for e in rationale.rationale):
                llm_calls_made += 1

        # Phase 5: user-taught force_status rules win over the initial verdict.
        # They're keyed on the row's signature, which is only knowable now.
        _sig = triage_store.signature_for_matched(rationale, row_ctx)
        _forced = rules_store.apply_force_status_rules(account_rules, _sig, m.key_a)
        if _forced is not None and _forced.status != rationale.status:
            rationale = _forced
            rule_applications += 1

        # Surface the fee-pattern label on the display row (badge convenience)
        fee_pattern_label = None
        for e in rationale.rationale:
            if e.source.startswith(("stripe_fee", "paypal_fee")) and " matches " in e.evidence:
                fee_pattern_label = e.evidence.split(" matches ", 1)[1].split(" on ")[0]
                break

        record = {
            "key": m.key_a,
            "match_type": m.match_type,
            "status": rationale.status,
            "fee_pattern": fee_pattern_label,
            "amount_a": a_amt,
            "amount_b": b_amt,
            "diff_abs": row_ctx["diff_abs"],
            "diff_pct": row_ctx["diff_pct"],
            "date_a": row_a["_date"].isoformat() if pd.notna(row_a["_date"]) else None,
            "date_b": row_b["_date"].isoformat() if pd.notna(row_b["_date"]) else None,
            "delta_days": row_ctx["delta_days"],
            "rationale": rationale.model_dump(),
        }
        matched_rows.append(record)
        if rationale.status != "match":
            discrepancy_rows.append(record)

    # --- Step 5: unmatched rows -----------------------------------------------
    drop_internal = ["_amt", "_date"]

    def _row_dict(row: pd.Series) -> Dict[str, Any]:
        rec = row.drop(labels=drop_internal, errors="ignore").to_dict()
        return {k: (None if (isinstance(v, float) and np.isnan(v)) else v) for k, v in rec.items()}

    unmatched_a_rows = [_row_dict(a.loc[i]) for i in mres.unmatched_a_idx]
    unmatched_b_rows = [_row_dict(b.loc[i]) for i in mres.unmatched_b_idx]

    # Phase 4: split out account-rule-suppressed unmatched rows so they don't
    # crowd the inbox. The unmatched rows are still returned in the result
    # for the audit table, but tagged so the UI can collapse them.
    expected_unmatched_a = 0
    expected_unmatched_b = 0
    for row in unmatched_a_rows:
        key = _first_key_value(row)
        rule = rules_store.is_expected_unmatched(account_rules, "a", str(key))
        if rule is not None:
            row["_expected_by_rule"] = {"rule_id": rule.id, "description": rule.description}
            expected_unmatched_a += 1
    for row in unmatched_b_rows:
        key = _first_key_value(row)
        rule = rules_store.is_expected_unmatched(account_rules, "b", str(key))
        if rule is not None:
            row["_expected_by_rule"] = {"rule_id": rule.id, "description": rule.description}
            expected_unmatched_b += 1

    discrepancy_rows.sort(key=lambda r: abs(r.get("diff_abs") or 0), reverse=True)

    # --- Step 6: timing -------------------------------------------------------
    timing = timing_stats(deltas_days)

    # --- Step 7: summary ------------------------------------------------------
    total_disc_value = sum(abs(r.get("diff_abs") or 0) for r in discrepancy_rows)
    total_amt_a = float(a["_amt"].sum(skipna=True)) if not a["_amt"].isna().all() else 0.0
    total_amt_b = float(b["_amt"].sum(skipna=True)) if not b["_amt"].isna().all() else 0.0

    summary = Summary(
        total_a=int(len(a)),
        total_b=int(len(b)),
        matched=len(matched_rows),
        matched_pct=round(100.0 * len(matched_rows) / max(len(a), 1), 2),
        unmatched_a=len(unmatched_a_rows),
        unmatched_b=len(unmatched_b_rows),
        discrepancies=len(discrepancy_rows),
        fuzzy_matches=mres.fuzzy_count,
        total_discrepancy_value=round(total_disc_value, 2),
        total_amount_a=round(total_amt_a, 2),
        total_amount_b=round(total_amt_b, 2),
    )

    # --- Step 8: insights -----------------------------------------------------
    # LLM-only in v3. If unavailable, this raises and the job fails loudly.
    insights = _synthesize_insights(
        summary=summary, label_a=label_a, label_b=label_b,
        discrepancies=discrepancy_rows, timing=timing,
        account=account, job_id=job_id,
    )
    llm_calls_made += 1

    # --- Step 9: emit TriageItems (cross-job) --------------------------------
    # Don't emit triage for unmatched rows that were suppressed by an active
    # expected_unmatched rule.
    unmatched_a_for_triage = [r for r in unmatched_a_rows if "_expected_by_rule" not in r]
    unmatched_b_for_triage = [r for r in unmatched_b_rows if "_expected_by_rule" not in r]

    emitted = triage_store.emit_for_job(
        account.id, job_id or "unknown-job",
        rationales=matched_rows,
        unmatched_a=unmatched_a_for_triage,
        unmatched_b=unmatched_b_for_triage,
    )

    # --- Step 10: compute + snapshot insight-density metrics -----------------
    job_metrics = metrics_store.compute_density(
        matched_rationales=matched_rows,
        triage_items_emitted=len(emitted),
        account_id=account.id,
        job_id=job_id or "unknown-job",
        llm_calls=llm_calls_made,
    )
    metrics_store.snapshot(account.id, job_metrics)

    # --- Step 11: scan decision log → propose new rules ----------------------
    # Returns pending Rules already persisted to rules.json. Phase 5 UI shows
    # them on /rules for Accept / Edit / Reject.
    rule_proposer.propose_from_decisions(account.id)

    return AgentOutput(
        summary=summary,
        matched=matched_rows,
        unmatched_a=unmatched_a_rows,
        unmatched_b=unmatched_b_rows,
        discrepancies=discrepancy_rows,
        timing=timing,
        insights=insights,
        llm_calls=llm_calls_made,
        triage_emitted=emitted,
        metrics=job_metrics,
        rule_applications=rule_applications,
        expected_unmatched_a=expected_unmatched_a,
        expected_unmatched_b=expected_unmatched_b,
    )
