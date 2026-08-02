from __future__ import annotations

import uuid

import pytest

from app.agent_runtime import store
from app.db.base import session_scope
from app.db.models import AccountORM
from app.models import AutonomyLevel, RunEventType, RunStatus


@pytest.fixture()
def account_id() -> str:
    acct = str(uuid.uuid4())
    with session_scope() as s:
        s.add(AccountORM(id=acct, payload={}))
    return acct


def test_create_and_load_run(account_id):
    run = store.create_run(
        account_id=account_id,
        goal={"intent": "close_month"},
        autonomy=AutonomyLevel.assist,
        budget={"tool_call_cap": 30},
    )
    assert run.status is RunStatus.pending
    assert run.created_at is not None

    loaded = store.load_run(run.id, account_id)
    assert loaded is not None
    assert loaded.goal["intent"] == "close_month"


def test_load_run_is_account_scoped(account_id):
    """Cross-account reads must return None, not raise — no existence oracle."""
    run = store.create_run(
        account_id=account_id, goal={}, autonomy=AutonomyLevel.assist, budget={},
    )
    other = str(uuid.uuid4())
    with session_scope() as s:
        s.add(AccountORM(id=other, payload={}))
    assert store.load_run(run.id, other) is None


def test_append_event_assigns_increasing_seq_and_id(account_id):
    run = store.create_run(
        account_id=account_id, goal={}, autonomy=AutonomyLevel.assist, budget={},
    )
    a = store.append_event(run=run, type=RunEventType.goal_received, payload={})
    b = store.append_event(run=run, type=RunEventType.plan_proposed, payload={"steps": []})

    assert a.seq == 0 and b.seq == 1
    assert a.id is not None and b.id is not None
    assert b.id > a.id


def test_events_since_supports_sse_resume(account_id):
    run = store.create_run(
        account_id=account_id, goal={}, autonomy=AutonomyLevel.assist, budget={},
    )
    first = store.append_event(run=run, type=RunEventType.goal_received, payload={})
    store.append_event(run=run, type=RunEventType.tool_called, payload={"tool": "x"})
    store.append_event(run=run, type=RunEventType.tool_returned, payload={"tool": "x"})

    resumed = store.events_since(
        run_id=run.id, account_id=account_id, after_id=first.id,
    )
    assert [e.type for e in resumed] == [
        RunEventType.tool_called, RunEventType.tool_returned,
    ]


def test_save_transcript_and_status_round_trip(account_id):
    run = store.create_run(
        account_id=account_id, goal={}, autonomy=AutonomyLevel.assist, budget={},
    )
    transcript = [{"role": "user", "content": "did June get paid?"}]
    store.save_transcript(
        run_id=run.id, account_id=account_id,
        transcript=transcript, spend={"tool_calls": 3},
    )
    store.set_status(run_id=run.id, account_id=account_id, status=RunStatus.running)

    loaded = store.load_run(run.id, account_id)
    assert loaded.transcript == transcript
    assert loaded.spend["tool_calls"] == 3
    assert loaded.status is RunStatus.running


def test_suspend_records_the_pending_question(account_id):
    run = store.create_run(
        account_id=account_id, goal={}, autonomy=AutonomyLevel.assist, budget={},
    )
    q = store.append_event(
        run=run, type=RunEventType.question_asked,
        payload={"text": "order date or payout date?"},
    )
    store.set_status(
        run_id=run.id, account_id=account_id,
        status=RunStatus.suspended, suspended_on=q.id,
    )

    loaded = store.load_run(run.id, account_id)
    assert loaded.status is RunStatus.suspended
    assert loaded.suspended_on == q.id
