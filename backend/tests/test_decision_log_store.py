from app.memory import accounts, decision_log
from app.models import DecisionLogEntry


def test_append_and_replay_preserves_order():
    acc = accounts.create_account()
    decision_log.append(acc.id, DecisionLogEntry(
        job_id="j1", row_key="k1", signature="s1",
        original_status="match", user_status="expected", user_reason="r1",
    ))
    decision_log.append(acc.id, DecisionLogEntry(
        job_id="j1", row_key="k2", signature="s2",
        original_status="fee_offset", user_status="expected", user_reason="r2",
    ))

    entries = list(decision_log.replay(acc.id))
    assert [e.row_key for e in entries] == ["k1", "k2"]


def test_all_entries_returns_list():
    acc = accounts.create_account()
    decision_log.append(acc.id, DecisionLogEntry(
        job_id="j1", row_key="k1", signature="s1",
        original_status="match", user_status="expected",
    ))
    assert len(decision_log.all_entries(acc.id)) == 1


def test_replay_empty_account_returns_nothing():
    acc = accounts.create_account()
    assert list(decision_log.replay(acc.id)) == []


def test_deleting_account_cascades_to_decisions():
    from app.db.base import session_scope
    from app.db.models import AccountORM, DecisionORM

    acc = accounts.create_account()
    decision_log.append(acc.id, DecisionLogEntry(
        job_id="j1", row_key="k1", signature="s1",
        original_status="match", user_status="expected",
    ))

    with session_scope() as s:
        s.delete(s.get(AccountORM, acc.id))

    with session_scope() as s:
        assert s.query(DecisionORM).filter(DecisionORM.account_id == acc.id).count() == 0
