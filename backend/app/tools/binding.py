"""Bind columns of an arbitrary DataFrame to concepts in the ontology.

Three signals combine into a confidence score per (column, concept) pair:

  1. ALIAS_HIT   — normalized column name is a known alias of the concept.
                   This is the strongest signal: weight ~0.55-0.85.
  2. NAME_CONTAINS — column name contains an alias as a substring after
                     normalization (catches "Order Total ($)" → order.gross_total).
                     Weight ~0.30.
  3. VALUE_SHAPE — values in the column match the concept's value_hints
                   (regex, numeric range, parseable as datetime). Weight ~0.25.

We sum the contributions and clamp to [0, 0.99] (we never claim 1.0 without
explicit user confirmation; that's the job of provenance='user_confirmed').

Top-3 alternative concepts are recorded per binding so the UI can show
"did you mean ...?" options without re-running inference.

Notes
-----
- Phase 1 has no per-brand learned aliases; the embedding-index boost
  comes in Phase 4.
- An LLM-based binder is intentionally NOT used here. We want this layer fast,
  cheap, and deterministic. The agent (Phase 3) only calls the LLM if these
  rule-based signals leave a column with low confidence.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from ..ontology import CONCEPTS, ALIAS_INDEX, Concept, _norm_alias, concept_by_id
from ..models import BindingSet, SemanticBinding


# --- weights -----------------------------------------------------------------
W_ALIAS_EXACT = 0.70
W_ALIAS_SELF  = 0.85   # column is named exactly with a concept id, e.g. "order.id"
W_NAME_CONTAINS = 0.30
W_VALUE_SHAPE = 0.25
MIN_CONFIDENCE_TO_REPORT = 0.20


def _column_alias_hits(column: str) -> List[Tuple[str, float]]:
    """Return [(concept_id, weight)] for alias signals from the column name."""
    norm = _norm_alias(column)
    hits: List[Tuple[str, float]] = []

    # exact alias hit (full normalized name == known alias)
    if norm in ALIAS_INDEX:
        cid = ALIAS_INDEX[norm]
        # is the column name *itself* a concept id?
        weight = W_ALIAS_SELF if _norm_alias(cid) == norm else W_ALIAS_EXACT
        hits.append((cid, weight))

    # contains-style hit: any alias appears as a substring of the normalized column
    for alias, cid in ALIAS_INDEX.items():
        if len(alias) < 3:
            continue
        if alias == norm:
            continue  # already captured above
        if alias in norm:
            hits.append((cid, W_NAME_CONTAINS))

    return hits


_DATETIME_PATTERNS = [
    re.compile(r"^\d{4}-\d{1,2}-\d{1,2}([ T]\d{1,2}:\d{2}(:\d{2})?)?(Z|[+-]\d{2}:?\d{2})?$"),
    re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}( \d{1,2}:\d{2}(:\d{2})?)?$"),
    re.compile(r"^\d{1,2}-[A-Za-z]{3}-\d{2,4}$"),
]


def _value_shape_score(series: pd.Series, concept: Concept) -> Tuple[float, List[str]]:
    """Return (shape_score, evidence_lines) given the concept's value hints."""
    hints = concept.value_hints
    if hints is None:
        return 0.0, []

    sample = series.dropna().astype(str).head(20).tolist()
    if not sample:
        return 0.0, []

    evidence: List[str] = []
    score = 0.0

    # regex check
    if hints.regex:
        rx = re.compile(hints.regex)
        ok = sum(1 for v in sample if rx.match(v))
        ratio = ok / len(sample)
        if ratio >= 0.7:
            score += W_VALUE_SHAPE
            evidence.append(f"{int(ratio*100)}% of values match expected pattern")

    # datetime check
    if hints.datetime:
        ok = sum(1 for v in sample if any(p.match(v) for p in _DATETIME_PATTERNS))
        if ok / len(sample) >= 0.7:
            score += W_VALUE_SHAPE
            evidence.append(f"{int(100*ok/len(sample))}% of values parse as dates")
        else:
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                parsed = pd.to_datetime(series.head(20), errors="coerce")
            if parsed.notna().mean() >= 0.7:
                score += W_VALUE_SHAPE * 0.8
                evidence.append("values parse as datetimes via pandas")

    # numeric range check
    if hints.numeric_range:
        lo, hi = hints.numeric_range
        nums = pd.to_numeric(series.head(50), errors="coerce").dropna()
        if len(nums) >= max(3, 0.5 * min(50, len(series))):
            in_range = ((nums >= lo) & (nums <= hi)).mean()
            if in_range >= 0.8:
                score += W_VALUE_SHAPE
                evidence.append(f"values numeric and {int(in_range*100)}% within [{lo}, {hi}]")

    return min(score, W_VALUE_SHAPE * 1.5), evidence


def _column_value_signal(series: pd.Series) -> Dict[str, Tuple[float, List[str]]]:
    """For one column, score it against every concept that has value_hints.
    Returns {concept_id: (shape_score, evidence)}.
    """
    out: Dict[str, Tuple[float, List[str]]] = {}
    for cid, concept in CONCEPTS.items():
        if concept.value_hints is None:
            continue
        score, evidence = _value_shape_score(series, concept)
        if score > 0:
            out[cid] = (score, evidence)
    return out


