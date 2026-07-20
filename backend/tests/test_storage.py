import pytest


def test_list_jobs_filters_by_account_and_sorts(tmp_path, monkeypatch):
    monkeypatch.setenv("RECONOPS_DATA_DIR", str(tmp_path))
    import importlib
    from app import storage
    importlib.reload(storage)

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


def test_list_jobs_tolerates_corrupt_files(tmp_path, monkeypatch):
    monkeypatch.setenv("RECONOPS_DATA_DIR", str(tmp_path))
    import importlib
    from app import storage
    importlib.reload(storage)

    storage.save_job("ok", {"job_id": "ok", "account_id": "A",
                            "created_at": "2026-07-01T00:00:00Z", "status": "complete"})
    storage.ensure_dirs()
    (storage.JOBS_DIR / "bad.json").write_text("{truncated", encoding="utf-8")

    jobs = storage.list_jobs("A")
    assert [j["job_id"] for j in jobs] == ["ok"]


def test_update_job_merges_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("RECONOPS_DATA_DIR", str(tmp_path))
    import importlib
    from app import storage
    importlib.reload(storage)

    storage.save_job("j1", {"job_id": "j1", "account_id": "A", "status": "processing"})
    storage.update_job("j1", status="complete", summary={"matched_pct": 100.0})

    job = storage.load_job("j1")
    assert job["status"] == "complete"
    assert job["summary"] == {"matched_pct": 100.0}
    assert job["account_id"] == "A"   # untouched fields survive


def test_update_job_missing_job_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("RECONOPS_DATA_DIR", str(tmp_path))
    import importlib
    from app import storage
    importlib.reload(storage)

    with pytest.raises(FileNotFoundError):
        storage.update_job("nope", status="error")


def test_reap_stale_jobs_marks_processing_as_error(tmp_path, monkeypatch):
    monkeypatch.setenv("RECONOPS_DATA_DIR", str(tmp_path))
    import importlib
    from app import storage
    importlib.reload(storage)

    storage.save_job("stuck", {"job_id": "stuck", "account_id": "A", "status": "processing"})
    storage.save_job("done", {"job_id": "done", "account_id": "A", "status": "complete"})

    count = storage.reap_stale_jobs()

    assert count == 1
    assert storage.load_job("stuck")["status"] == "error"
    assert "restarted" in storage.load_job("stuck")["error"]
    assert storage.load_job("done")["status"] == "complete"
