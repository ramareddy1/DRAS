"""One-shot importer: existing JSON-on-disk pilot data -> Postgres.

Idempotent per entity: accounts/rules/triage/jobs upsert by their existing
id; decisions/metrics (which have no natural id — they're append-only logs)
skip an account entirely if it already has any rows, so re-running the
script after a partial cutover never duplicates history.

Usage (from backend/, with RECONOPS_DATABASE_URL set):
    python -m scripts.import_json_to_postgres --data-dir data
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.db.base import session_scope
from app.db.models import AccountORM, DecisionORM, JobORM, MetricORM, RuleORM, TriageItemORM


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def import_accounts(data_dir: Path) -> int:
    accounts_dir = data_dir / "accounts"
    if not accounts_dir.exists():
        return 0
    count = 0
    with session_scope() as s:
        for acc_dir in accounts_dir.iterdir():
            profile_path = acc_dir / "profile.json"
            if not profile_path.exists():
                continue
            payload = _load_json(profile_path)
            row = s.get(AccountORM, payload["id"])
            if row is None:
                row = AccountORM(id=payload["id"])
                s.add(row)
            row.payload = payload
            count += 1
    return count


def import_rules(data_dir: Path) -> int:
    accounts_dir = data_dir / "accounts"
    if not accounts_dir.exists():
        return 0
    count = 0
    with session_scope() as s:
        for acc_dir in accounts_dir.iterdir():
            rules_path = acc_dir / "rules.json"
            if not rules_path.exists():
                continue
            account_id = acc_dir.name
            raw = _load_json(rules_path)
            for r in raw.get("rules", []):
                row = s.get(RuleORM, r["id"])
                if row is None:
                    row = RuleORM(id=r["id"], account_id=account_id)
                    s.add(row)
                row.payload = r
                count += 1
    return count


def import_triage(data_dir: Path) -> int:
    accounts_dir = data_dir / "accounts"
    if not accounts_dir.exists():
        return 0
    count = 0
    with session_scope() as s:
        for acc_dir in accounts_dir.iterdir():
            triage_path = acc_dir / "triage.json"
            if not triage_path.exists():
                continue
            account_id = acc_dir.name
            raw = _load_json(triage_path)
            for i in raw.get("items", []):
                row = s.get(TriageItemORM, i["id"])
                if row is None:
                    row = TriageItemORM(id=i["id"], account_id=account_id)
                    s.add(row)
                row.signature = i["signature"]
                row.state = i["state"]
                row.payload = i
                count += 1
    return count


def import_decisions(data_dir: Path) -> int:
    accounts_dir = data_dir / "accounts"
    if not accounts_dir.exists():
        return 0
    count = 0
    with session_scope() as s:
        for acc_dir in accounts_dir.iterdir():
            decisions_path = acc_dir / "decisions.jsonl"
            if not decisions_path.exists():
                continue
            account_id = acc_dir.name
            existing = s.query(DecisionORM).filter(DecisionORM.account_id == account_id).count()
            if existing > 0:
                continue
            for line in decisions_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                s.add(DecisionORM(account_id=account_id, payload=json.loads(line)))
                count += 1
    return count


def import_metrics(data_dir: Path) -> int:
    accounts_dir = data_dir / "accounts"
    if not accounts_dir.exists():
        return 0
    count = 0
    with session_scope() as s:
        for acc_dir in accounts_dir.iterdir():
            metrics_path = acc_dir / "metrics.jsonl"
            if not metrics_path.exists():
                continue
            account_id = acc_dir.name
            existing = s.query(MetricORM).filter(MetricORM.account_id == account_id).count()
            if existing > 0:
                continue
            for line in metrics_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                s.add(MetricORM(account_id=account_id, payload=json.loads(line)))
                count += 1
    return count


def import_jobs(data_dir: Path) -> int:
    jobs_dir = data_dir / "jobs"
    if not jobs_dir.exists():
        return 0
    count = 0
    with session_scope() as s:
        for job_path in jobs_dir.glob("*.json"):
            payload = _load_json(job_path)
            job_id = payload["job_id"]
            row = s.get(JobORM, job_id)
            if row is None:
                row = JobORM(job_id=job_id)
                s.add(row)
            flat = dict(payload)
            row.account_id = flat.pop("account_id", None)
            row.created_at = flat.pop("created_at", None)
            row.status = flat.pop("status", "complete")
            flat.pop("job_id", None)
            row.payload = flat
            count += 1
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="data", help="Path to the legacy JSON data directory")
    args = parser.parse_args()
    data_dir = Path(args.data_dir)

    print(f"accounts:  {import_accounts(data_dir)}")
    print(f"rules:     {import_rules(data_dir)}")
    print(f"triage:    {import_triage(data_dir)}")
    print(f"decisions: {import_decisions(data_dir)}")
    print(f"metrics:   {import_metrics(data_dir)}")
    print(f"jobs:      {import_jobs(data_dir)}")


if __name__ == "__main__":
    main()
