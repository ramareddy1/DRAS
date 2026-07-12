"""Amount coercion, classification, and fee-pattern detection.

In v3 each branch of `classify_amount_diff` returns a structured
(status, confidence, evidence[], alternatives[]) tuple that the agent
turns into a Rationale object.

`FEE_PATTERNS` is a list of (rule_id, label, predicate) triples. Phase 4
migrates these into the per-account rules.json — same shape, different
storage.
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


# (rule_id, human_label, predicate(a, b) -> bool)
FEE_PATTERNS: List[Tuple[str, str, callable]] = [
    ("stripe_fee_2.9_0.30", "Stripe (2.9% + $0.30)",
     lambda a, b: abs((a - b) - (a * 0.029 + 0.30)) < max(0.02, a * 0.001)),
    ("paypal_fee_2.99",     "PayPal (2.99%)",
     lambda a, b: abs((a - b) - (a * 0.0299)) < max(0.02, a * 0.001)),
    ("paypal_fee_3.49_0.49", "PayPal (3.49% + $0.49)",
     lambda a, b: abs((a - b) - (a * 0.0349 + 0.49)) < max(0.02, a * 0.001)),
]


def classify_amount_diff(
    diff_abs: float, diff_pct: float, a_amt: float, b_amt: float,
    tol_abs: float, tol_pct: float,
) -> Tuple[str, float, List[Evidence], List[Alt]]:
    """Deterministic amount classification.

    Returns (status, confidence, evidence, alternatives). All four are the
    shape the agent will wrap into a Rationale.
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

    # Fee patterns (A > B, processor took a cut)
    if a_amt > b_amt and a_amt > 0:
        for rule_id, label, fn in FEE_PATTERNS:
            try:
                if fn(a_amt, b_amt):
                    return ("fee_offset", 0.95, [
                        Evidence(
                            source=rule_id,
                            evidence=(f"diff_abs=${diff_abs:.2f} matches {label} "
                                      f"on amount=${a_amt:.2f} -> expected fee "
                                      f"~${(a_amt - b_amt):.2f}"),
                        ),
                    ], [
                        Alt(
                            status=("major" if (abs(diff_pct) >= 0.03 or abs(diff_abs) >= 100) else "minor"),
                            confidence=0.05,
                            reason=("raw diff would otherwise classify as "
                                    f"{'major' if (abs(diff_pct) >= 0.03 or abs(diff_abs) >= 100) else 'minor'} "
                                    "but fee pattern dominates"),
                        ),
                    ])
            except Exception:
                pass

    # Major / minor thresholds
    is_major = abs(diff_pct) >= 0.03 or abs(diff_abs) >= 100
    if is_major:
        return ("major", 0.90, [
            Evidence(
                source="threshold_major",
                evidence=(f"|diff_abs|=${abs(diff_abs):.2f} >= $100 or "
                          f"|diff_pct|={abs(diff_pct)*100:.2f}% >= 3%"),
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
