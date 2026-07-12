"""Replay a real account's history against the current rules.

    python scripts/replay_eval.py <account_id>

Loads every persisted job for the account, concatenates their matched rows,
and replays them against the current rule set — comparing each verdict to the
user's recorded decision-log truth. Prints accuracy before/after, the
override-rate, and any regressions a rule change has introduced.

This is the manual counterpart to `python -m app.eval` (which runs the pinned
sample snapshots + a synthetic decision log). Use it after editing rules or
the classifier to confirm you haven't broken what a real account taught you.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Make the `app` package importable when run as a loose script.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import storage  # noqa: E402
from app.replay import replay  # noqa: E402


def _account_jobs(account_id: str):
    matched = []
    job_count = 0
    for p in sorted(storage.JOBS_DIR.glob("*.json")):
        try:
            job = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if job.get("account_id") != account_id:
            continue
        job_count += 1
        matched.extend(job.get("matched", []))
    return job_count, matched


def main(argv):
    if not argv:
        print("usage: python scripts/replay_eval.py <account_id>")
        return 2
    account_id = argv[0]
    job_count, matched = _account_jobs(account_id)
    if job_count == 0:
        print(f"No persisted jobs found for account {account_id} "
              f"(data dir: {storage.DATA_DIR}).")
        return 1

    report = replay(account_id, matched)
    r = report.as_dict()
    print(f"Account {account_id}")
    print(f"  jobs replayed:    {job_count}  ({len(matched)} matched rows)")
    print(f"  signatures eval:  {r['evaluated']}")
    print(f"  accuracy before:  {r['accuracy_before']}")
    print(f"  accuracy after:   {r['accuracy_after']}")
    print(f"  override rate:    {r['override_rate']}")
    if r["notes"]:
        for n in r["notes"]:
            print(f"  note: {n}")
    if r["regressions"]:
        print(f"  REGRESSIONS ({len(r['regressions'])}):")
        for reg in r["regressions"]:
            print(f"    {reg['signature'][:8]}… {reg['before']} -> {reg['after']} "
                  f"(user wanted {reg['user_status']}, row {reg['row_key']})")
        return 1
    print("  no regressions.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