def bind_columns(df: pd.DataFrame, account_id: Optional[str] = None) -> List[SemanticBinding]:
    """Infer SemanticBindings for every column of `df`.

    When `account_id` is supplied, account-scoped memory takes precedence:
      1. Account learned aliases (highest priority — what the user confirmed before)
      2. Account column-embedding index (Phase 4 stub for now)
      3. Global ontology aliases + value-shape heuristics (Phase 1 logic)
    """
    # Lazy import to avoid a circular dep with the memory package
    learned_lookup = None
    if account_id:
        from ..memory import learned_aliases as la
        learned_lookup = lambda col: la.lookup(account_id, col)

    bindings: List[SemanticBinding] = []

    for col in df.columns:
        # 0. Account-learned alias short-circuit
        if learned_lookup:
            learned = learned_lookup(col)
            if learned and learned in CONCEPTS:
                bindings.append(SemanticBinding(
                    column_name=col,
                    concept_id=learned,
                    confidence=0.99,
                    provenance="user_confirmed",
                    evidence=["you confirmed this binding before on this account"],
                    alternatives=[],
                ))
                continue

        scores: Dict[str, float] = {}
        evidence_by_concept: Dict[str, List[str]] = {}

        # 1. name-based signals (keep the *strongest* signal per concept)
        best_name_signal: Dict[str, float] = {}
        for cid, weight in _column_alias_hits(col):
            best_name_signal[cid] = max(best_name_signal.get(cid, 0.0), weight)
        for cid, weight in best_name_signal.items():
            scores[cid] = scores.get(cid, 0.0) + weight
            evidence_by_concept.setdefault(cid, []).append(
                "column name matches concept alias" if weight >= W_ALIAS_EXACT
                else "column name contains concept alias"
            )

        # 2. value-shape signals
        for cid, (vscore, vev) in _column_value_signal(df[col]).items():
            scores[cid] = scores.get(cid, 0.0) + vscore
            evidence_by_concept.setdefault(cid, []).extend(vev)

        if not scores:
            continue

        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        top_cid, top_score = ranked[0]
        # clamp to [0, 0.99]; user confirmation is what gets you to 1.0
        confidence = min(top_score, 0.99)

        if confidence < MIN_CONFIDENCE_TO_REPORT:
            continue

        alternatives = [
            {"concept_id": cid, "confidence": round(min(s, 0.99), 3),
             "reason": "; ".join(evidence_by_concept.get(cid, [])[:2]) or "lower-scoring candidate"}
            for cid, s in ranked[1:4]
        ]

        bindings.append(SemanticBinding(
            column_name=col,
            concept_id=top_cid,
            confidence=round(confidence, 3),
            provenance="inferred",
            evidence=evidence_by_concept.get(top_cid, []),
            alternatives=alternatives,
        ))

    return bindings


# ---------------------------------------------------------------------------
# Role resolution — picking concrete column names for the reconciliation engine
# from a BindingSet.
# ---------------------------------------------------------------------------


def _value_overlap(a: pd.Series, b: pd.Series) -> float:
    """Fraction of normalized non-null A values that also appear in B."""
    from .matching import norm_key
    a_vals = {norm_key(v) for v in a.dropna().astype(str)} - {""}
    b_vals = {norm_key(v) for v in b.dropna().astype(str)} - {""}
    if not a_vals or not b_vals:
        return 0.0
    return len(a_vals & b_vals) / len(a_vals)


def _candidate_keys(bindings: BindingSet, df: pd.DataFrame) -> List[SemanticBinding]:
    """All primary_key bindings whose column exists in df."""
    return [
        b for b in bindings.bindings
        if b.column_name in df.columns
        and (c := concept_by_id(b.concept_id)) is not None
        and c.role == "primary_key"
    ]


def pick_key_pair(
    a_bindings: BindingSet, b_bindings: BindingSet,
    df_a: pd.DataFrame, df_b: pd.DataFrame,
) -> Tuple[SemanticBinding, SemanticBinding, float]:
    """Choose the (A.key, B.key) pair with the highest value overlap.

    When either side has multiple primary_key candidates (e.g. a payments
    file with both txn_id and order_reference), the right one to join on is
    whichever pair actually shares values. Returns (key_a, key_b, overlap).

    Raises ValueError if there's no candidate on either side, or if no pair
    shares enough values to be a credible join.
    """
    keys_a = _candidate_keys(a_bindings, df_a)
    keys_b = _candidate_keys(b_bindings, df_b)
    if not keys_a:
        raise ValueError(
            f"No primary_key binding for Source A. "
            f"Available columns: {list(df_a.columns)}. "
            f"Bind one column to a *.id concept (e.g. order.id, sku.id, payment.txn_id)."
        )
    if not keys_b:
        raise ValueError(
            f"No primary_key binding for Source B. "
            f"Available columns: {list(df_b.columns)}."
        )

    best: Optional[Tuple[SemanticBinding, SemanticBinding, tuple]] = None
    best_overlap = -1.0
    for ka in keys_a:
        for kb in keys_b:
            overlap = _value_overlap(df_a[ka.column_name], df_b[kb.column_name])
            score = (overlap, ka.confidence + kb.confidence)
            if best is None or score > best[2]:
                best = (ka, kb, score)
                best_overlap = overlap

    ka, kb, _ = best
    if best_overlap < 0.05:
        raise ValueError(
            f"Couldn't find a key pair with overlapping values. "
            f"Tried {len(keys_a)*len(keys_b)} candidate pairs; "
            f"max overlap was {best_overlap*100:.1f}%. "
            f"Source A candidates: {[k.column_name for k in keys_a]}; "
            f"Source B candidates: {[k.column_name for k in keys_b]}."
        )
    return ka, kb, best_overlap


def resolve_amount_date(bindings: BindingSet, df: pd.DataFrame) -> Tuple[Optional[str], Optional[str]]:
    """Resolve the (amount_column, date_column) names for one side."""
    amt_b = bindings.by_role("primary_amount")
    date_b = bindings.by_role("event_time")
    amt_col = amt_b.column_name if amt_b and amt_b.column_name in df.columns else None
    date_col = date_b.column_name if date_b and date_b.column_name in df.columns else None
    return amt_col, date_col
