from __future__ import annotations

import threading
import uuid

import pytest
from sqlalchemy import select

from app.agent_runtime import store
from app.db.base import session_scope
from app.db.models import AccountORM, RunORM
from app.models import AutonomyLevel, Run, RunEventType, RunStatus


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


def test_claim_suspended_under_real_contention_admits_exactly_one_thread(
    account_id,
):
    """8 threads race to claim one suspended run — but this is NOT the
    lock's regression guard; see the honest caveat below.

    `test_claim_suspended_is_single_use` above is purely sequential — two
    back-to-back calls in one thread, each in its own `session_scope()`
    that fully commits before the next opens. This test instead forces
    genuine contention, following
    `test_concurrent_append_event_does_not_collide_on_seq`'s pattern: a
    `Barrier` releases N threads together, all racing to claim the SAME
    suspended run. When it passes, it does prove something real — that
    under actual concurrent load, `claim_suspended` never lets two threads
    both walk away with a claimed run (no double-claim was observed).

    What it does *not* prove: that this outcome depends on
    `.with_for_update()` being present. Verified by hand — with the lock
    temporarily removed from `claim_suspended` and no artificial delay
    added anywhere, this test still passed 5/5 runs on a local Postgres
    instance. 8 Python threads under the GIL, each doing one fast local
    round trip, apparently don't reliably interleave inside the narrow
    read-then-write window a naive implementation would need to lose on.
    So an accidental removal of the row lock would very likely leave this
    test green — it is not a reliable regression guard for the lock.

    The actual regression guard is
    `test_claim_suspended_blocks_a_second_claimant_while_the_row_lock_is_held`
    below, which controls the interleaving deterministically instead of
    racing for it. This test is kept anyway because it exercises a
    different, still-useful property under real thread contention (no
    double-claim observed empirically across many concurrent callers), as
    long as no one mistakes it for proof that the lock exists.
    """
    run = store.create_run(
        account_id=account_id, goal={}, autonomy=AutonomyLevel.assist, budget={},
    )
    store.set_status(
        run_id=run.id, account_id=account_id, status=RunStatus.suspended,
    )

    n_threads = 8
    barrier = threading.Barrier(n_threads)
    results = []
    errors = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        try:
            barrier.wait()  # maximize contention: all threads race together
            claimed = store.claim_suspended(run.id, account_id)
            with lock:
                results.append(claimed)
        except Exception as exc:  # pragma: no cover - failure path
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"claim_suspended raised in {len(errors)} thread(s): {errors}"
    assert len(results) == n_threads

    winners = [r for r in results if r is not None]
    assert len(winners) == 1, (
        f"expected exactly one thread to claim the run, got {len(winners)}"
    )
    assert winners[0].status is RunStatus.running
    assert winners[0].suspended_on is None


def test_claim_suspended_blocks_a_second_claimant_while_the_row_lock_is_held(
    account_id,
):
    """The lock's actual regression guard — controls the interleaving
    instead of racing for it.

    The threaded test above races 8 threads and hopes to land in the
    narrow window where an unlocked `claim_suspended` would double-claim.
    On this box that window is too narrow to hit by chance (see its
    docstring). This test does not gamble on timing at all: it forces the
    exact interleaving that proves the row lock is what's serializing
    claimants.

    The main thread performs the identical `SELECT ... FOR UPDATE` that
    `claim_suspended` issues, flips the row to `running` inside that
    transaction, and deliberately does **not** commit yet — the lock stays
    held. A second thread then calls the real `store.claim_suspended` on
    the same run. With a correctly-locked implementation, that call blocks
    inside its own `SELECT ... FOR UPDATE` for as long as the main
    transaction stays open — asserted directly below via
    `thread.join(timeout=...)` returning because of the timeout, not
    because the thread finished, so `thread.is_alive()` is still true.
    Only after the main transaction commits (releasing the lock) does the
    second thread wake up, see the row is now `running` rather than
    `suspended`, and correctly return `None`.

    Empirically confirmed (see the Task 7 fix report) that removing
    `.with_for_update()` from `claim_suspended` makes this test fail
    reliably, with no artificial delay anywhere — but not quite via the
    mechanism this docstring first assumed. A plain, non-locking `SELECT`
    never blocks in Postgres, so the second thread's read returns
    instantly with the pre-commit snapshot (still `suspended`) rather than
    blocking at `assert thread.is_alive()`. It only blocks later, inside
    its own commit, because the implicit `UPDATE` SQLAlchemy issues to
    persist `row.payload`/`row.status` still needs the row's write lock —
    which the held-open main transaction is holding regardless of whether
    it took that lock via `FOR UPDATE` or via being itself an uncommitted
    writer. So `thread.is_alive()` reads `True` either way; it is a real,
    useful sanity check (proves the second claimant genuinely goes through
    Postgres locking rather than some out-of-band, lock-free path) but not
    the discriminating assertion. The discriminator is the *value* the
    second thread returns once it does unblock: it flushes the `run`
    object it built from its stale pre-commit read (still "suspended" ->
    "running"), unconditionally overwriting the row with no re-check of
    the now-current status — a lost update — and hands back a non-`None`
    `Run` as if it had won the claim, even though the main thread's
    transaction already had. That is exactly the double-claim
    `claim_suspended` exists to prevent, and it is what
    `assert result == [None]` below catches. This is not luck: given the
    main transaction is deliberately held open for the full 2-second join,
    Postgres's read-committed semantics plus SQLAlchemy's unconditional
    UPDATE make this outcome guaranteed on every run when the lock is
    missing, not just probable.
    """
    run = store.create_run(
        account_id=account_id, goal={}, autonomy=AutonomyLevel.assist, budget={},
    )
    store.set_status(
        run_id=run.id, account_id=account_id, status=RunStatus.suspended,
    )

    result = []
    errors = []

    def claim_in_background() -> None:
        try:
            result.append(store.claim_suspended(run.id, account_id))
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    thread = threading.Thread(target=claim_in_background)
    try:
        with session_scope() as s:
            row = s.scalar(
                select(RunORM).where(
                    RunORM.id == run.id, RunORM.account_id == account_id,
                ).with_for_update()
            )
            held = Run.model_validate(row.payload)
            held.status = RunStatus.running
            held.suspended_on = None
            row.payload = held.model_dump(mode="json")
            row.status = held.status.value

            # The row lock is held by this open, uncommitted transaction.
            # A second, correctly-implemented claimant must block here.
            thread.start()
            thread.join(timeout=2.0)
            assert thread.is_alive(), (
                "second claimant returned while the row lock was still "
                "held open — claim_suspended is not blocking on "
                "SELECT ... FOR UPDATE"
            )
        # Exiting the `with` block committed the transaction above,
        # releasing the lock the second thread was blocked on.
    finally:
        # Always join, on every path (including assertion failure above),
        # so a failing run can't leave a background thread — or the lock
        # its transaction might still be holding — outliving this test.
        thread.join(timeout=5.0)

    assert not thread.is_alive(), (
        "worker thread never finished after the lock was released"
    )
    assert errors == [], f"claim_suspended raised: {errors}"
    assert result == [None], (
        f"expected the second claimant to see `running` and decline, got {result}"
    )


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
