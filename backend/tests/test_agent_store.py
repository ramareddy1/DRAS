from __future__ import annotations

import threading
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


def test_concurrent_append_event_does_not_collide_on_seq(account_id):
    """Regression test for UniqueConstraint("run_id", "seq").

    `append_event` computes `next_seq = max(seq) + 1` and then inserts.
    Without locking the parent `runs` row for the duration of that
    read-then-insert, concurrent appends to the SAME run can read the same
    max(seq), both attempt to insert the same seq, and the loser gets an
    IntegrityError from the database — silently dropping an event. This
    test spawns several threads that genuinely contend (via a Barrier) to
    append events to one run concurrently and asserts every append
    succeeds with a contiguous, gap-free, duplicate-free seq sequence.
    """
    run = store.create_run(
        account_id=account_id, goal={}, autonomy=AutonomyLevel.assist, budget={},
    )

    n_threads = 8
    barrier = threading.Barrier(n_threads)
    results = []
    errors = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        try:
            barrier.wait()  # maximize contention: all threads race together
            event = store.append_event(
                run=run, type=RunEventType.tool_called, payload={"i": i},
            )
            with lock:
                results.append(event)
        except Exception as exc:  # pragma: no cover - failure path
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"append_event raised in {len(errors)} thread(s): {errors}"
    assert len(results) == n_threads

    seqs = [e.seq for e in results]
    assert sorted(seqs) == list(range(n_threads)), (
        f"expected contiguous seqs 0..{n_threads - 1} with no duplicates/"
        f"gaps, got {sorted(seqs)}"
    )


def test_claim_suspended_flips_status_and_clears_the_question(account_id):
    run = store.create_run(
        account_id=account_id, goal={}, autonomy=AutonomyLevel.assist, budget={},
    )
    q = store.append_event(
        run=run, type=RunEventType.question_asked, payload={"tool": "x"},
    )
    store.set_status(
        run_id=run.id, account_id=account_id,
        status=RunStatus.suspended, suspended_on=q.id,
    )

    claimed = store.claim_suspended(run.id, account_id)
    assert claimed is not None
    assert claimed.status is RunStatus.running
    assert claimed.suspended_on is None


def test_claim_suspended_is_single_use(account_id):
    """The double-POST guard: two answers must not both execute the tool."""
    run = store.create_run(
        account_id=account_id, goal={}, autonomy=AutonomyLevel.assist, budget={},
    )
    store.set_status(
        run_id=run.id, account_id=account_id, status=RunStatus.suspended,
    )

    assert store.claim_suspended(run.id, account_id) is not None
    assert store.claim_suspended(run.id, account_id) is None


def test_claim_suspended_refuses_a_run_that_never_suspended(account_id):
    run = store.create_run(
        account_id=account_id, goal={}, autonomy=AutonomyLevel.assist, budget={},
    )
    assert store.claim_suspended(run.id, account_id) is None


def test_claim_suspended_is_account_scoped(account_id):
    run = store.create_run(
        account_id=account_id, goal={}, autonomy=AutonomyLevel.assist, budget={},
    )
    store.set_status(
        run_id=run.id, account_id=account_id, status=RunStatus.suspended,
    )
    other = str(uuid.uuid4())
    with session_scope() as s:
        s.add(AccountORM(id=other, payload={}))

    assert store.claim_suspended(run.id, other) is None
    # ...and the run is untouched, so the owner can still claim it.
    assert store.claim_suspended(run.id, account_id) is not None


def test_rejected_calls_round_trip(account_id):
    run = store.create_run(
        account_id=account_id, goal={}, autonomy=AutonomyLevel.assist, budget={},
    )
    store.record_rejected_calls(
        run_id=run.id, account_id=account_id,
        calls=[{"tool": "run_reconciliation", "input": {"a": 1}}],
    )

    loaded = store.load_run(run.id, account_id)
    assert loaded.rejected_calls == [
        {"tool": "run_reconciliation", "input": {"a": 1}},
    ]
