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
