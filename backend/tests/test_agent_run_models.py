from __future__ import annotations

import uuid

from sqlalchemy import select

from app.db.base import session_scope
from app.db.models import RunEventORM, RunORM
from app.models import AutonomyLevel, Run, RunEvent, RunEventType, RunStatus


def _account_row():
    """Runs FK to accounts; create a bare account row to satisfy it."""
    from app.db.models import AccountORM

    acct_id = str(uuid.uuid4())
    with session_scope() as s:
        s.add(AccountORM(id=acct_id, payload={}))
    return acct_id


def test_run_round_trips_through_postgres():
    acct = _account_row()
    run = Run(
        id=str(uuid.uuid4()),
        account_id=acct,
        goal={"intent": "close_month", "entities": {"month": "2026-06"}},
        status=RunStatus.pending,
        autonomy=AutonomyLevel.assist,
        budget={"usd_cap": 0.40, "tool_call_cap": 30, "wall_clock_s": 120},
    )
    with session_scope() as s:
        s.add(RunORM(
            id=run.id, account_id=run.account_id, status=run.status.value,
            payload=run.model_dump(mode="json"),
        ))

    with session_scope() as s:
        row = s.get(RunORM, run.id)
        assert row is not None
        loaded = Run.model_validate(row.payload)
    assert loaded.goal["intent"] == "close_month"
    assert loaded.autonomy is AutonomyLevel.assist
    assert loaded.budget["tool_call_cap"] == 30


def test_run_events_are_ordered_by_monotonic_id():
    """SSE Last-Event-ID resume depends on a total order."""
    acct = _account_row()
    run_id = str(uuid.uuid4())
    with session_scope() as s:
        s.add(RunORM(id=run_id, account_id=acct, status="running", payload={}))

    with session_scope() as s:
        for seq, etype in enumerate([
            RunEventType.goal_received,
            RunEventType.plan_proposed,
            RunEventType.tool_called,
        ]):
            s.add(RunEventORM(
                run_id=run_id, account_id=acct, seq=seq,
                type=etype.value, payload={"n": seq},
            ))

    with session_scope() as s:
        rows = list(s.scalars(
            select(RunEventORM).where(RunEventORM.run_id == run_id)
            .order_by(RunEventORM.id)
        ))
        observed = [(r.type, r.id) for r in rows]

    assert [t for t, _ in observed] == [
        "goal_received", "plan_proposed", "tool_called",
    ]
    ids = [i for _, i in observed]
    assert ids == sorted(ids)
    assert ids[0] < ids[1] < ids[2]


def test_run_event_model_validates_payload():
    ev = RunEvent(
        id=1, run_id="r", account_id="a", seq=0,
        type=RunEventType.tool_returned,
        payload={"tool": "bind_columns", "bound_count": 24},
        at="2026-08-01T00:00:00Z",
    )
    assert ev.type is RunEventType.tool_returned
    assert ev.payload["bound_count"] == 24
