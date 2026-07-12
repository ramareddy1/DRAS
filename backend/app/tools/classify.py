"""Per-row classification — deterministic verdicts + batched advisory review.

The agent calls `propose_classification` for every matched row. Verdicts are
purely deterministic (`classify_amount_diff`) — the LLM never flips a status,
so the same file always classifies the same way.

Ambiguous discrepancies (status != match, confidence below the escalation
threshold) are collected by the agent and sent to `batch_second_opinions` in
ONE capped LLM call, ranked by $ impact. The reviewer contributes *advisory*
Evidence lines on top of the deterministic ones — the audit trail keeps both,
and an LLM failure can never fail or change the job.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from ..llm import call_claude_json, is_configured
from ..models import Evidence, Rationale
from .amounts import classify_amount_diff


ESCALATION_THRESHOLD = 0.75   # below this, a discrepancy row gets AI review


BATCH_SYSTEM_PROMPT = (
    "You are a senior operations analyst reviewing reconciliation discrepancies "
    "between two systems for a small e-commerce brand. You receive a JSON array "
    "of rows, each with the deterministic classifier's verdict and the numbers. "
    "For EACH row return an object:\n"
    '{"key": <same key>, "agrees": true|false, '
    '"note": "one short sentence citing the numbers", "confidence": 0.0..1.0}\n'
    "Respond with a JSON array only, same order as the input. No other text."
)


def propose_classification(
    *,
    row_ctx: Dict[str, Any],
    tol_abs: float,
    tol_pct: float,
    account_id: str,
    job_id: Optional[str],
    allow_llm: bool = True,
) -> Rationale:
    """Return a deterministic Rationale for one matched row.

    `row_ctx` must contain: key, amount_a, amount_b, diff_abs, diff_pct,
    match_type, optionally delta_days, label_a, label_b.

    `allow_llm` is kept for signature stability; the per-row LLM escalation
    was replaced by the agent-level `batch_second_opinions` pass.
    """
    key = row_ctx["key"]
    a_amt = row_ctx.get("amount_a")
    b_amt = row_ctx.get("amount_b")

    if a_amt is None or b_amt is None:
        return Rationale(
            row_key=key,
            status="match",
            confidence=0.5,
            rationale=[Evidence(
                source="amounts_missing",
                evidence="one or both amount columns were null; status is a default, not a comparison",
            )],
            alternatives=[],
        )

    diff_abs = row_ctx["diff_abs"] or (a_amt - b_amt)
    diff_pct_pct = row_ctx.get("diff_pct")
    diff_pct = (diff_pct_pct / 100.0) if diff_pct_pct is not None else (diff_abs / (abs(a_amt) or 1.0))

    status, confidence, evidence, alts = classify_amount_diff(
        diff_abs, diff_pct, a_amt, b_amt, tol_abs, tol_pct,
    )

    if row_ctx.get("match_type") == "fuzzy":
        evidence = [Evidence(
            source="fuzzy_key_match",
            evidence=(f"keys '{key}' ~ '{row_ctx.get('key_b', key)}' matched after "
                      "prefix/case normalization"),
            weight=0.2,
        )] + evidence

    return Rationale(
        row_key=key,
        status=status,
        confidence=round(confidence, 3),
        rationale=evidence,
        alternatives=alts,
    )


def batch_second_opinions(
    *,
    candidates: List[Dict[str, Any]],   # [{"rationale": Rationale, "row_ctx": {...}}]
    account_id: str,
    job_id: Optional[str],
) -> Dict[str, Dict[str, Any]]:
    """One LLM call reviewing the top-N candidates by |diff_abs|.

    Appends advisory Evidence to each reviewed candidate's Rationale IN PLACE
    (never flips status — determinism is the product guarantee). Returns
    {row_key: llm_item} for the rows actually reviewed.
    """
    max_rows = int(os.getenv("RECONOPS_MAX_LLM_ROWS", "25"))
    row_model = os.getenv("ANTHROPIC_ROW_MODEL", "claude-haiku-4-5")
    if not candidates or max_rows <= 0 or not is_configured():
        return {}

    ranked = sorted(candidates,
                    key=lambda c: abs(c["row_ctx"].get("diff_abs") or 0),
                    reverse=True)[:max_rows]
    payload = [{
        "key": c["row_ctx"].get("key"),
        "amount_a": c["row_ctx"].get("amount_a"),
        "amount_b": c["row_ctx"].get("amount_b"),
        "diff_abs": c["row_ctx"].get("diff_abs"),
        "diff_pct": c["row_ctx"].get("diff_pct"),
        "match_type": c["row_ctx"].get("match_type"),
        "verdict": {"status": c["rationale"].status,
                    "confidence": c["rationale"].confidence,
                    "evidence": [e.evidence for e in c["rationale"].rationale]},
    } for c in ranked]

    try:
        data = call_claude_json(
            tool_name="batch_second_opinions",
            account_id=account_id, job_id=job_id,
            system=BATCH_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(payload, default=str)}],
            max_tokens=120 * len(ranked),
            model=row_model,
        )
    except Exception:
        return {}  # advisory pass — failure must never fail the job

    if not isinstance(data, list):
        return {}
    by_key = {str(item.get("key")): item for item in data if isinstance(item, dict)}
    reviewed: Dict[str, Dict[str, Any]] = {}
    for c in ranked:
        key = str(c["row_ctx"].get("key"))
        item = by_key.get(key)
        if not item or not (item.get("note") or "").strip():
            continue
        verb = "confirms" if item.get("agrees", True) else "questions"
        c["rationale"].rationale.append(Evidence(
            source="llm_second_opinion",
            evidence=f"AI review {verb} the verdict: {item['note'].strip()} "
                     f"(confidence {float(item.get('confidence', 0.6)):.2f})",
            weight=0.3,
        ))
        reviewed[key] = item
    return reviewed
