"""Column-embedding index — Phase 4 stub.

Per [PLAN.md](PLAN.md) §4.4, semantic memory of past confirmations is
supposed to live here: when the user has confirmed `Order Total →
order.gross_total` 3 times, a column named `Total Price` (with similar
sample values) should bind to the same concept by embedding similarity.

For the pilot we ship `learned_aliases.py` (an exact / normalized-name
lookup) and keep this module as a deliberate stub so the architecture
shape is right without taking on a heavy dep (sentence-transformers or
remote embedding calls) before it's earning its keep.

The shape of the API matches the eventual implementation. Phase 4
verification, Phase 5 UI, and Phase 6 eval all call through this surface;
swapping in real embeddings later is a localized change.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def upsert(
    account_id: str, column_name: str, sample_values: List[str], concept_id: str,
) -> None:
    """No-op in the pilot. Real implementation will write to
    `embeddings.parquet` (or sqlite-vss) with an embedding of
    column_name + tokenized sample values."""
    return None


def lookup(
    account_id: str, column_name: str, sample_values: List[str],
    threshold: float = 0.85,
) -> Optional[Dict[str, Any]]:
    """Return None in the pilot.

    Real implementation returns `{"concept_id": ..., "score": ...,
    "matched_against": <prior column name>}` when the cosine similarity to
    a stored confirmation exceeds `threshold`.
    """
    return None


def stats(account_id: str) -> Dict[str, Any]:
    return {"entries": 0, "note": "embedding index is a stub in the pilot"}
