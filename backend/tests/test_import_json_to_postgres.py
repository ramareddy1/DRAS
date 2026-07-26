import json

from app.memory import accounts, rules_store, triage as triage_store, decision_log, metrics as metrics_store
from app import storage
from scripts.import_json_to_postgres import (
    import_accounts, import_rules, import_triage, import_decisions, import_metrics, import_jobs,
)


def _write_legacy_account(tmp_path, account_id, display_name="Legacy Co"):
    acc_dir = tmp_path / "accounts" / account_id
    acc_dir.mkdir(parents=True)
    profile = {
        "id": account_id, "display_name": display_name,
        "created_at": "2026-01-01T00:00:00", "profile": {
            "time_zone": "UTC", "amount_tolerance_abs": 0.01, "amount_tolerance_pct": 0.005,
            "materiality_abs": 100.0, "materiality_pct": 0.03,
        },
    }
    (acc_dir / "profile.json").write_text(json.dumps(profile), encoding="utf-8")
    return acc_dir


def test_import_accounts_creates_row(tmp_path):
    account_id = "11111111-1111-4111-8111-111111111111"
    _write_legacy_account(tmp_path, account_id)

    count = import_accounts(tmp_path)

    assert count == 1
    acc = accounts.load_account(account_id)
    assert acc is not None
    assert acc.display_name == "Legacy Co"


def test_import_is_idempotent(tmp_path):
    account_id = "22222222-2222-4222-8222-222222222222"
    _write_legacy_account(tmp_path, account_id)

    import_accounts(tmp_path)
    count2 = import_accounts(tmp_path)  # re-run — must not fail or duplicate

    assert count2 == 1
    assert accounts.load_account(account_id).id == account_id


def test_import_rules_triage_decisions_metrics_and_jobs(tmp_path):
    account_id = "33333333-3333-4333-8333-333333333333"
    acc_dir = _write_legacy_account(tmp_path, account_id)
    import_accounts(tmp_path)

    (acc_dir / "rules.json").write_text(json.dumps({"rules": [{
        "id": "44444444-4444-4444-8444-444444444444", "account_id": account_id,
        "kind": "fee_pattern", "description": "legacy rule", "when": {}, "then": {},
        "origin": "system", "confidence": 1.0, "state": "active",
        "created_at": "2026-01-01T00:00:00", "applied_signatures": [],
    }]}), encoding="utf-8")
    (acc_dir / "triage.json").write_text(json.dumps({"items": [{
        "id": "55555555-5555-4555-8555-555555555555", "account_id": account_id,
        "signature": "sig1", "state": "open", "created_at": "2026-01-01T00:00:00",
        "last_seen_at": "2026-01-01T00:00:00", "source_job_ids": ["j1"], "row_key": "k1",
        "status": "unmatched_a", "side": "a",
    }]}), encoding="utf-8")
    (acc_dir / "decisions.jsonl").write_text(
        json.dumps({"job_id": "j1", "row_key": "k1", "signature": "sig1",
                    "original_status": "match", "user_status": "expected"}) + "\n",
        encoding="utf-8",
    )
    (acc_dir / "metrics.jsonl").write_text(
        json.dumps({"job_id": "j1", "at": "2026-01-01T00:00:00", "total_rows": 1,
                    "auto_handled": 1, "needed_user": 0, "insight_density": 1.0,
                    "override_rate": 0.0, "revocation_rate": 0.0,
                    "trust_adjusted_density": 1.0, "llm_calls": 0}) + "\n",
        encoding="utf-8",
    )
    jobs_dir = tmp_path / "jobs"
    jobs_dir.mkdir()
    (jobs_dir / "j1.json").write_text(json.dumps({
        "job_id": "j1", "account_id": account_id, "created_at": "2026-01-01T00:00:00Z",
        "status": "complete", "summary": {"matched_pct": 100.0},
    }), encoding="utf-8")

    assert import_rules(tmp_path) == 1
    assert import_triage(tmp_path) == 1
    assert import_decisions(tmp_path) == 1
    assert import_metrics(tmp_path) == 1
    assert import_jobs(tmp_path) == 1

    assert len(rules_store.load_rules(account_id)) == 1
    assert len(triage_store.load_all(account_id)) == 1
    assert len(decision_log.all_entries(account_id)) == 1
    assert len(metrics_store.series(account_id)) == 1
    assert storage.load_job("j1")["status"] == "complete"

    # idempotent re-run for the append-only stores doesn't duplicate rows
    import_decisions(tmp_path)
    import_metrics(tmp_path)
    assert len(decision_log.all_entries(account_id)) == 1
    assert len(metrics_store.series(account_id)) == 1
