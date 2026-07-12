"""Per-row classification — deterministic + LLM escalation on ambiguity.

The agent (Phase 3) calls `propose_classification` for every matched row.
Most rows get a deterministic verdict from `classify_amount_diff` with high
confidence. Rows whose deterministic confidence is below the escalation
threshold get an LLM second-opinion that can adjust the status, refine the
confidence, and add a human-readable Evidence line.

The deterministic Evidence is always preserved; the LLM contributes
*additional* Evidence rather than replacing it. That keeps the audit trail
intact even when the LLM is unavailable mid-job.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ..llm import LLMUnavailable, call_claude_json, is_configured
from ..models import Alt, Evidence, Rationale
from .amounts import classify_amount_diff


ESCALATION_THRESHOLD = 0.75   # below this, ask the LLM for a second opinion


SYSTEM_PROMPT = (
    "You are a senior supply chain operations analyst classifying reconciliation "
    "diffs between two systems for a small e-commerce brand. You are given a "
    "single matched row with the deterministic classifier's current verdict and "
    "evidence. Your job is to either (a) confirm the verdict, or (b) recommend "
    "a different status from {match, minor, major, fee_offset} with a clear, "
    "one-sentence reason. Always respond as JSON with this exact shape:\n"
    "{\n"
    '  "status": "match"|"minor"|"major"|"fee_offset",\n'
    '  "confidence": 0.0..1.0,\n'
    '  "reason": "one short sentence citing the numbers"\n'
    "}\n"
    "Do NOT add any other text. Cite the actual diff amounts and percentages "
    "in the reason. Be conservative — only override the deterministic verdict "
    "when there is a clear reason in the row's context."
)


def _row_context_for_llm(row_ctx: Dict[str, Any]) -> str:
    """Compact payload for the LLM — only the numbers and labels that matter."""
    return json.dumps({
        "key": row_ctx.get("key"),
        "amount_a": row_ctx.get("amount_a"),
        "amount_b": row_ctx.get("amount_b"),
        "diff_abs": row_ctx.get("diff_abs"),
        "diff_pct": row_ctx.get("diff_pct"),
        "match_type": row_ctx.get("match_type"),
        "delta_days": row_ctx.get("delta_days"),
        "current_verdict": {
            "status": row_ctx.get("status"),
            "confidence": row_ctx.get("_det_confidence"),
            "evidence": row_ctx.get("_det_evidence"),
        },
        "label_a": row_ctx.get("label_a"),
        "label_b": row_ctx.get("label_b"),
    }, default=str)


def propose_classification(
    *,
    row_ctx: Dict[str, Any],
    tol_abs: float,
    tol_pct: float,
    account_id: str,
    job_id: Optional[str],
    allow_llm: bool = True,
) -> Rationale:
    """Return a Rationale for one matched row.

    `row_ctx` must contain: key, amount_a, amount_b, diff_abs, diff_pct,
    match_type, optionally delta_days, label_a, label_b.

    If amounts are present and deterministic confidence is below the
    escalation threshold, ask Claude for a second opinion. LLM unavailable
    or errors → deterministic verdict stands (with a note in evidence).
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

    # Escalate if below threshold and the LLM is available
    if allow_llm and confidence < ESCALATION_THRESHOLD and is_configured():
        try:
            ctx_for_llm = {**row_ctx,
                           "_det_confidence": confidence,
                           "_det_evidence": [e.evidence for e in evidence]}
            data = call_claude_json(
                tool_name="propose_classification",
                account_id=account_id, job_id=job_id,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": _row_context_for_llm(ctx_for_llm)}],
                max_tokens=200,
            )
            llm_status = data.get("status")
            llm_conf = float(data.get("confidence", 0.6))
            llm_reason = (data.get("reason") or "").strip()
            if llm_status in ("match", "minor", "major", "fee_offset") and llm_reason:
                # Record the LLM's evidence even when it agrees — keeps audit trail honest
                evidence = evidence + [Evidence(
                    source="llm_second_opinion",
                    evidence=f"{llm_reason} (confidence {llm_conf:.2f})",
                    weight=0.5,
                )]
                if llm_status != status:
                    # LLM disagreed — promote its verdict, demote the deterministic one
                    alts = [Alt(
                        status=status, confidence=confidence,
                        reason=f"deterministic classifier said {status} ({confidence:.2f})",
                    )] + alts
                    status = llm_status
                    confidence = min(0.99, max(confidence, llm_conf))
                else:
                    # LLM agreed — slight confidence bump
                    confidence = min(0.99, (confidence + llm_conf) / 2 + 0.05)
        except LLMUnavailable:
            pass
        except Exception as e:
            evidence = evidence + [Evidence(
                source="llm_error",
                evidence=f"LLM second-opinion failed ({type(e).__name__}); deterministic verdict stands",
                weight=0.0,
            )]

    return Rationale(
        row_key=key,
        status=status,
        confidence=round(confidence, 3),
        rationale=evidence,
        alternatives=alts,
    )
