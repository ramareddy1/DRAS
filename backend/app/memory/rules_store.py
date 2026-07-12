"""Per-account rule store + dispatcher.

A rule is a structured (`kind`, `when`, `then`) triple. The dispatcher knows
how to evaluate each kind on a row context and returns a Rationale-shaped
override (or None if no match).

Rules are stored as `data/accounts/{id}/rules.json` — a single JSON file
keyed by rule id. Default fee-pattern rules are seeded at account creation
so brands get a sensible baseline without any setup.

DSL choice (pilot-only). We deliberately avoid runtime-evaluating user
predicates as Python code. Each `kind` has a small fixed predicate shape
defined by this module. New behaviors require new kinds, not new strings —
which makes the rule store safe by construction.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..models import Alt, Evidence, Rationale, Rule
from .fsutil import account_lock, atomic_write_json


DATA_DIR = Path(os.getenv("RECONOPS_DATA_DIR", "data"))


def _rules_path(account_id: str) -> Path:
    return DATA_DIR / "accounts" / account_id / "rules.json"


# ---------------------------------------------------------------------------
# Default rules — seeded at account creation
# ---------------------------------------------------------------------------

DEFAULT_RULES_FOR_ACCOUNT = [
    {
        "kind": "fee_pattern",
        "description": "Stripe (2.9% + $0.30)",
        "when": {"rate": 0.029, "flat": 0.30, "tolerance_abs": 0.02, "tolerance_pct_of_amount": 0.001},
        "then": {"status": "fee_offset", "label": "Stripe (2.9% + $0.30)", "rule_id_hint": "stripe_fee_2.9_0.30"},
        "origin": "system",
        "confidence": 1.0,
        "state": "active",
    },
    {
        "kind": "fee_pattern",
        "description": "PayPal (2.99%)",
        "when": {"rate": 0.0299, "flat": 0.0, "tolerance_abs": 0.02, "tolerance_pct_of_amount": 0.001},
        "then": {"status": "fee_offset", "label": "PayPal (2.99%)", "rule_id_hint": "paypal_fee_2.99"},
        "origin": "system",
        "confidence": 1.0,
        "state": "active",
    },
    {
        "kind": "fee_pattern",
        "description": "PayPal (3.49% + $0.49)",
        "when": {"rate": 0.0349, "flat": 0.49, "tolerance_abs": 0.02, "tolerance_pct_of_amount": 0.001},
        "then": {"status": "fee_offset", "label": "PayPal (3.49% + $0.49)", "rule_id_hint": "paypal_fee_3.49_0.49"},
        "origin": "system",
        "confidence": 1.0,
        "state": "active",
    },
]


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _load_raw(account_id: str) -> Dict[str, Any]:
    p = _rules_path(account_id)
    if not p.exists():
        return {"rules": []}
    return json.loads(p.read_text(encoding="utf-8"))


def _save_raw(account_id: str, payload: Dict[str, Any]) -> None:
    atomic_write_json(_rules_path(account_id), payload, indent=2)


def load_rules(account_id: str) -> List[Rule]:
    raw = _load_raw(account_id)
    return [Rule.model_validate(r) for r in raw.get("rules", [])]


def save_rules(account_id: str, rules: List[Rule]) -> None:
    _save_raw(account_id, {"rules": [r.model_dump(mode="json") for r in rules]})


def seed_defaults(account_id: str) -> None:
    """Seed default fee-pattern rules. Called once at account creation."""
    if _rules_path(account_id).exists():
        return
    rules = [Rule(account_id=account_id, **defn) for defn in DEFAULT_RULES_FOR_ACCOUNT]
    save_rules(account_id, rules)


def add_rule(account_id: str, rule: Rule) -> Rule:
    with account_lock(account_id):
        rules = load_rules(account_id)
        rules.append(rule)
        save_rules(account_id, rules)
    return rule


def update_rule(account_id: str, rule_id: str, updates: Dict[str, Any]) -> Optional[Rule]:
    with account_lock(account_id):
        rules = load_rules(account_id)
        for r in rules:
            if r.id == rule_id:
                data = r.model_dump()
                data.update(updates)
                new_r = Rule.model_validate(data)
                # in-place mutation
                idx = rules.index(r)
                rules[idx] = new_r
                save_rules(account_id, rules)
                return new_r
    return None


def revoke_rule(account_id: str, rule_id: str) -> bool:
    return update_rule(account_id, rule_id, {"state": "revoked"}) is not None


# ---------------------------------------------------------------------------
# Dispatcher — evaluate rules against a row context
# ---------------------------------------------------------------------------


def _fits_fee_pattern(when: Dict[str, Any], a_amt: float, b_amt: float) -> bool:
    rate = float(when.get("rate", 0.0))
    flat = float(when.get("flat", 0.0))
    tol_abs = float(when.get("tolerance_abs", 0.02))
    tol_pct = float(when.get("tolerance_pct_of_amount", 0.001))
    if a_amt <= 0 or a_amt <= b_amt:
        return False
    expected = a_amt * rate + flat
    actual = a_amt - b_amt
    return abs(actual - expected) < max(tol_abs, a_amt * tol_pct)


def _matches_key_pattern(key: str, when: Dict[str, Any]) -> bool:
    prefix = (when.get("key_prefix") or "")
    contains = (when.get("key_contains") or "")
    if prefix and not key.startswith(prefix):
        return False
    if contains and contains not in key:
        return False
    return bool(prefix or contains)


def apply_rules_to_matched(
    rules: List[Rule], row_ctx: Dict[str, Any],
) -> Optional[Rationale]:
    """Return a Rationale if any rule applies, otherwise None.

    `row_ctx` must include: key, amount_a, amount_b, diff_abs.
    """
    a_amt = row_ctx.get("amount_a")
    b_amt = row_ctx.get("amount_b")
    if a_amt is None or b_amt is None:
        return None

    for r in rules:
        if r.state != "active":
            continue
        if r.kind == "fee_pattern":
            if _fits_fee_pattern(r.when, float(a_amt), float(b_amt)):
                expected = float(a_amt) * float(r.when.get("rate", 0)) + float(r.when.get("flat", 0))
                label = r.then.get("label", r.description or "fee pattern")
                src = r.then.get("rule_id_hint") or f"rule:{r.id[:8]}"
                ev = [Evidence(
                    source=src,
                    evidence=(f"diff_abs=${(a_amt - b_amt):.2f} matches {label} "
                              f"on amount=${a_amt:.2f} -> expected ~${expected:.2f}"),
                )]
                # Annotate provenance — was this a seeded rule or one the user taught us?
                if r.origin and r.origin != "system":
                    ev.append(Evidence(
                        source="rule_origin",
                        evidence=f"applied from rule '{r.description}' ({r.origin})",
                        weight=0.0,
                    ))
                if r.user_origin_text:
                    ev.append(Evidence(
                        source="rule_user_origin",
                        evidence=f"you told us: \"{r.user_origin_text}\"",
                        weight=0.0,
                    ))
                return Rationale(
                    row_key=row_ctx["key"],
                    status=r.then.get("status", "fee_offset"),
                    confidence=r.confidence,
                    rationale=ev,
                    alternatives=[],
                )
        elif r.kind == "force_status":
            sig_match = r.when.get("signature_prefix")
            if sig_match and row_ctx.get("signature", "").startswith(sig_match):
                return Rationale(
                    row_key=row_ctx["key"],
                    status=r.then.get("status", "match"),
                    confidence=r.confidence,
                    rationale=[Evidence(
                        source=f"rule:{r.id[:8]}",
                        evidence=f"forced by rule '{r.description}'",
                    )],
                    alternatives=[],
                )
        elif r.kind == "tolerance_override":
            # Handled inline by the agent (it reads tolerances at job start)
            continue
        # custom / expected_unmatched are evaluated elsewhere
    return None


def apply_force_status_rules(
    rules: List[Rule], signature: str, key: str,
) -> Optional[Rationale]:
    """Post-classification override.

    `force_status` rules are keyed on a TriageItem signature prefix — they're
    what the rule proposer writes after the user marks the same kind of gap
    expected ≥3 times, and what the user accepts on /rules. Because a
    signature is only knowable *after* an initial classification, the agent
    runs this pass once it has computed the row's signature. An active match
    here wins over the initial verdict.
    """
    for r in rules:
        if r.state != "active" or r.kind != "force_status":
            continue
        sig_prefix = r.when.get("signature_prefix") or r.when.get("signature")
        if not sig_prefix or not signature.startswith(sig_prefix):
            continue
        ev = [Evidence(
            source=f"rule:{r.id[:8]}",
            evidence=f"forced by rule '{r.description}'",
        )]
        if r.user_origin_text:
            ev.append(Evidence(
                source="rule_user_origin",
                evidence=f"you told us: \"{r.user_origin_text}\"",
                weight=0.0,
            ))
        return Rationale(
            row_key=key,
            status=r.then.get("status", "match"),
            confidence=r.confidence,
            rationale=ev,
            alternatives=[],
        )
    return None


def is_expected_unmatched(
    rules: List[Rule], side: str, key: str,
) -> Optional[Rule]:
    """Return the matching rule if the unmatched row is *expected* to be unmatched."""
    for r in rules:
        if r.state != "active" or r.kind != "expected_unmatched":
            continue
        if r.when.get("side", "").lower() != side.lower():
            continue
        if _matches_key_pattern(key, r.when):
            return r
    return None


def applicable_tolerances(rules: List[Rule]) -> Tuple[Optional[float], Optional[float]]:
    """Return (abs, pct) overrides if a tolerance_override rule is active."""
    for r in rules:
        if r.state == "active" and r.kind == "tolerance_override":
            return r.when.get("tolerance_abs"), r.when.get("tolerance_pct")
    return None, None
