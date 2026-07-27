import pytest


def _seed_account(account_id: str) -> None:
    """Insert a bare AccountORM row so job_id FK inserts below satisfy the
    accounts.id foreign key — these tests only exercise storage.*, not the
    account domain object, so a raw id/empty payload row is sufficient."""
    from app.db.base import session_scope
    from app.db.models import AccountORM

    with session_scope() as s:
        s.add(AccountORM(id=account_id, payload={}))


def test_list_jobs_filters_by_account_and_sorts():
    from app import storage

    _seed_account("A")
    _seed_account("B")

    storage.save_job("j1", {"job_id": "j1", "account_id": "A", "created_at": "2026-07-01T00:00:00Z",
                            "status": "complete", "summary": {"matched_pct": 90.0}})
    storage.save_job("j2", {"job_id": "j2", "account_id": "A", "created_at": "2026-07-02T00:00:00Z",
                            "status": "complete", "summary": {"matched_pct": 95.0}})
    storage.save_job("j3", {"job_id": "j3", "account_id": "B", "created_at": "2026-07-03T00:00:00Z",
                            "status": "complete", "summary": {}})

    jobs = storage.list_jobs("A")
    assert [j["job_id"] for j in jobs] == ["j2", "j1"]
    assert jobs[0]["matched_pct"] == 95.0
    assert all(j.get("account_id") != "B" for j in jobs)


def test_update_job_merges_fields():
    from app import storage

    _seed_account("A")

    storage.save_job("j1", {"job_id": "j1", "account_id": "A", "status": "processing"})
    storage.update_job("j1", status="complete", summary={"matched_pct": 100.0})

    job = storage.load_job("j1")
    assert job["status"] == "complete"
    assert job["summary"] == {"matched_pct": 100.0}
    assert job["account_id"] == "A"   # untouched fields survive


def test_update_job_missing_job_raises():
    from app import storage

    with pytest.raises(FileNotFoundError):
        storage.update_job("nope", status="error")


def test_reap_stale_jobs_marks_processing_as_error():
    from app import storage

    _seed_account("A")

    storage.save_job("stuck", {"job_id": "stuck", "account_id": "A", "status": "processing"})
    storage.save_job("done", {"job_id": "done", "account_id": "A", "status": "complete"})

    count = storage.reap_stale_jobs()

    assert count == 1
    assert storage.load_job("stuck")["status"] == "error"
    assert "restarted" in storage.load_job("stuck")["error"]
    assert storage.load_job("done")["status"] == "complete"


def test_deleting_account_cascades_to_jobs():
    from app.db.base import session_scope
    from app.db.models import AccountORM, JobORM
    from app import storage
    from app.memory import accounts

    acc = accounts.create_account()
    storage.save_job("j1", {"job_id": "j1", "account_id": acc.id, "status": "complete"})

    with session_scope() as s:
        row = s.get(AccountORM, acc.id)
        s.delete(row)

    with session_scope() as s:
        assert s.get(JobORM, "j1") is None


def test_cleanup_honors_per_account_retention_days():
    from datetime import datetime, timedelta
    from app import storage
    from app.memory import accounts

    short = accounts.create_account()
    accounts.update_profile(short.id, {"retention_days": 1})
    long_ = accounts.create_account()  # default retention_days=7

    three_days_ago = (datetime.utcnow() - timedelta(days=3)).isoformat() + "Z"
    storage.save_job("short-old", {"job_id": "short-old", "account_id": short.id,
                                    "created_at": three_days_ago, "status": "complete"})
    storage.save_job("long-old", {"job_id": "long-old", "account_id": long_.id,
                                   "created_at": three_days_ago, "status": "complete"})

    storage.cleanup()

    assert storage.load_job("short-old") is None    # 3 days > short's 1-day retention
    assert storage.load_job("long-old") is not None  # 3 days < long's 7-day default
