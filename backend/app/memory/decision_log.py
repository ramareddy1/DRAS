"""Per-account decision log — append-only.

Every time the user disagrees with the system (mark expected, override
classification, rebind a column, override a binding, etc.) one row is
appended here. This is the training signal for rule proposals, the
override-rate metric, and Phase 6's replay-eval.

Append-only by design — corrections to the corrections are themselves
new entries. We never edit history.
"""
from __future__ import annotations

from typing import Iterator, List

from ..models import DecisionLogEntry
from ..db.base import session_scope
from ..db.models import DecisionORM


def append(account_id: str, entry: DecisionLogEntry) -> None:
    with session_scope() as s:
        s.add(DecisionORM(account_id=account_id, payload=entry.model_dump(mode="json")))


def replay(account_id: str) -> Iterator[DecisionLogEntry]:
    with session_scope() as s:
        rows = (
            s.query(DecisionORM)
            .filter(DecisionORM.account_id == account_id)
            .order_by(DecisionORM.id.asc())
            .all()
        )
        for row in rows:
            yield DecisionLogEntry.model_validate(row.payload)


def all_entries(account_id: str) -> List[DecisionLogEntry]:
    return list(replay(account_id))
