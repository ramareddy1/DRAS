"""Key matching: exact pass + fuzzy pass.

Returns a structure the agent can iterate over without re-walking the
DataFrames. Pure function — no state.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd


_KEY_PREFIX_RE = re.compile(
    r"^(#|ord[-_]|order[-_]|inv[-_]|invoice[-_]|po[-_]|pi[-_]|ch[-_])",
    re.IGNORECASE,
)


def norm_key(v: Any) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return ""
    s = str(v).strip()
    s = _KEY_PREFIX_RE.sub("", s)
    return s.lower().lstrip("0") or s.lower()


@dataclass
class Match:
    idx_a: int
    idx_b: int
    key_a: str
    key_b: str
    match_type: str  # "exact" | "fuzzy"


@dataclass
class MatchResult:
    matches: List[Match]
    unmatched_a_idx: List[int]
    unmatched_b_idx: List[int]
    fuzzy_count: int


@dataclass
class AggregationInfo:
    groups: int          # normalized keys that had >1 row
    rows_collapsed: int  # extra rows folded into their group's first row


def aggregate_duplicate_keys(df: pd.DataFrame, key_col: str):
    """Collapse rows sharing a normalized key into one row per key.

    Real exports are many-to-one — an order paid by several charges or
    vouchers, split shipments against one PO. `_amt` is summed across the
    group (min_count=1, so all-missing stays missing); every other field
    keeps the first row's value; `_agg_count` records the group size.

    Returns (df, AggregationInfo) — or (df, None) untouched when every key
    is already unique. Singleton rows keep their original `_amt`, including
    NaN, so the amounts_missing path still fires for them.
    """
    norm = df[key_col].astype(str).str.strip().map(norm_key)
    sizes = norm.map(norm.value_counts())
    if (sizes <= 1).all():
        return df, None

    work = df.copy()
    work["_norm_key"] = norm
    work["_agg_count"] = sizes.astype(int).values
    group_sums = work.groupby("_norm_key")["_amt"].sum(min_count=1)
    first_mask = ~work["_norm_key"].duplicated(keep="first")
    dup_first = first_mask & (work["_agg_count"] > 1)
    work.loc[dup_first, "_amt"] = work.loc[dup_first, "_norm_key"].map(group_sums)

    out = work[first_mask].drop(columns=["_norm_key"])
    return out, AggregationInfo(
        groups=int((work.loc[first_mask, "_agg_count"] > 1).sum()),
        rows_collapsed=int((~first_mask).sum()),
    )


def match_by_key(
    df_a: pd.DataFrame, df_b: pd.DataFrame,
    key_a_col: str, key_b_col: str,
) -> MatchResult:
    """Two-pass match. Exact on trimmed string; fuzzy on normalized form.

    Each B row can only match one A row (first wins). A rows that find no B
    partner are unmatched; B rows never touched are unmatched too.
    """
    a_keys_exact = df_a[key_a_col].astype(str).str.strip()
    b_keys_exact = df_b[key_b_col].astype(str).str.strip()
    a_keys_norm = a_keys_exact.map(norm_key)
    b_keys_norm = b_keys_exact.map(norm_key)

    b_by_exact: Dict[str, int] = {}
    b_by_norm: Dict[str, int] = {}
    for idx, k in b_keys_exact.items():
        b_by_exact.setdefault(k, idx)
    for idx, k in b_keys_norm.items():
        b_by_norm.setdefault(k, idx)

    matches: List[Match] = []
    used_b: set = set()
    fuzzy_count = 0
    a_participated: set = set()

    for idx_a in df_a.index:
        ke = a_keys_exact.loc[idx_a]
        kn = a_keys_norm.loc[idx_a]

        b_idx = b_by_exact.get(ke)
        match_type = "exact"
        if b_idx is None or b_idx in used_b:
            b_idx = b_by_norm.get(kn)
            if b_idx is not None and b_idx not in used_b:
                match_type = "fuzzy"
                fuzzy_count += 1
            else:
                b_idx = None

        if b_idx is None:
            continue

        used_b.add(b_idx)
        a_participated.add(idx_a)
        matches.append(Match(
            idx_a=idx_a, idx_b=b_idx,
            key_a=ke, key_b=b_keys_exact.loc[b_idx],
            match_type=match_type,
        ))

    unmatched_a = [i for i in df_a.index if i not in a_participated]
    unmatched_b = [i for i in df_b.index if i not in used_b]
    return MatchResult(
        matches=matches,
        unmatched_a_idx=unmatched_a,
        unmatched_b_idx=unmatched_b,
        fuzzy_count=fuzzy_count,
    )
