"""Persists an `AgentOutput` onto its job row.

Extracted verbatim from `main.py`'s `_run_job_background` (Task 10): the
upload background task and the `run_reconciliation` macro-tool both call
`agent.run_job(...)` themselves and hand the result here, so there is exactly
one place that writes a job's result payload regardless of which path
produced it.

The macro-tool calls this for a `job_id` that has no row yet (the upload
path always creates one via `storage.save_job` before this runs) — the
`account_id` parameter exists only to create that row on demand. When a row
already exists this is a no-op and behaviour is identical to the code this
was extracted from.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from .. import storage


def _clean(obj):
    """Replace NaN/Inf with None so JSON serialization stays valid."""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    return obj


def persist_agent_output(*, job_id: str, account_id: str, output: Any) -> None:
    if storage.load_job(job_id) is None:
        storage.save_job(job_id, {
            "job_id": job_id,
            "account_id": account_id,
            "created_at": datetime.utcnow().isoformat() + "Z",
            "status": "processing",
        })

    result = output
    fields = _clean({
        "status": "complete",
        "summary": result.summary.model_dump(),
        "matched": result.matched,
        "unmatched_a": result.unmatched_a,
        "unmatched_b": result.unmatched_b,
        "discrepancies": result.discrepancies,
        "timing": result.timing,
        "insights": result.insights,
        "insights_status": result.insights_status,
        "llm_calls": result.llm_calls,
        "metrics": result.metrics.model_dump(mode="json") if result.metrics else None,
        "triage_emitted_count": len(result.triage_emitted),
        "rule_applications": result.rule_applications,
        "expected_unmatched_a": result.expected_unmatched_a,
        "expected_unmatched_b": result.expected_unmatched_b,
        "binding_warning": result.binding_warning,
        "key_col_a": result.key_col_a,
        "key_col_b": result.key_col_b,
    })
    storage.update_job(job_id, **fields)
