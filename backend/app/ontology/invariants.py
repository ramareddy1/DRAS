"""Concept-level invariants.

For Phase 1 these are declarations the LLM and reports can reference. They are
not enforced programmatically yet — Phase 3+ will use them to drive
amount-comparison logic across more than one column pair.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple


@dataclass(frozen=True)
class Invariant:
    """A relation that should hold across matched rows."""
    name: str
    expression: str        # human-readable, e.g. "order.gross_total ≈ payment.amount + payment.fee"
    concepts: Tuple[str, ...]   # concept ids referenced
    tolerance_abs: float = 0.01
    tolerance_pct: float = 0.005


INVARIANTS: List[Invariant] = [
    Invariant(
        name="orders_balance_payments",
        expression="order.gross_total ≈ payment.amount + payment.fee",
        concepts=("order.gross_total", "payment.amount", "payment.fee"),
    ),
    Invariant(
        name="inventory_parity",
        expression="sku.qty_available [A] ≈ sku.qty_available [B]",
        concepts=("sku.qty_available",),
    ),
    Invariant(
        name="po_matches_invoice",
        expression="po.amount ≈ invoice.amount (per matched key)",
        concepts=("po.amount", "invoice.amount"),
    ),
]


def invariants_for(concept_ids: List[str]) -> List[Invariant]:
    s = set(concept_ids)
    return [inv for inv in INVARIANTS if s & set(inv.concepts)]
