"""Postgres persistence for agent runs and their event log.

The only module that reads or writes `runs` / `run_events`. Everything in
`agent_runtime` goes through here, which is what makes "one write path"
(spec §1.2) reviewable rather than aspirational.

Every function is account-scoped. A run is never loaded by id alone — a
cross-account read returns None rather than raising, so the API surface
never becomes an existence oracle.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select

from ..db.base import session_scope
from ..db.models import RunEventORM, RunORM
from ..models import AutonomyLevel, Run, RunEvent, RunEventType, RunStatus


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_run(
    *,
    account_id: str,
    goal: Dict[str, Any],
    autonomy: AutonomyLevel,
    budget: Dict[str, Any],
    playbook_id: Optional[str] = None,
) -> Run:
    run = Run(
        id=str(uuid.uuid4()),
        account_id=account_id,
        goal=goal,
        status=RunStatus.pending,
        autonomy=autonomy,
        playbook_id=playbook_id,
        budget=budget,
        created_at=_now(),
    )
    with session_scope() as s:
        s.add(RunORM(
            id=run.id,
            account_id=run.account_id,
            status=run.status.value,
            created_at=run.created_at,
            payload=run.model_dump(mode="json"),
        ))
    return run


def load_run(run_id: str, account_id: str) -> Optional[Run]:
    with session_scope() as s:
        row = s.scalar(
            select(RunORM).where(
                RunORM.id == run_id, RunORM.account_id == account_id,
            )
        )
        if row is None:
            return None
        return Run.model_validate(row.payload)


def _mutate(run_id: str, account_id: str, apply) -> None:
    """Load, mutate, and persist a run's payload inside one transaction."""
    with session_scope() as s:
        row = s.scalar(
            select(RunORM).where(
                RunORM.id == run_id, RunORM.account_id == account_id,
            ).with_for_update()
        )
        if row is None:
            raise KeyError(f"run {run_id} not found for account {account_id}")
        run = Run.model_validate(row.payload)
        apply(run)
        row.payload = run.model_dump(mode="json")
        row.status = run.status.value


def append_event(
    *, run: Run, type: RunEventType, payload: Dict[str, Any],
) -> RunEvent:
    with session_scope() as s:
        # Serialize appends for this run. Without this lock two concurrent
        # appends read the same max(seq) and collide on
        # UniqueConstraint("run_id", "seq"), losing one event to an
        # IntegrityError. Every append for a run goes through here, so
        # locking the parent row is sufficient.
        s.execute(
            select(RunORM.id).where(RunORM.id == run.id).with_for_update()
        )
        next_seq = s.scalar(
            select(func.coalesce(func.max(RunEventORM.seq), -1) + 1)
            .where(RunEventORM.run_id == run.id)
        )
        orm = RunEventORM(
            run_id=run.id,
            account_id=run.account_id,
            seq=int(next_seq),
            type=type.value,
            payload=payload,
            at=_now(),
        )
        s.add(orm)
        s.flush()  # assign the bigserial id before the session closes
        return RunEvent(
            id=orm.id, run_id=orm.run_id, account_id=orm.account_id,
            seq=orm.seq, type=type, payload=payload, at=orm.at,
        )


def events_since(
    *, run_id: str, account_id: str, after_id: int = 0, limit: int = 500,
) -> List[RunEvent]:
    with session_scope() as s:
        rows = s.scalars(
            select(RunEventORM)
            .where(
                RunEventORM.run_id == run_id,
                RunEventORM.account_id == account_id,
                RunEventORM.id > after_id,
            )
            .order_by(RunEventORM.id)
            .limit(limit)
        ).all()
        return [
            RunEvent(
                id=r.id, run_id=r.run_id, account_id=r.account_id, seq=r.seq,
                type=RunEventType(r.type), payload=r.payload, at=r.at,
            )
            for r in rows
        ]


def save_transcript(
    *, run_id: str, account_id: str,
    transcript: List[Dict[str, Any]], spend: Dict[str, Any],
) -> None:
    def apply(run: Run) -> None:
        run.transcript = transcript
        run.spend = spend

    _mutate(run_id, account_id, apply)


def set_status(
    *, run_id: str, account_id: str, status: RunStatus,
    error: Optional[str] = None, suspended_on: Optional[int] = None,
) -> None:
    def apply(run: Run) -> None:
        run.status = status
        if error is not None:
            run.error = error
        if suspended_on is not None:
            run.suspended_on = suspended_on
        if status in (RunStatus.done, RunStatus.failed, RunStatus.aborted):
            run.ended_at = _now()

    _mutate(run_id, account_id, apply)
