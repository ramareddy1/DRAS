"""Tier-1 tools: handle-based wrappers over the deterministic tools.

Every function takes references and returns a bounded summary (spec 2.1).
Row data never crosses the model boundary, with one deliberate exception:
capped, truncated sample values, which concept induction needs.

Account scope comes from `context.current_run()`, never from a parameter.
"""
from __future__ import annotations

from typing import Any, Dict, List

import pandas as pd

from ..tools import binding as binding_tool
from ..tools import matching as matching_tool
from . import artifacts
from .context import current_run
from .registry import Effect, register

MAX_SAMPLES = 3
MAX_SAMPLE_CHARS = 32
MAX_UNBOUND_REPORTED = 25


def _samples(series: pd.Series) -> List[str]:
    out: List[str] = []
    for value in series.dropna().unique()[:MAX_SAMPLES]:
        out.append(str(value)[:MAX_SAMPLE_CHARS])
    return out


@register(Effect.read)
def profile_schema(dataset_id: str) -> Dict[str, Any]:
    """Fingerprint a dataset's columns: dtype, null rate, cardinality, samples.

    Call this first on any dataset you have not seen, before bind_columns.
    Returns bounded per-column statistics — never the underlying rows.

    Args:
        dataset_id: Handle returned when the dataset was loaded.
    """
    ctx = current_run()
    df = artifacts.get_dataset(dataset_id=dataset_id, account_id=ctx.account_id)

    columns: List[Dict[str, Any]] = []
    for name in df.columns:
        series = df[name]
        columns.append({
            "name": str(name),
            "dtype": str(series.dtype),
            "null_rate": round(float(series.isna().mean()), 4),
            "cardinality": int(series.nunique(dropna=True)),
            "samples": _samples(series),
        })

    return {
        "dataset_id": dataset_id,
        "row_count": int(len(df)),
        "columns": columns,
    }


@register(Effect.read)
def bind_columns(dataset_id: str) -> Dict[str, Any]:
    """Map a dataset's columns onto ontology concepts.

    Call this after profile_schema on any newly loaded dataset, and before
    matching or classifying. Returns which columns bound and which did not.

    Args:
        dataset_id: Handle returned when the dataset was loaded.
    """
    ctx = current_run()
    df = artifacts.get_dataset(dataset_id=dataset_id, account_id=ctx.account_id)

    bindings = binding_tool.bind_columns(df, account_id=ctx.account_id)
    bound_names = {b.column_name for b in bindings}
    unbound = [str(c) for c in df.columns if str(c) not in bound_names]

    return {
        "dataset_id": dataset_id,
        "total_count": int(len(df.columns)),
        "bound_count": len(bound_names),
        "mappings": [
            {
                "column": b.column_name,
                "concept": b.concept_id,
                "confidence": round(float(b.confidence), 3),
            }
            for b in bindings
        ],
        "unbound": unbound[:MAX_UNBOUND_REPORTED],
        "unbound_truncated": len(unbound) > MAX_UNBOUND_REPORTED,
    }


@register(Effect.read)
def match_datasets(
    dataset_a_id: str,
    dataset_b_id: str,
    key_a_column: str,
    key_b_column: str,
) -> Dict[str, Any]:
    """Join two datasets on a key column, exact first then fuzzy.

    Call this once both datasets are bound and you have chosen a key column
    on each side. Returns counts only — use the run's artifacts to inspect
    individual rows.

    Args:
        dataset_a_id: Handle for the left dataset.
        dataset_b_id: Handle for the right dataset.
        key_a_column: Column in the left dataset to join on.
        key_b_column: Column in the right dataset to join on.
    """
    ctx = current_run()
    df_a = artifacts.get_dataset(dataset_id=dataset_a_id, account_id=ctx.account_id)
    df_b = artifacts.get_dataset(dataset_id=dataset_b_id, account_id=ctx.account_id)

    result = matching_tool.match_by_key(df_a, df_b, key_a_column, key_b_column)

    return {
        "rows_in_a": int(len(df_a)),
        "rows_in_b": int(len(df_b)),
        "matched": len(result.matches),
        "unmatched_a": len(result.unmatched_a_idx),
        "unmatched_b": len(result.unmatched_b_idx),
        "fuzzy_count": int(result.fuzzy_count),
    }
