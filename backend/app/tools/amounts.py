"""Amount coercion, currency detection, and deterministic classification.

Each branch of `classify_amount_diff` returns a structured
(status, confidence, evidence[], alternatives[]) tuple that the agent
turns into a Rationale object.

Fee-pattern knowledge lives in the per-account rules store (seeded at
account creation) — not here — so revoking a fee rule changes verdicts.
"""
from __future__ import annotations

import re
from typing import List, Tuple

import pandas as pd

from ..models import Alt, Evidence


def coerce_amount(s: pd.Series) -> pd.Series:
    if pd.api.types.is_numeric_dtype(s):
        return s.astype(float)
    cleaned = s.astype(str).str.replace(r"[$,€£\s]", "", regex=True)
    cleaned = cleaned.str.replace(r"^\((.+)\)$", r"-\1", regex=True)  # (100) -> -100
    return pd.to_numeric(cleaned, errors="coerce")


_CURRENCY_MAP = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "R$": "BRL"}
_CURRENCY_RE = re.compile(
    r"(R\$|[$€£¥])|\b(USD|EUR|GBP|BRL|CAD|AUD|INR|JPY|MXN)\b", re.IGNORECASE
)


def detect_currency_tokens(s: pd.Series, sample_n: int = 50) -> set:
    """Best-effort currency detection from raw (pre-coercion) amount values.

    Returns a set of ISO codes found in the sample — empty for bare numbers.
    Symbols map to their most common code ($ -> USD); this is a guard against
    silently comparing across currencies, not a converter.
    """
    out = set()
    for v in s.dropna().astype(str).head(sample_n):
        m = _CURRENCY_RE.search(v)
        if m:
            token = (m.group(1) or m.group(2)).upper()
            out.add(_CURRENCY_MAP.get(token, token))
    return out


def classify_amount_diff(
    diff_abs: float, diff_pct: float, a_amt: float, b_amt: float,
    tol_abs: float, tol_pct: float,
    major_abs: float = 100.0, major_pct: float = 0.03,
) -> Tuple[str, float, List[Evidence], List[Alt]]:
    """Deterministic amount classification.

    Returns (status, confidence, evidence, alternatives). All four are the
    shape the agent will wrap into a Rationale.

    Fee-shape detection deliberately does NOT live here: the per-account
    `fee_pattern` rules (seeded at account creation, dispatched by
    rules_store.apply_rules_to_matched *before* this classifier runs) are
    the single source of fee knowledge — so revoking a fee rule actually
    changes verdicts.
    """
    # Within tolerance
    if abs(diff_abs) <= tol_abs or abs(diff_pct) <= tol_pct:
        return ("match", 0.99, [
            Evidence(
                source="tolerance",
                evidence=(f"diff_abs=${diff_abs:.2f} (<= ${tol_abs:.2f}) "
                          f"or |diff_pct|={abs(diff_pct)*100:.2f}% (<= {tol_pct*100:.2f}%)"),
            ),
        ], [])

    # Major / minor thresholds (per-account materiality)
    is_major = abs(diff_pct) >= major_pct or abs(diff_abs) >= major_abs
    if is_major:
        return ("major", 0.90, [
            Evidence(
                source="threshold_major",
                evidence=(f"|diff_abs|=${abs(diff_abs):.2f} >= ${major_abs:.2f} or "
                          f"|diff_pct|={abs(diff_pct)*100:.2f}% >= {major_pct*100:.2f}%"),
            ),
        ], [
            Alt(status="minor", confidence=0.10,
                reason="would be minor if both thresholds were narrowly missed"),
        ])

    return ("minor", 0.70, [
        Evidence(
            source="threshold_minor",
            evidence=(f"diff_abs=${diff_abs:.2f}, diff_pct={diff_pct*100:.2f}% — "
                      "outside tolerance, below major threshold, no fee pattern matched"),
        ),
    ], [
        Alt(status="major", confidence=0.10,
            reason="approaches threshold but did not cross"),
        Alt(status="fee_offset", confidence=0.10,
            reason="no known fee pattern matched but A > B"),
    ])
