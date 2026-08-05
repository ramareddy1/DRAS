"""The classic reconciliation pipeline, exposed as one registered tool.

`app/agent.py` is called, never reimplemented. That is what makes the
migration zero-regression and gives the planner a fast path for the goal it
handles best (spec 1.3).
"""
from __future__ import annotations

import uuid
from typing import Any, Dict

from ..agent import run_job
from ..memory import accounts as accounts_memory
from ..models import BindingSet, ReconcileConfig
from ..tools import binding as binding_tool
from . import artifacts
from .context import current_run
from .job_persist import persist_agent_output
from .registry import Effect, register


@register(Effect.write)
def run_reconciliation(
    dataset_a_id: str,
    dataset_b_id: str,
    label_a: str,
    label_b: str,
) -> Dict[str, Any]:
    """Reconcile two datasets end-to-end: bind, match, compare, classify.

    Call this when the goal is the classic two-source reconciliation — "did
    these orders get paid", "match this export against that one". It runs
    the full deterministic pipeline in one step and returns counts plus a
    job id for the detailed result.

    Args:
        dataset_a_id: Handle for the left dataset (e.g. orders).
        dataset_b_id: Handle for the right dataset (e.g. payments).
        label_a: Human-readable name for the left source.
        label_b: Human-readable name for the right source.
    """
    ctx = current_run()
    account = accounts_memory.load_account(ctx.account_id)
    if account is None:
        raise KeyError(f"account {ctx.account_id} not found")

    df_a = artifacts.get_dataset(dataset_id=dataset_a_id, account_id=ctx.account_id)
    df_b = artifacts.get_dataset(dataset_id=dataset_b_id, account_id=ctx.account_id)

    cfg = ReconcileConfig(
        source_a=BindingSet(
            bindings=binding_tool.bind_columns(df_a, account_id=ctx.account_id),
        ),
        source_b=BindingSet(
            bindings=binding_tool.bind_columns(df_b, account_id=ctx.account_id),
        ),
        label_a=label_a,
        label_b=label_b,
    )

    job_id = str(uuid.uuid4())
    output = run_job(
        account=account, df_a=df_a, df_b=df_b, cfg=cfg, job_id=job_id,
    )
    persist_agent_output(
        job_id=job_id, account_id=ctx.account_id, output=output,
    )

    return {
        "job_id": job_id,
        "matched": len(output.matched),
        "unmatched_a": len(output.unmatched_a),
        "unmatched_b": len(output.unmatched_b),
        "discrepancies": len(output.discrepancies),
        "triage_emitted": len(output.triage_emitted),
        "rule_applications": int(output.rule_applications),
        "insights_status": output.insights_status,
    }
