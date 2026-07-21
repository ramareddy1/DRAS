from app.memory import accounts, triage as triage_store
from app.models import TriageItem


def test_save_and_load_all_round_trips():
    acc = accounts.create_account()
    item = TriageItem(account_id=acc.id, signature="sig1", state="open",
                       source_job_ids=["j1"], row_key="k1", status="unmatched_a", side="a")
    triage_store.save_all(acc.id, [item])

    loaded = triage_store.load_all(acc.id)
    assert len(loaded) == 1
    assert loaded[0].id == item.id
    assert loaded[0].signature == "sig1"


def test_emit_for_job_dedups_by_signature():
    acc = accounts.create_account()
    row = {"key": "#123", "amount_a": 10.0, "amount_b": 9.0, "diff_abs": 1.0, "fee_pattern": None,
           "rationale": {"row_key": "#123", "status": "minor", "confidence": 0.5, "rationale": [], "alternatives": []}}

    emitted1 = triage_store.emit_for_job(acc.id, "job1", rationales=[row], unmatched_a=[], unmatched_b=[])
    emitted2 = triage_store.emit_for_job(acc.id, "job2", rationales=[row], unmatched_a=[], unmatched_b=[])

    assert len(emitted1) == 1
    assert len(emitted2) == 1
    assert emitted1[0].id == emitted2[0].id  # same signature → bumped, not duplicated
    all_items = triage_store.load_all(acc.id)
    assert len(all_items) == 1
    assert all_items[0].source_job_ids == ["job1", "job2"]


def test_resolve_marks_resolved():
    acc = accounts.create_account()
    item = TriageItem(account_id=acc.id, signature="sig1", state="open", row_key="k1")
    triage_store.save_all(acc.id, [item])

    resolved = triage_store.resolve(acc.id, item.id, action="mark_expected", user_reason="normal fee")
    assert resolved.state == "resolved"
    assert resolved.resolution["action"] == "mark_expected"


def test_deleting_account_cascades_to_triage_items():
    from app.db.base import session_scope
    from app.db.models import AccountORM, TriageItemORM

    acc = accounts.create_account()
    item = TriageItem(account_id=acc.id, signature="sig1", state="open", row_key="k1")
    triage_store.save_all(acc.id, [item])

    with session_scope() as s:
        s.delete(s.get(AccountORM, acc.id))

    with session_scope() as s:
        assert s.query(TriageItemORM).filter(TriageItemORM.account_id == acc.id).count() == 0
