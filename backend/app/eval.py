"""ReconOps eval runner — `python -m app.eval`.

Two suites, both deterministic (stub LLM, throwaway data dir):

  1. Snapshot suite — runs each sample pair and compares summary stats to the
     pinned `samples/samples_*_expected.json`. Catches any classifier /
     ontology / matching regression.

  2. Synthetic-corrections suite — builds an account, runs a sample job,
     synthesizes a decision log of ≥30 user confirmations, then replays the
     job against the (current) rules. Asserts accuracy ≥ 95% and
     override-rate < 5%.

Exit code is non-zero if any check fails — wire it into CI.

Self-test:  `python -m app.eval --inject-bad-rule`  seeds a rule that
contradicts the recorded truth; the replay must catch it as a regression and
the runner must exit non-zero. Use it to prove the eval actually bites.
"""
from __future__ import annotations

import os
import sys
import json
import tempfile

# Must be set before any memory module captures DATA_DIR at import time.
os.environ.setdefault("RECONOPS_STUB_LLM", "1")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-stub-eval")
os.environ["RECONOPS_DATA_DIR"] = tempfile.mkdtemp(prefix="reconops_eval_")

from pathlib import Path  # noqa: E402

import pandas as pd  # noqa: E402

from .agent import run_job  # noqa: E402
from .memory import accounts, decision_log, rules_store, triage as triage_store  # noqa: E402
from .models import BindingSet, DecisionLogEntry, Rationale, ReconcileConfig, Rule  # noqa: E402
from .replay import replay  # noqa: E402
from .tools.binding import bind_columns  # noqa: E402

SAMPLES = Path(__file__).resolve().parents[2] / "samples"

ACCURACY_FLOOR = 0.95
OVERRIDE_CEIL = 0.05


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_pair(spec, account=None):
    da = pd.read_csv(SAMPLES / spec["file_a"])
    db = pd.read_csv(SAMPLES / spec["file_b"])
    if account is None:
        account = accounts.create_account()
        rules_store.seed_defaults(account.id)
    cfg = ReconcileConfig(
        recon_type=spec["label"], label_a=spec["label_a"], label_b=spec["label_b"],
        source_a=BindingSet(bindings=bind_columns(da)),
        source_b=BindingSet(bindings=bind_columns(db)),
    )
    out = run_job(account=account, df_a=da, df_b=db, cfg=cfg, job_id="eval")
    return account, out


def _summary_actual(out):
    fee = sum(1 for r in out.discrepancies if r.get("status") == "fee_offset")
    s = out.summary
    return {
        "total_a": s.total_a, "total_b": s.total_b, "matched": s.matched,
        "matched_pct": s.matched_pct, "unmatched_a": s.unmatched_a,
        "unmatched_b": s.unmatched_b, "discrepancies": s.discrepancies,
        "fuzzy_matches": s.fuzzy_matches, "fee_offset": fee,
        "total_discrepancy_value": s.total_discrepancy_value,
    }


def _sig(row):
    return triage_store.signature_for_matched(
        Rationale.model_validate(row["rationale"]), row
    )


# ---------------------------------------------------------------------------
# Suite 1 — snapshots
# ---------------------------------------------------------------------------

def snapshot_suite():
    failures = []
    for fname in ("samples_orders_expected.json", "samples_inventory_expected.json"):
        spec = json.loads((SAMPLES / fname).read_text(encoding="utf-8"))
        _, out = _run_pair(spec)
        actual = _summary_actual(out)
        exp = spec["expected"]
        mismatches = []
        for k, v in exp.items():
            av = actual.get(k)
            ok = abs(av - v) <= 0.01 if isinstance(v, float) else av == v
            if not ok:
                mismatches.append(f"{k}: expected {v}, got {av}")
        if mismatches:
            failures.append(f"[{spec['label']}] " + "; ".join(mismatches))
            print(f"  FAIL {spec['label']}: {'; '.join(mismatches)}")
        else:
            print(f"  PASS {spec['label']}: {actual['matched']} matched / "
                  f"{actual['discrepancies']} disc / ${actual['total_discrepancy_value']}")
    return failures


# ---------------------------------------------------------------------------
# Suite 2 — synthetic corrections + replay
# ---------------------------------------------------------------------------

def synthetic_suite(inject_bad_rule=False):
    failures = []
    spec = json.loads((SAMPLES / "samples_orders_expected.json").read_text(encoding="utf-8"))
    account, out = _run_pair(spec)

    # Build a decision log: the user audits and confirms rows as expected.
    fee_rows = [r for r in out.matched if r["rationale"]["status"] == "fee_offset"]
    match_rows = [r for r in out.matched if r["rationale"]["status"] == "match"]

    decisions = 0
    for r in fee_rows:  # ~16 fee confirmations
        decision_log.append(account.id, DecisionLogEntry(
            job_id="eval", row_key=r["key"], signature=_sig(r),
            original_status="fee_offset", user_status="expected",
            user_reason="standard processor fees",
        ))
        decisions += 1
    for r in match_rows[:20]:  # +20 clean-match confirmations → ≥30 total
        decision_log.append(account.id, DecisionLogEntry(
            job_id="eval", row_key=r["key"], signature=_sig(r),
            original_status="match", user_status="expected",
            user_reason="verified clean",
        ))
        decisions += 1

    if inject_bad_rule and match_rows:
        # A rule that pins a signature the user confirmed as "expected/match"
        # to "major" — directly contradicts recorded truth.
        bad_sig = _sig(match_rows[0])
        rules_store.add_rule(account.id, Rule(
            account_id=account.id, kind="force_status",
            description="(eval self-test) bad rule forcing clean rows to major",
            when={"signature_prefix": bad_sig}, then={"status": "major"},
            origin="eval_bad_rule", confidence=0.9, state="active",
        ))

    report = replay(account.id, out.matched)
    r = report.as_dict()
    print(f"  decisions={decisions} evaluated={r['evaluated']} "
          f"accuracy_before={r['accuracy_before']} accuracy_after={r['accuracy_after']} "
          f"override_rate={r['override_rate']} regressions={len(r['regressions'])}")

    # ≥30 simulated decisions is the volume floor; they dedup to a handful of
    # distinct signatures by design (that's TriageItem dedup) — accuracy is
    # measured per signature, so `evaluated` is small and that's expected.
    if decisions < 30:
        failures.append(f"synthetic suite logged only {decisions} (<30) decisions")
    if r["evaluated"] < 2:
        failures.append(f"synthetic suite evaluated only {r['evaluated']} (<2) distinct signatures")
    if r["accuracy_after"] < ACCURACY_FLOOR:
        failures.append(f"accuracy_after {r['accuracy_after']} < {ACCURACY_FLOOR}")
    if r["override_rate"] >= OVERRIDE_CEIL:
        failures.append(f"override_rate {r['override_rate']} >= {OVERRIDE_CEIL}")
    if r["regressions"]:
        for reg in r["regressions"][:5]:
            print(f"    REGRESSION {reg['signature'][:8]}… {reg['before']} -> {reg['after']} "
                  f"(user wanted {reg['user_status']})")
        failures.append(f"{len(r['regressions'])} regression(s) detected")
    return failures


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    inject_bad = "--inject-bad-rule" in argv

    print("Snapshot suite:")
    failures = snapshot_suite()
    print("Synthetic-corrections suite:")
    failures += synthetic_suite(inject_bad_rule=inject_bad)

    print()
    if failures:
        print(f"EVAL FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("EVAL PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
