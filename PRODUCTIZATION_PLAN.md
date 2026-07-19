# ReconOps AI — Productization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden the ReconOps AI pilot into a product that can be sold directly to business clients: safe on real data, bounded in cost, deterministic in output, and honest about what it suppresses.

**Architecture:** Keep the existing FastAPI + JSON-on-disk + React stack through Phases 0–1 (correctness and trust first), then migrate infrastructure in Phase 2 (auth, Postgres, async jobs) once behavior is pinned by tests and evals. Every classification-behavior change re-pins the eval snapshots in the same task.

**Tech Stack:** FastAPI, pandas, pydantic v2, Anthropic SDK, pytest (new), filelock (new), React + Vite, GitHub Actions (new).

**Conventions for all tasks:**
- Run backend commands from `backend/` with the venv active.
- Test command: `python -m pytest -q`. Eval command: `python -m app.eval` (exit 0 = pass).
- Commit after every task, using the message given in the task's final step.
- Tasks within a phase are ordered by dependency — execute top to bottom.

---

## Phase 0 — Safety net & hardening

Goal: nothing a user or a crash can do corrupts data; LLM spend is bounded; CI guards every commit.

### Task 1: pytest infrastructure + regression tests for matching/amounts

**Files:**
- Create: `backend/requirements-dev.txt`
- Create: `backend/pytest.ini`
- Create: `backend/tests/__init__.py` (empty)
- Create: `backend/tests/test_matching.py`
- Create: `backend/tests/test_amounts.py`

- [x] **Step 1: Add dev requirements and pytest config**

`backend/requirements-dev.txt`:
```
pytest==8.3.3
```

`backend/pytest.ini`:
```ini
[pytest]
pythonpath = .
testpaths = tests
```

Run: `pip install -r requirements-dev.txt`

- [x] **Step 2: Write matching regression tests**

`backend/tests/test_matching.py`:
```python
import pandas as pd

from app.tools.matching import match_by_key, norm_key


def test_norm_key_strips_prefix_and_leading_zeros():
    assert norm_key("#1001") == "1001"
    assert norm_key("ORD-0042") == "42"
    assert norm_key("pi_ABC123") == "abc123"


def test_norm_key_all_zeros_survives():
    # lstrip("0") on "000" yields "", falls back to the lowercased original
    assert norm_key("000") == "000"


def test_exact_match_preferred_over_fuzzy():
    a = pd.DataFrame({"k": ["#1001"]})
    b = pd.DataFrame({"k": ["#1001", "1001"]})
    res = match_by_key(a, b, "k", "k")
    assert len(res.matches) == 1
    assert res.matches[0].match_type == "exact"
    assert res.matches[0].key_b == "#1001"


def test_duplicate_keys_first_wins_rest_unmatched():
    # Documents current 1:1 behavior — Task 10 changes this via aggregation
    a = pd.DataFrame({"k": ["1", "1"], "v": [10, 20]})
    b = pd.DataFrame({"k": ["1"], "v": [10]})
    res = match_by_key(a, b, "k", "k")
    assert len(res.matches) == 1
    assert res.unmatched_a_idx == [1]


def test_no_cross_matching_of_unrelated_keys():
    a = pd.DataFrame({"k": ["A1", "B2"]})
    b = pd.DataFrame({"k": ["C3", "D4"]})
    res = match_by_key(a, b, "k", "k")
    assert res.matches == []
    assert len(res.unmatched_a_idx) == 2
    assert len(res.unmatched_b_idx) == 2
```

- [x] **Step 3: Write amounts regression tests**

`backend/tests/test_amounts.py`:
```python
import pandas as pd

from app.tools.amounts import classify_amount_diff, coerce_amount


def test_coerce_amount_currency_symbols_and_parens():
    s = pd.Series(["$1,234.56", "(100)", "€50.00", "abc"])
    out = coerce_amount(s)
    assert out.iloc[0] == 1234.56
    assert out.iloc[1] == -100.0
    assert out.iloc[2] == 50.0
    assert pd.isna(out.iloc[3])


def test_within_tolerance_is_match():
    status, conf, ev, alts = classify_amount_diff(0.005, 0.00005, 100.0, 99.995, 0.01, 0.005)
    assert status == "match"


def test_stripe_fee_shape_detected():
    a = 100.00
    b = round(a - (a * 0.029 + 0.30), 2)  # 96.80
    status, conf, ev, alts = classify_amount_diff(a - b, (a - b) / a, a, b, 0.01, 0.005)
    assert status == "fee_offset"


def test_major_threshold():
    status, conf, ev, alts = classify_amount_diff(150.0, 0.15, 1000.0, 850.0, 0.01, 0.005)
    assert status == "major"
```

- [x] **Step 4: Run tests, confirm all pass**

Run: `python -m pytest -q`
Expected: all pass (these pin existing behavior; a failure means you found a live bug — fix the test's expectation only if the current behavior is genuinely correct).

- [x] **Step 5: Commit**

```bash
git add backend/requirements-dev.txt backend/pytest.ini backend/tests/
git commit -m "test: pin matching and amount-classification behavior with pytest"
```

---

### Task 2: Atomic writes + per-account file locks

Every store does unlocked read-modify-write with truncating writes. One concurrent request or crash mid-write corrupts `rules.json` — and a corrupted rules file 500s every future job for that account.

**Files:**
- Create: `backend/app/memory/fsutil.py`
- Modify: `backend/requirements.txt` (add `filelock==3.16.1`)
- Modify: `backend/app/memory/rules_store.py:80-83` (`_save_raw`), wrap `add_rule`/`update_rule` in lock
- Modify: `backend/app/memory/triage.py:124-127` (`_save_raw`), wrap `emit_for_job`/`resolve` in lock
- Modify: `backend/app/memory/learned_aliases.py:54-57` (`_save`), wrap `upsert` in lock
- Modify: `backend/app/memory/accounts.py:46-50,63-71` (profile writes)
- Modify: `backend/app/storage.py:27-29` (`save_job`)
- Modify: `backend/app/memory/decision_log.py:27`, `notes.py:25`, `observations.py:29` (lock around JSONL appends)
- Test: `backend/tests/test_fsutil.py`

- [x] **Step 1: Write the failing test**

`backend/tests/test_fsutil.py`:
```python
import json
import threading

import pytest


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("RECONOPS_DATA_DIR", str(tmp_path))
    return tmp_path


def test_atomic_write_json_replaces_not_truncates(data_dir):
    from app.memory.fsutil import atomic_write_json
    target = data_dir / "x.json"
    atomic_write_json(target, {"a": 1})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}
    # No stray temp files left behind
    assert list(data_dir.glob("*.tmp")) == []


def test_concurrent_add_rule_loses_no_writes(data_dir, monkeypatch):
    # Re-import with patched DATA_DIR (module-level constant)
    import importlib
    from app.memory import rules_store
    importlib.reload(rules_store)
    from app.models import Rule

    account_id = "11111111-1111-4111-8111-111111111111"
    n = 25

    def add(i):
        rules_store.add_rule(account_id, Rule(
            account_id=account_id, kind="custom",
            description=f"rule-{i}", when={}, then={},
            origin="test", confidence=0.5, state="pending",
        ))

    threads = [threading.Thread(target=add, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    rules = rules_store.load_rules(account_id)
    assert len(rules) == n  # unlocked read-modify-write loses writes here
```

- [x] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_fsutil.py -v`
Expected: `test_concurrent_add_rule_loses_no_writes` FAILS (fewer than 25 rules survive) and the fsutil import fails (module doesn't exist yet).

- [x] **Step 3: Implement fsutil**

Add `filelock==3.16.1` to `backend/requirements.txt`, run `pip install -r requirements.txt`.

`backend/app/memory/fsutil.py`:
```python
"""Atomic JSON persistence + per-account advisory locking.

Every store that does read-modify-write on a per-account file must:
  1. hold `account_lock(account_id)` across the read AND the write, and
  2. persist via `atomic_write_json` (write temp file, then os.replace)
so a crash mid-write can never leave a truncated file, and two concurrent
requests can never interleave a lost update.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from filelock import FileLock

DATA_DIR = Path(os.getenv("RECONOPS_DATA_DIR", "data"))


def atomic_write_json(path: Path, payload: Any, indent: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(payload, default=str, indent=indent))
        os.replace(tmp, path)  # atomic on same volume, incl. Windows
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def account_lock(account_id: str, timeout: float = 10.0) -> FileLock:
    lock_dir = DATA_DIR / "accounts" / account_id
    lock_dir.mkdir(parents=True, exist_ok=True)
    return FileLock(str(lock_dir / ".lock"), timeout=timeout)
```

- [x] **Step 4: Convert the whole-file writers**

In `rules_store.py`, replace `_save_raw` body and wrap the mutators:
```python
from .fsutil import account_lock, atomic_write_json

def _save_raw(account_id: str, payload: Dict[str, Any]) -> None:
    atomic_write_json(_rules_path(account_id), payload, indent=2)

def add_rule(account_id: str, rule: Rule) -> Rule:
    with account_lock(account_id):
        rules = load_rules(account_id)
        rules.append(rule)
        save_rules(account_id, rules)
    return rule
```
Apply the identical pattern (lock across load→mutate→save, atomic write inside):
- `rules_store.update_rule` (lock around its existing body)
- `triage._save_raw` → `atomic_write_json`; `emit_for_job` and `resolve` bodies wrapped in `with account_lock(account_id):`
- `learned_aliases._save` → `atomic_write_json`; `upsert` wrapped in lock
- `accounts.create_account` / `accounts.update_profile` → write profile via `atomic_write_json(_profile_path(...), json.loads(acc.model_dump_json()), indent=2)`
- `storage.save_job` → `atomic_write_json(job_path(job_id), payload)` (no account lock needed — job ids are single-writer)

- [x] **Step 5: Lock the JSONL appenders**

In `decision_log.append`, `notes.append`, `observations.append`, wrap the existing `with p.open("a", ...)` block in `with account_lock(account_id):` (import from `.fsutil`). Appends stay append-mode; the lock serializes them across requests.

- [x] **Step 6: Run tests + eval**

Run: `python -m pytest -q` → all pass, including the 25-thread test.
Run: `python -m app.eval` → exit 0 (no behavior change).

- [x] **Step 7: Commit**

```bash
git add backend/app/memory/fsutil.py backend/app/memory/*.py backend/app/storage.py backend/requirements.txt backend/tests/test_fsutil.py
git commit -m "fix: atomic writes + per-account locks for all JSON stores"
```

---

### Task 3: GitHub Actions CI

**Files:**
- Create: `.github/workflows/ci.yml`

- [x] **Step 1: Write the workflow**

`.github/workflows/ci.yml`:
```yaml
name: CI
on:
  push:
    branches: [main]
  pull_request:

jobs:
  backend:
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: backend
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
          cache: pip
      - run: pip install -r requirements.txt -r requirements-dev.txt
      - name: Unit tests
        run: python -m pytest -q
      - name: Eval (snapshot + synthetic corrections)
        run: python -m app.eval
      - name: Eval self-test — injected bad rule must be caught
        run: "! python -m app.eval --inject-bad-rule"

  frontend-build:
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: frontend
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: 20
          cache: npm
          cache-dependency-path: frontend/package-lock.json
      - run: npm ci
      - run: npm run build
```

- [x] **Step 2: Commit, push, verify green**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: run pytest + eval + frontend build on every push"
git push
```
Then check: `gh run watch` (or the Actions tab) — both jobs must pass before continuing.

---

### Task 4: Single source of truth for the data directory

`DATA_DIR = Path(os.getenv("RECONOPS_DATA_DIR", "data"))` is duplicated in 8+ modules and resolves relative to the process CWD — running uvicorn from the repo root vs `backend/` silently splits your data into two trees (this already happened: both `data/` and `backend/data/` exist).

**Files:**
- Create: `backend/app/config.py`
- Modify: `backend/app/storage.py:10`, `backend/app/llm.py:18`, `backend/app/memory/fsutil.py`, `backend/app/memory/{accounts,rules_store,triage,metrics,decision_log,notes,observations,learned_aliases}.py` (each module's `DATA_DIR` line)

- [x] **Step 1: Create config module**

`backend/app/config.py`:
```python
"""Central config. DATA_DIR resolves to backend/data regardless of CWD;
RECONOPS_DATA_DIR overrides (eval sets it to a temp dir before import)."""
import os
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parents[1]

DATA_DIR = Path(os.getenv("RECONOPS_DATA_DIR") or (_BACKEND_ROOT / "data"))
```

- [x] **Step 2: Point every module at it**

In each listed file, replace the local `DATA_DIR = Path(os.getenv("RECONOPS_DATA_DIR", "data"))` with `from ..config import DATA_DIR` (memory modules) / `from .config import DATA_DIR` (storage.py, llm.py — for llm.py derive `USAGE_LOG_PATH = DATA_DIR / "llm_usage.jsonl"`). In `storage.py` keep `JOBS_DIR`/`UPLOADS_DIR` derived from the imported `DATA_DIR`.

Note: `app/eval.py` sets `RECONOPS_DATA_DIR` in `os.environ` *before* importing app modules — that ordering keeps working because config reads the env var at import time. Do not import `app.config` at the top of `eval.py` before the env assignment.

- [x] **Step 3: Migrate stray data**

If `<repo>/data/` (root) contains account dirs, move its contents into `backend/data/` and delete the root `data/` folder.

- [x] **Step 4: Verify + commit**

Run: `python -m pytest -q && python -m app.eval` → both pass.
Run: `uvicorn app.main:app --port 8000` from `backend/`, hit `GET /api/health`, confirm no new `data/` folder appears anywhere but `backend/data/`.

```bash
git add backend/app/config.py backend/app/*.py backend/app/memory/*.py
git commit -m "refactor: single DATA_DIR anchored to backend/, not CWD"
```

---

### Task 5: LLM cost control — capped, batched, advisory, cheap model

Today every "minor" row (deterministic confidence 0.70 < threshold 0.75) triggers one sequential LLM call ([classify.py:23](backend/app/tools/classify.py)), and the LLM may flip the verdict — unbounded cost, unbounded latency, non-deterministic output. Change to: deterministic verdicts always stand; one batched call reviews the top-N discrepancies by $ impact and contributes *advisory* evidence only.

**Files:**
- Modify: `backend/app/tools/classify.py` (add `batch_second_opinions`, keep `propose_classification` deterministic-only)
- Modify: `backend/app/agent.py:229-315` (row loop calls `allow_llm=False`; single batch pass after the loop)
- Modify: `backend/app/llm.py:136-162` (`_stub_response` branch for the batch tool)
- Modify: `backend/.env.example` (document new knobs)
- Test: `backend/tests/test_classify_batch.py`

- [x] **Step 1: Write the failing test**

`backend/tests/test_classify_batch.py`:
```python
import os


def test_batch_respects_cap_and_is_advisory(monkeypatch):
    monkeypatch.setenv("RECONOPS_STUB_LLM", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-stub")
    monkeypatch.setenv("RECONOPS_MAX_LLM_ROWS", "2")

    from app.models import Evidence, Rationale
    from app.tools.classify import batch_second_opinions

    def mk(key, diff):
        return {
            "rationale": Rationale(
                row_key=key, status="minor", confidence=0.70,
                rationale=[Evidence(source="threshold_minor", evidence="x")],
                alternatives=[],
            ),
            "row_ctx": {"key": key, "amount_a": 100.0, "amount_b": 100.0 - diff,
                        "diff_abs": diff, "diff_pct": diff, "match_type": "exact"},
        }

    candidates = [mk("small", 1.0), mk("big", 50.0), mk("mid", 10.0)]
    reviewed = batch_second_opinions(
        candidates=candidates, account_id="acc", job_id="job",
    )
    # Cap: only the top 2 by |diff_abs| were sent
    assert set(reviewed) <= {"big", "mid"}
    # Advisory: statuses unchanged on every candidate
    for c in candidates:
        assert c["rationale"].status == "minor"
```

- [x] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_classify_batch.py -v`
Expected: FAIL — `batch_second_opinions` doesn't exist.

- [x] **Step 3: Implement the batch reviewer**

In `backend/app/tools/classify.py` add:
```python
import os

BATCH_SYSTEM_PROMPT = (
    "You are a senior operations analyst reviewing reconciliation discrepancies "
    "between two systems for a small e-commerce brand. You receive a JSON array "
    "of rows, each with the deterministic classifier's verdict and the numbers. "
    "For EACH row return an object:\n"
    '{"key": <same key>, "agrees": true|false, '
    '"note": "one short sentence citing the numbers", "confidence": 0.0..1.0}\n'
    "Respond with a JSON array only, same order as the input. No other text."
)


def batch_second_opinions(
    *,
    candidates: List[Dict[str, Any]],   # [{"rationale": Rationale, "row_ctx": {...}}]
    account_id: str,
    job_id: Optional[str],
) -> Dict[str, Dict[str, Any]]:
    """One LLM call reviewing the top-N candidates by |diff_abs|.

    Appends advisory Evidence to each reviewed candidate's Rationale IN PLACE
    (never flips status — determinism is the product guarantee). Returns
    {row_key: llm_item} for the rows actually reviewed.
    """
    max_rows = int(os.getenv("RECONOPS_MAX_LLM_ROWS", "25"))
    row_model = os.getenv("ANTHROPIC_ROW_MODEL", "claude-haiku-4-5")
    if not candidates or max_rows <= 0 or not is_configured():
        return {}

    ranked = sorted(candidates,
                    key=lambda c: abs(c["row_ctx"].get("diff_abs") or 0),
                    reverse=True)[:max_rows]
    payload = [{
        "key": c["row_ctx"].get("key"),
        "amount_a": c["row_ctx"].get("amount_a"),
        "amount_b": c["row_ctx"].get("amount_b"),
        "diff_abs": c["row_ctx"].get("diff_abs"),
        "diff_pct": c["row_ctx"].get("diff_pct"),
        "match_type": c["row_ctx"].get("match_type"),
        "verdict": {"status": c["rationale"].status,
                    "confidence": c["rationale"].confidence,
                    "evidence": [e.evidence for e in c["rationale"].rationale]},
    } for c in ranked]

    try:
        data = call_claude_json(
            tool_name="batch_second_opinions",
            account_id=account_id, job_id=job_id,
            system=BATCH_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(payload, default=str)}],
            max_tokens=120 * len(ranked),
            model=row_model,
        )
    except Exception:
        return {}  # advisory pass — failure must never fail the job

    if not isinstance(data, list):
        return {}
    by_key = {str(item.get("key")): item for item in data if isinstance(item, dict)}
    reviewed: Dict[str, Dict[str, Any]] = {}
    for c in ranked:
        key = str(c["row_ctx"].get("key"))
        item = by_key.get(key)
        if not item or not (item.get("note") or "").strip():
            continue
        verb = "confirms" if item.get("agrees", True) else "questions"
        c["rationale"].rationale.append(Evidence(
            source="llm_second_opinion",
            evidence=f"AI review {verb} the verdict: {item['note'].strip()} "
                     f"(confidence {float(item.get('confidence', 0.6)):.2f})",
            weight=0.3,
        ))
        reviewed[key] = item
    return reviewed
```

In `propose_classification`, delete the entire per-row escalation block (the `if allow_llm and confidence < ESCALATION_THRESHOLD ...` section, lines 114–155) — the function becomes purely deterministic. Keep the `allow_llm` parameter for signature stability.

- [x] **Step 4: Wire into the agent**

In `backend/app/agent.py` step 4 (row loop): collect candidates instead of escalating inline —
```python
escalation_candidates: List[Dict[str, Any]] = []
...
# inside the loop, where propose_classification is called:
rationale = propose_classification(
    row_ctx=row_ctx, tol_abs=tol_abs, tol_pct=tol_pct,
    account_id=account.id, job_id=job_id, allow_llm=False,
)
if rationale.status != "match" and rationale.confidence < 0.75:
    escalation_candidates.append({"rationale": rationale, "row_ctx": row_ctx})
```
After the loop (before Step 5 unmatched handling), run the single batch pass and count it:
```python
from .tools.classify import batch_second_opinions
reviewed = batch_second_opinions(
    candidates=escalation_candidates, account_id=account.id, job_id=job_id,
)
if reviewed:
    llm_calls_made += 1
```
The `record` dicts hold `rationale.model_dump()` — build records *after* the batch pass, or re-serialize: simplest is to keep `(record, rationale)` pairs during the loop and set `record["rationale"] = rationale.model_dump()` after the batch pass.

- [x] **Step 5: Stub support**

In `backend/app/llm.py` `_stub_response`, add before the final `else`:
```python
    elif tool_name == "batch_second_opinions":
        text = "[]"
```

- [x] **Step 6: Document the knobs**

Append to `backend/.env.example`:
```
# Max discrepancy rows sent for AI second-opinion per job (0 disables)
RECONOPS_MAX_LLM_ROWS=25
# Cheaper model for row-level review; the summary keeps ANTHROPIC_MODEL
ANTHROPIC_ROW_MODEL=claude-haiku-4-5
```

- [x] **Step 7: Run tests + eval, re-pin snapshots if needed**

Run: `python -m pytest -q` → pass.
Run: `python -m app.eval` — statuses are now purely deterministic; if the pinned summaries in `samples/samples_*_expected.json` differ (the old stub could flip verdicts), inspect the diff, update the expected JSON to the new deterministic values, and re-run to green.

- [x] **Step 8: Commit**

```bash
git add backend/app/tools/classify.py backend/app/agent.py backend/app/llm.py backend/.env.example backend/tests/test_classify_batch.py samples/
git commit -m "feat: cap+batch LLM review of top discrepancies; verdicts fully deterministic"
```

---

### Task 6: Graceful degradation when the LLM is unavailable

An Anthropic outage currently takes the whole product down (503 on upload), even though matching/classification/export are deterministic. Degrade instead: finish the job, mark insights unavailable.

**Files:**
- Modify: `backend/app/agent.py:368-375` (wrap insights call), `backend/app/agent.py:87-102` (AgentOutput field)
- Modify: `backend/app/main.py:180-185` (remove the 503 gate), `main.py:239-255` (persist `insights_status`)
- Modify: `frontend/src/pages/ResultsPage.jsx` (banner when insights missing)
- Test: `backend/tests/test_degradation.py`

- [x] **Step 1: Write the failing test**

`backend/tests/test_degradation.py`:
```python
import pandas as pd


def test_job_completes_without_api_key(monkeypatch, tmp_path):
    monkeypatch.setenv("RECONOPS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("RECONOPS_STUB_LLM", raising=False)

    import importlib
    from app.memory import accounts, rules_store
    importlib.reload(accounts); importlib.reload(rules_store)
    from app.agent import run_job
    from app.models import BindingSet, ReconcileConfig
    from app.tools.binding import bind_columns

    da = pd.DataFrame({"order_id": ["#1", "#2"], "order_total": [10.0, 20.0]})
    db = pd.DataFrame({"order_reference": ["#1", "#2"], "amount": [10.0, 15.0]})
    acc = accounts.create_account()
    rules_store.seed_defaults(acc.id)
    cfg = ReconcileConfig(
        source_a=BindingSet(bindings=bind_columns(da)),
        source_b=BindingSet(bindings=bind_columns(db)),
    )
    out = run_job(account=acc, df_a=da, df_b=db, cfg=cfg, job_id="t")
    assert out.summary.matched == 2
    assert out.insights_status == "unavailable"
```

- [x] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_degradation.py -v`
Expected: FAIL — `run_job` raises `LLMUnavailable` (and `insights_status` doesn't exist).

- [x] **Step 3: Implement**

`AgentOutput` gains `insights_status: str = "ok"`. Replace agent step 8 with:
```python
    try:
        insights = _synthesize_insights(
            summary=summary, label_a=label_a, label_b=label_b,
            discrepancies=discrepancy_rows, timing=timing,
            account=account, job_id=job_id,
        )
        llm_calls_made += 1
        insights_status = "ok"
    except Exception:
        insights = ""
        insights_status = "unavailable"
```
Set `insights_status=insights_status` in the returned `AgentOutput`.

In `main.py`: delete the `if not is_configured(): raise HTTPException(503 ...)` gate in `upload_and_reconcile` and the `except LLMUnavailable` handler (no longer raised); add `"insights_status": result.insights_status` to the payload.

- [x] **Step 4: Frontend banner**

In `ResultsPage.jsx`, where insights render, show a dismissable amber banner when `job.insights_status === "unavailable"`:
`"AI summary unavailable for this run — matching and classification below are complete and deterministic."`

- [x] **Step 5: Verify + commit**

Run: `python -m pytest -q && python -m app.eval` → pass.

```bash
git add backend/app/agent.py backend/app/main.py frontend/src/pages/ResultsPage.jsx backend/tests/test_degradation.py
git commit -m "feat: jobs complete deterministically when LLM is unavailable"
```

---

### Task 7: Remove the awaiting_user dead end

`POST /api/jobs/{id}/answer` returns 501 and uploaded files aren't persisted, so paused jobs can never resume. Replace the pause with proceed-plus-warning.

**Files:**
- Modify: `backend/app/agent.py:74-81,196-212` (delete `AskUser`; emit `binding_warning` instead)
- Modify: `backend/app/main.py:16,218-231,260-296` (drop the `AskUser` handler and the `/answer` endpoint)
- Modify: `frontend/src/pages/ResultsPage.jsx` (warning banner)
- Test: extend `backend/tests/test_degradation.py`

- [x] **Step 1: Write the failing test**

Append to `backend/tests/test_degradation.py`:
```python
def test_low_confidence_join_proceeds_with_warning(monkeypatch, tmp_path):
    monkeypatch.setenv("RECONOPS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RECONOPS_STUB_LLM", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-stub")

    import importlib
    from app.memory import accounts, rules_store
    importlib.reload(accounts); importlib.reload(rules_store)
    from app.agent import run_job
    from app.models import BindingSet, ReconcileConfig, SemanticBinding

    import pandas as pd
    da = pd.DataFrame({"ref": ["x1", "x2"], "total": [10.0, 20.0]})
    db = pd.DataFrame({"memo": ["x1", "zz"], "amount": [10.0, 5.0]})
    # Force low-confidence key bindings on both sides
    cfg = ReconcileConfig(
        source_a=BindingSet(bindings=[SemanticBinding(
            column_name="ref", concept_id="order.id", confidence=0.3,
            provenance="inferred", evidence=[], alternatives=[])]),
        source_b=BindingSet(bindings=[SemanticBinding(
            column_name="memo", concept_id="payment.order_reference", confidence=0.3,
            provenance="inferred", evidence=[], alternatives=[])]),
    )
    acc = accounts.create_account()
    rules_store.seed_defaults(acc.id)
    out = run_job(account=acc, df_a=da, df_b=db, cfg=cfg, job_id="t2")
    assert out.binding_warning is not None
    assert out.summary.matched >= 1  # it proceeded anyway
```

- [x] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_degradation.py -v`
Expected: FAIL — `run_job` raises `AskUser` / `binding_warning` doesn't exist.

- [x] **Step 3: Implement**

In `agent.py`: delete the `AskUser` class. `AgentOutput` gains `binding_warning: Optional[Dict[str, Any]] = None`. Replace the `raise AskUser(...)` block with:
```python
    binding_warning = None
    if min(key_a.confidence, key_b.confidence) < ASK_USER_BINDING_THRESHOLD and key_overlap < 0.5:
        binding_warning = {
            "message": (
                f"Low confidence in the join: '{key_a.column_name}' (A) ↔ "
                f"'{key_b.column_name}' (B), value overlap {key_overlap*100:.0f}%. "
                "Results below assume this join — re-upload with corrected "
                "column bindings if it looks wrong."
            ),
            "proposed_a": key_a.column_name,
            "proposed_b": key_b.column_name,
            "overlap": round(key_overlap, 3),
        }
```
Set it on the returned `AgentOutput`.

In `main.py`: remove `AskUser` from the import, delete the `except AskUser` block and the whole `answer_pending_question` endpoint; add `"binding_warning": result.binding_warning` to the payload.

In `ResultsPage.jsx`: render `job.binding_warning.message` in the same banner style as Task 6 when present.

- [x] **Step 4: Verify + commit**

Run: `python -m pytest -q && python -m app.eval` → pass.
Run: `grep -rn "awaiting_user\|pending_question" backend/app frontend/src` → remaining hits should only be in `_load_job_for_account`-adjacent legacy read paths (old persisted jobs may still carry the fields); remove any frontend rendering of them.

```bash
git add backend/app/agent.py backend/app/main.py frontend/src/pages/ResultsPage.jsx backend/tests/test_degradation.py
git commit -m "feat: replace awaiting_user dead-end with proceed-plus-warning"
```

---

### Task 8: Server-side job history + enforced retention

Job history lives only in browser localStorage; retention cleanup only runs at process startup.

**Files:**
- Modify: `backend/app/storage.py` (add `list_jobs`)
- Modify: `backend/app/main.py` (add `GET /api/jobs`; call `storage.cleanup()` in upload)
- Modify: `frontend/src/pages/HistoryPage.jsx`, `frontend/src/api/client.js`
- Test: `backend/tests/test_storage.py`

- [x] **Step 1: Write the failing test**

`backend/tests/test_storage.py`:
```python
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
```

- [x] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_storage.py -v` → FAIL: no `list_jobs`.

- [x] **Step 3: Implement**

In `storage.py`:
```python
def list_jobs(account_id: str, limit: int = 50) -> list:
    ensure_dirs()
    out = []
    for p in JOBS_DIR.glob("*.json"):
        try:
            job = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if job.get("account_id") != account_id:
            continue
        s = job.get("summary") or {}
        out.append({
            "job_id": job.get("job_id"),
            "created_at": job.get("created_at"),
            "status": job.get("status", "complete"),
            "filenames": job.get("filenames"),
            "matched_pct": s.get("matched_pct"),
            "discrepancies": s.get("discrepancies"),
            "total_discrepancy_value": s.get("total_discrepancy_value"),
        })
    out.sort(key=lambda j: j.get("created_at") or "", reverse=True)
    return out[:limit]
```
(O(n) file scan is fine at pilot scale; Postgres replaces it in Phase 2.)

In `main.py`:
```python
@app.get("/api/jobs")
def list_jobs_endpoint(account: Account = Depends(require_account)):
    return {"jobs": storage.list_jobs(account.id)}
```
and add `storage.cleanup()` as the first line of `upload_and_reconcile` (cheap directory scan; enforces the 24h/7d retention promise on live servers).

- [x] **Step 4: Frontend**

`client.js`: add `export async function getJobs() { return handle(await accountFetch(`${BASE}/api/jobs`)); }`
`HistoryPage.jsx`: fetch `getJobs()` on mount and render the server list as the primary source; keep the localStorage list only as a fallback when the request fails.

- [x] **Step 5: Verify + commit**

Run: `python -m pytest -q` → pass. Manual: run backend + frontend, upload a sample pair, confirm History shows the job in a fresh incognito window *after* adopting the same account via `?account=<uuid>`.

```bash
git add backend/app/storage.py backend/app/main.py frontend/src/pages/HistoryPage.jsx frontend/src/api/client.js backend/tests/test_storage.py
git commit -m "feat: server-side job history + retention enforced per upload"
```

---

## Phase 1 — Correctness on real data

Goal: the Olist datasets (and later real Stripe/Shopify exports) reconcile credibly; learned rules can't silently hide real money.

### Task 9: Olist-derived eval fixtures

Olist's true reconciliation pair: **sum(order_items.price + freight_value) per order** vs **sum(order_payments.payment_value) per order** — many-to-one on both sides, BRL, ~99k orders, with natural discrepancies (vouchers, rounding, canceled orders). Source CSVs exceed the 10MB upload cap, so derive sampled pairs.

**Files:**
- Create: `samples/build_olist_pair.py`
- Modify: `.gitignore` (add `samples/olist/` — derived from CC BY-NC-SA data, must not be committed to a public repo)

- [x] **Step 1: Write the derivation script**

`samples/build_olist_pair.py`:
```python
"""Derive two-sided reconciliation pairs from the Olist Kaggle dataset.

Reads  samples/Kaggle/Olist_datasets/  (never committed - CC BY-NC-SA 4.0)
Writes samples/olist/                  (gitignored, derived data)

  olist_order_totals.csv  - order_id, order_total, order_purchase_timestamp, order_status
  olist_payments_raw.csv  - order_id, payment_value, payment_type, payment_sequential
                            (deliberately NOT aggregated: exercises many-to-one)

Run: python samples/build_olist_pair.py [n_orders]
"""
import sys
from pathlib import Path

import pandas as pd

SRC = Path(__file__).parent / "Kaggle" / "Olist_datasets"
OUT = Path(__file__).parent / "olist"
N_ORDERS = int(sys.argv[1]) if len(sys.argv) > 1 else 20000

OUT.mkdir(exist_ok=True)

items = pd.read_csv(SRC / "olist_order_items_dataset.csv",
                    usecols=["order_id", "price", "freight_value"])
orders = pd.read_csv(SRC / "olist_orders_dataset.csv",
                     usecols=["order_id", "order_purchase_timestamp", "order_status"])
pay = pd.read_csv(SRC / "olist_order_payments_dataset.csv")

totals = (items.groupby("order_id", as_index=False)
               .agg(items_total=("price", "sum"), freight=("freight_value", "sum")))
totals["order_total"] = (totals["items_total"] + totals["freight"]).round(2)
totals = (totals[["order_id", "order_total"]]
          .merge(orders, on="order_id", how="inner"))

sampled = totals.sample(n=min(N_ORDERS, len(totals)), random_state=42)
sampled.to_csv(OUT / "olist_order_totals.csv", index=False)
pay[pay["order_id"].isin(sampled["order_id"])].to_csv(
    OUT / "olist_payments_raw.csv", index=False)

print(f"orders: {len(sampled)} rows -> {OUT/'olist_order_totals.csv'}")
print(f"payments: {(pay['order_id'].isin(sampled['order_id'])).sum()} rows")
```

- [x] **Step 2: Run it and inspect**

Run: `python samples/build_olist_pair.py`
Expected: two CSVs under `samples/olist/`, each well under 10MB. Spot-check one order with `payment_sequential > 1` — its payments should sum to (approximately) the order total.

- [x] **Step 3: Gitignore the output + commit the script**

Add `samples/olist/` to `.gitignore`.

```bash
git add samples/build_olist_pair.py .gitignore
git commit -m "feat: Olist-derived reconciliation pair builder (fixtures gitignored)"
```

Note: uploading this pair through the UI **will look broken until Task 10 lands** — ~3% of orders have multiple payment rows that 1:1 matching strands as unmatched. That is the point.

---

### Task 10: Many-to-one matching via duplicate-key aggregation

**Files:**
- Modify: `backend/app/tools/matching.py` (add `aggregate_duplicate_keys`)
- Modify: `backend/app/agent.py:217-227` (aggregate after amount coercion, before matching)
- Modify: `backend/app/models.py` (Summary gains `aggregated_a: int = 0`, `aggregated_b: int = 0`)
- Test: `backend/tests/test_aggregation.py`

- [x] **Step 1: Write the failing test**

`backend/tests/test_aggregation.py`:
```python
import pandas as pd

from app.tools.matching import aggregate_duplicate_keys


def test_duplicate_keys_sum_amounts():
    df = pd.DataFrame({
        "order_id": ["o1", "o1", "o2"],
        "_amt": [60.0, 39.33, 20.0],
    })
    out, info = aggregate_duplicate_keys(df, "order_id")
    assert len(out) == 2
    assert out.loc[out["order_id"] == "o1", "_amt"].iloc[0] == 99.33
    assert out.loc[out["order_id"] == "o1", "_agg_count"].iloc[0] == 2
    assert info.groups == 1
    assert info.rows_collapsed == 1


def test_no_duplicates_is_passthrough():
    df = pd.DataFrame({"order_id": ["o1", "o2"], "_amt": [1.0, 2.0]})
    out, info = aggregate_duplicate_keys(df, "order_id")
    assert info is None
    assert len(out) == 2
```

- [x] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_aggregation.py -v` → FAIL: function doesn't exist.

- [x] **Step 3: Implement**

In `matching.py`:
```python
@dataclass
class AggregationInfo:
    groups: int          # normalized keys that had >1 row
    rows_collapsed: int  # extra rows folded into their group's first row


def aggregate_duplicate_keys(df: pd.DataFrame, key_col: str):
    """Collapse rows sharing a normalized key: `_amt` is summed, the first
    row's other fields are kept for display, `_agg_count` records group size.
    Dates keep the first row's value. Returns (df, AggregationInfo|None)."""
    norm = df[key_col].astype(str).str.strip().map(norm_key)
    sizes = norm.map(norm.value_counts())
    if (sizes <= 1).all():
        return df, None
    work = df.copy()
    work["_norm_key"] = norm
    work["_agg_count"] = sizes.values
    summed = work.groupby("_norm_key")["_amt"].transform("sum")
    first_mask = ~work["_norm_key"].duplicated(keep="first")
    work.loc[first_mask, "_amt"] = summed[first_mask]
    out = work[first_mask].drop(columns=["_norm_key"])
    return out, AggregationInfo(
        groups=int((work.loc[first_mask, "_agg_count"] > 1).sum()),
        rows_collapsed=int((~first_mask).sum()),
    )
```

In `agent.py`, immediately after the `_amt`/`_date` coercion (Step 2):
```python
    from .tools.matching import aggregate_duplicate_keys
    a, agg_a = aggregate_duplicate_keys(a, key_a.column_name)
    b, agg_b = aggregate_duplicate_keys(b, key_b.column_name)
```
Add to the `Summary(...)` construction: `aggregated_a=(agg_a.rows_collapsed if agg_a else 0), aggregated_b=(agg_b.rows_collapsed if agg_b else 0)`. Add `_agg_count` to the `drop_internal` list so unmatched-row dicts stay clean, but copy it onto matched `record` dicts (`"agg_count": int(row_b.get("_agg_count", 1))`) so the UI can show "3 payments summed".

- [x] **Step 4: Verify on Olist + re-pin eval**

Run: `python -m pytest -q` → pass.
Run: `python -m app.eval` — the bundled samples have no duplicate keys, so snapshots should be unchanged; if they differ, stop and investigate before re-pinning.
Manual: upload `samples/olist/olist_order_totals.csv` + `olist_payments_raw.csv`; multi-payment orders must now match, and remaining discrepancies should be genuine (vouchers, canceled orders).

- [x] **Step 5: Commit**

```bash
git add backend/app/tools/matching.py backend/app/agent.py backend/app/models.py backend/tests/test_aggregation.py
git commit -m "feat: many-to-one matching via normalized-key aggregation"
```

---

### Task 11: Mixed-currency guard

**Files:**
- Modify: `backend/app/tools/amounts.py` (add `detect_currency_tokens`)
- Modify: `backend/app/agent.py` (guard before coercion), `backend/app/models.py` (`ReconcileConfig.allow_mixed_currency: bool = False`)
- Test: `backend/tests/test_currency.py`

- [x] **Step 1: Write the failing test**

`backend/tests/test_currency.py`:
```python
import pandas as pd

from app.tools.amounts import detect_currency_tokens


def test_symbols_normalize_to_iso():
    assert detect_currency_tokens(pd.Series(["$10.00", "$20.00"])) == {"USD"}
    assert detect_currency_tokens(pd.Series(["€10,00"])) == {"EUR"}
    assert detect_currency_tokens(pd.Series(["R$ 99,33"])) == {"BRL"}
    assert detect_currency_tokens(pd.Series(["10.00 GBP"])) == {"GBP"}


def test_bare_numbers_yield_empty_set():
    assert detect_currency_tokens(pd.Series([10.0, 20.5])) == set()
```

- [x] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_currency.py -v` → FAIL.

- [x] **Step 3: Implement**

In `amounts.py`:
```python
_CURRENCY_MAP = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "R$": "BRL"}
_CURRENCY_RE = re.compile(r"(R\$|[$€£¥])|\b(USD|EUR|GBP|BRL|CAD|AUD|INR|JPY|MXN)\b", re.IGNORECASE)


def detect_currency_tokens(s: pd.Series, sample_n: int = 50) -> set:
    out = set()
    for v in s.dropna().astype(str).head(sample_n):
        m = _CURRENCY_RE.search(v)
        if m:
            token = (m.group(1) or m.group(2)).upper()
            out.add(_CURRENCY_MAP.get(token, token))
    return out
```
(add `import re` if not present).

In `agent.py`, before the `_amt` coercion:
```python
    from .tools.amounts import detect_currency_tokens
    if amt_a_col and amt_b_col and not cfg.allow_mixed_currency:
        cur_a = detect_currency_tokens(df_a[amt_a_col])
        cur_b = detect_currency_tokens(df_b[amt_b_col])
        if cur_a and cur_b and not (cur_a & cur_b):
            raise ValueError(
                f"Source A amounts look like {sorted(cur_a)} but Source B looks like "
                f"{sorted(cur_b)}. Cross-currency reconciliation is not supported yet — "
                "convert one file first, or set allow_mixed_currency to override."
            )
```
(`ValueError` already maps to HTTP 400 in `main.py`.)

- [x] **Step 4: Verify + commit**

Run: `python -m pytest -q && python -m app.eval` → pass (samples are all `$`/bare, no behavior change).

```bash
git add backend/app/tools/amounts.py backend/app/agent.py backend/app/models.py backend/tests/test_currency.py
git commit -m "feat: refuse silently reconciling across currencies"
```

---

### Task 12: Rule guardrails — amount ceilings + scope preview

A learned `force_status` rule keyed on a coarse signature can silently reclassify a future $10k discrepancy as "match". Cap every learned rule by amount, and show the user what a rule *would have done* before they accept it.

**Files:**
- Modify: `backend/app/main.py:439-455` (`resolve_triage` add_rule branch), `backend/app/memory/rule_proposer.py:60-74`
- Modify: `backend/app/memory/rules_store.py:221-256` (`apply_force_status_rules` gains ceiling check)
- Modify: `backend/app/agent.py:283-289` (pass `diff_abs`)
- Add endpoint: `GET /api/rules/{rule_id}/preview` in `main.py`
- Modify: `frontend/src/pages/RulesPage.jsx` (show preview before Accept)
- Test: `backend/tests/test_rule_guardrails.py`

- [x] **Step 1: Write the failing test**

`backend/tests/test_rule_guardrails.py`:
```python
from app.memory.rules_store import apply_force_status_rules
from app.models import Rule


def _rule(ceiling):
    return Rule(
        account_id="a", kind="force_status", description="test",
        when={"signature_prefix": "abc123", "max_abs_diff": ceiling},
        then={"status": "match"}, origin="user_rule",
        confidence=0.9, state="active",
    )


def test_rule_fires_under_ceiling():
    r = apply_force_status_rules([_rule(50.0)], "abc123def", "k1", diff_abs=10.0)
    assert r is not None and r.status == "match"


def test_rule_skipped_over_ceiling():
    r = apply_force_status_rules([_rule(50.0)], "abc123def", "k1", diff_abs=5000.0)
    assert r is None


def test_legacy_rule_without_ceiling_still_fires():
    rule = _rule(None)
    del rule.when["max_abs_diff"]
    r = apply_force_status_rules([rule], "abc123def", "k1", diff_abs=5000.0)
    assert r is not None
```

- [x] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_rule_guardrails.py -v` → FAIL: unexpected `diff_abs` kwarg.

- [x] **Step 3: Implement the ceiling**

`apply_force_status_rules(rules, signature, key, diff_abs=None)` — after the prefix match, add:
```python
        ceiling = r.when.get("max_abs_diff")
        if ceiling is not None and diff_abs is not None and abs(diff_abs) > float(ceiling):
            continue  # too big for this rule — let the real verdict stand
```
In `agent.py` pass `diff_abs=row_ctx.get("diff_abs")` at the call site.

Set the ceiling at rule creation:
- `main.py` `resolve_triage` add_rule branch: `when={"signature_prefix": item.signature, "max_abs_diff": round(max(3 * abs(item.diff_abs or 0.0), 50.0), 2)}`
- `rule_proposer.propose_from_decisions`: same formula using the largest `|diff_abs|` seen — the decision log entries don't carry diff_abs today, so use the triage item when resolvable (`triage_store.find_by_signature`) and fall back to `50.0`.

- [x] **Step 4: Scope preview endpoint**

In `main.py`:
```python
@app.get("/api/rules/{rule_id}/preview")
def preview_rule(rule_id: str, account: Account = Depends(require_account)):
    """What would this rule have done across recent jobs? Shown before Accept."""
    from .models import Rationale
    rules = rules_store.load_rules(account.id)
    rule = next((r for r in rules if r.id == rule_id), None)
    if rule is None or rule.kind != "force_status":
        raise HTTPException(status_code=404, detail="force_status rule not found")
    prefix = rule.when.get("signature_prefix") or ""
    affected, total = [], 0.0
    for meta in storage.list_jobs(account.id, limit=10):
        job = storage.load_job(meta["job_id"]) or {}
        for row in job.get("matched", []):
            rat = row.get("rationale") or {}
            try:
                sig = triage_store.signature_for_matched(Rationale.model_validate(rat), row)
            except Exception:
                continue
            if sig.startswith(prefix):
                affected.append({"job_id": meta["job_id"], "key": row.get("key"),
                                 "status": rat.get("status"), "diff_abs": row.get("diff_abs")})
                total += abs(row.get("diff_abs") or 0.0)
    return _clean({"rule_id": rule_id, "affected_count": len(affected),
                   "total_abs_diff": round(total, 2), "rows": affected[:50],
                   "max_abs_diff_ceiling": rule.when.get("max_abs_diff")})
```
`RulesPage.jsx`: on pending rules, fetch the preview and render "Would have affected **N rows / $X** in your last 10 jobs (ceiling $C)" next to the Accept button.

- [x] **Step 5: Verify + commit**

Run: `python -m pytest -q && python -m app.eval` → pass.

```bash
git add backend/app/memory/rules_store.py backend/app/memory/rule_proposer.py backend/app/main.py backend/app/agent.py frontend/src/pages/RulesPage.jsx backend/tests/test_rule_guardrails.py
git commit -m "feat: amount ceilings + scope preview so learned rules can't hide big discrepancies"
```

---

### Task 13: Consolidate fee patterns into the rules store

Fee knowledge is duplicated: seeded rules in `rules_store` AND hardcoded `FEE_PATTERNS` in `amounts.py`. Revoking a seeded fee rule today changes provenance but not the verdict — the classifier re-derives it. Make the rules store the only fee path.

**Files:**
- Modify: `backend/app/tools/amounts.py:28-36,58-80` (delete `FEE_PATTERNS` and the fee branch in `classify_amount_diff`)
- Modify: `backend/app/agent.py:130-134` (insights fee counter reads `rule_id_hint` sources — verify still matches)
- Test: `backend/tests/test_fee_consolidation.py`
- Re-pin: `samples/samples_*_expected.json` if summaries change

- [x] **Step 1: Write the failing test**

`backend/tests/test_fee_consolidation.py`:
```python
import pandas as pd
import pytest


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RECONOPS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RECONOPS_STUB_LLM", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-stub")
    import importlib
    from app.memory import accounts, rules_store
    importlib.reload(accounts); importlib.reload(rules_store)
    return accounts, rules_store


def _run(accounts, rules_store, revoke_fees=False):
    from app.agent import run_job
    from app.models import BindingSet, ReconcileConfig
    from app.tools.binding import bind_columns

    acc = accounts.create_account()
    rules_store.seed_defaults(acc.id)
    if revoke_fees:
        for r in rules_store.load_rules(acc.id):
            if r.kind == "fee_pattern":
                rules_store.revoke_rule(acc.id, r.id)
    da = pd.DataFrame({"order_id": ["#1"], "order_total": [100.00]})
    db = pd.DataFrame({"order_reference": ["#1"], "amount": [96.80]})  # Stripe shape
    cfg = ReconcileConfig(
        source_a=BindingSet(bindings=bind_columns(da)),
        source_b=BindingSet(bindings=bind_columns(db)),
    )
    return run_job(account=acc, df_a=da, df_b=db, cfg=cfg, job_id="t")


def test_active_fee_rule_classifies_fee_offset(env):
    out = _run(*env)
    assert out.matched[0]["status"] == "fee_offset"


def test_revoked_fee_rule_changes_the_verdict(env):
    out = _run(*env, revoke_fees=True)
    assert out.matched[0]["status"] != "fee_offset"  # fails while FEE_PATTERNS exists
```

- [x] **Step 2: Run to verify the second test fails**

Run: `python -m pytest tests/test_fee_consolidation.py -v`
Expected: first passes, second FAILS (classifier re-derives the fee from `FEE_PATTERNS`).

- [x] **Step 3: Implement**

In `amounts.py`: delete the `FEE_PATTERNS` list and the entire `# Fee patterns` branch inside `classify_amount_diff` (the `if a_amt > b_amt and a_amt > 0:` block). The seeded `fee_pattern` rules in `rules_store.apply_rules_to_matched` (which runs *before* classification in `agent.run_job`) are now the only fee detector.

Check `grep -rn "FEE_PATTERNS" backend/` afterward — the only remaining references should be comments; delete those too.

- [x] **Step 4: Re-pin eval + verify**

Run: `python -m pytest -q` → both fee tests pass.
Run: `python -m app.eval` — fee counts should be identical (rules fire first for every seeded account); if `fee_offset` counts differ in the snapshot diff, investigate before re-pinning — a difference means some path reached classification without account rules loaded.

- [x] **Step 5: Commit**

```bash
git add backend/app/tools/amounts.py backend/tests/test_fee_consolidation.py samples/
git commit -m "fix: rules store is the single source of fee patterns; revoke now changes verdicts"
```

---

### Task 14: Anchor unmatched-row keys to the resolved binding

`_first_key_value` guesses the key column from a hardcoded name list (`agent.py:57`, `triage.py:271`) — files whose key column has any other name get unstable triage signatures and `expected_unmatched` rules that never fire.

**Files:**
- Modify: `backend/app/agent.py:57-67,323-342,377-388` (pass `key_a.column_name` / `key_b.column_name` through)
- Modify: `backend/app/memory/triage.py:90-104,178-266,271-280` (`emit_for_job` and `signature_for_unmatched` accept `key_col`)
- Test: `backend/tests/test_unmatched_keys.py`

- [x] **Step 1: Write the failing test**

`backend/tests/test_unmatched_keys.py`:
```python
from app.memory.triage import signature_for_unmatched


def test_signature_uses_explicit_key_column():
    row = {"weird_ref_col": "SUB-991", "amount": 5.0}
    with_col = signature_for_unmatched("a", row, key_col="weird_ref_col")
    # Same key prefix must produce the same signature regardless of dict order
    row2 = {"amount": 9.0, "weird_ref_col": "SUB-442"}
    assert with_col == signature_for_unmatched("a", row2, key_col="weird_ref_col")
```

- [x] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/test_unmatched_keys.py -v` → FAIL: unexpected `key_col` kwarg.

- [x] **Step 3: Implement**

`triage.signature_for_unmatched(side, row_ctx, key_col=None)`: when `key_col` is given and present in the row, use `str(row_ctx[key_col])` as the key; keep the existing guess-list as fallback. `_first_key_value(row, key_col=None)` same pattern (both copies — agent and triage). `emit_for_job(...)` gains `key_col_a=None, key_col_b=None` kwargs, passed to the per-side calls. In `agent.py`: pass `key_col_a=key_a.column_name, key_col_b=key_b.column_name` at the `emit_for_job` call, and use `row.get(key_a.column_name)` in the `is_expected_unmatched` loop for side A (respectively B).

Also update the same-signature call in `main.py` `compare_jobs` (`sigs()` helper) to pass the job's stored binding column names: `job["bindings_a"]["bindings"]` → find the binding whose concept has role `primary_key` (reuse `_candidate_keys` logic or store `key_col_a` in the job payload at upload — simplest: add `"key_col_a": key_a.column_name, "key_col_b": key_b.column_name` to `AgentOutput` and the persisted payload, then read it in `compare_jobs`).

- [x] **Step 4: Verify + commit**

Run: `python -m pytest -q && python -m app.eval` → pass (sample key columns are in the guess list, so signatures shouldn't change).

```bash
git add backend/app/agent.py backend/app/memory/triage.py backend/app/main.py backend/tests/test_unmatched_keys.py
git commit -m "fix: triage signatures anchor to the resolved key binding, not a guessed column"
```

---

### Task 15: Per-account materiality thresholds

"Major" is hardcoded at ≥$100 or ≥3% (`amounts.py:83`). A $1M-revenue brand and a $20M brand need different lines.

**Files:**
- Modify: `backend/app/models.py:7-13` (AccountProfile gains `materiality_abs: float = 100.0`, `materiality_pct: float = 0.03`)
- Modify: `backend/app/tools/amounts.py:39-46` (`classify_amount_diff` gains `major_abs=100.0, major_pct=0.03` params; replace the literals)
- Modify: `backend/app/tools/classify.py:64-104` (pass-through params)
- Modify: `backend/app/agent.py:177-190` (read from `account.profile`, pass down)
- Add endpoint: `PATCH /api/accounts/me/profile` in `main.py` (calls `accounts_memory.update_profile`)
- Test: extend `backend/tests/test_amounts.py`

- [x] **Step 1: Write the failing test**

Append to `backend/tests/test_amounts.py`:
```python
def test_materiality_thresholds_are_parameters():
    # $60 diff: major for a small brand (threshold $50), minor for default ($100)
    status_small, *_ = classify_amount_diff(60.0, 0.006, 10000.0, 9940.0,
                                            0.01, 0.005, major_abs=50.0, major_pct=0.03)
    status_default, *_ = classify_amount_diff(60.0, 0.006, 10000.0, 9940.0,
                                              0.01, 0.005)
    assert status_small == "major"
    assert status_default == "minor"
```

- [x] **Step 2: Run to verify it fails** → `python -m pytest tests/test_amounts.py -v` → FAIL: unexpected kwargs.

- [x] **Step 3: Implement**

`classify_amount_diff(diff_abs, diff_pct, a_amt, b_amt, tol_abs, tol_pct, major_abs=100.0, major_pct=0.03)`; replace both `0.03`/`100` literal pairs (major branch and the fee-alternative text) with the params. Thread through `propose_classification(..., major_abs, major_pct)` and from `agent.run_job`: `account.profile.materiality_abs`, `account.profile.materiality_pct`.

Profile endpoint in `main.py`:
```python
@app.patch("/api/accounts/me/profile", response_model=Account)
def patch_profile(payload: dict, account: Account = Depends(require_account)):
    allowed = {k: payload.get(k) for k in
               ("time_zone", "amount_tolerance_abs", "amount_tolerance_pct",
                "materiality_abs", "materiality_pct")}
    return accounts_memory.update_profile(account.id, allowed)
```

- [x] **Step 4: Verify + commit**

Run: `python -m pytest -q && python -m app.eval` → pass (defaults unchanged).

```bash
git add backend/app/models.py backend/app/tools/amounts.py backend/app/tools/classify.py backend/app/agent.py backend/app/main.py backend/tests/test_amounts.py
git commit -m "feat: per-account materiality thresholds for major/minor classification"
```

---

### Task 16: Ontology alias harvest from real datasets

**Files:**
- Modify: `backend/app/ontology/concepts.yaml`
- Test: `backend/tests/test_binding_aliases.py`

- [x] **Step 1: Write the failing test**

`backend/tests/test_binding_aliases.py`:
```python
import pandas as pd

from app.tools.binding import bind_columns


def _top_concept(df, col):
    for b in bind_columns(df):
        if b.column_name == col:
            return b.concept_id
    return None


def test_olist_payment_columns_bind():
    df = pd.DataFrame({
        "order_id": ["4244733e06e7ecb4970a6e2683c13e61"],
        "payment_value": [99.33],
        "payment_type": ["credit_card"],
    })
    assert _top_concept(df, "payment_value") == "payment.amount"
    assert _top_concept(df, "payment_type") == "payment.method"


def test_stripe_export_headers_bind():
    df = pd.DataFrame({
        "Created (UTC)": ["2026-07-01 10:00:00"],
        "Amount": [100.0],
        "Fee": [3.2],
        "Converted Amount": [96.8],
    })
    assert _top_concept(df, "Created (UTC)") == "date.event"
    assert _top_concept(df, "Fee") == "payment.fee"
```

- [x] **Step 2: Run to verify what fails** → `python -m pytest tests/test_binding_aliases.py -v`. Some assertions may already pass via substring/value-shape signals; keep only genuinely failing ones in mind for step 3 but leave all assertions in the test.

- [x] **Step 3: Add aliases to `concepts.yaml`**

- `payment.amount` aliases += `payment_value`, `converted_amount`
- `payment.method` aliases += `payment_type`
- `payment.fee` aliases += `fee_amount`
- `order.gross_total` aliases += `price`
- `date.event` aliases += `order_purchase_timestamp`, `created (utc)`, `created_utc`, `available_on`, `available on (utc)`
- `sku.id` aliases += `product_id`

- [x] **Step 4: Verify + commit**

Run: `python -m pytest -q && python -m app.eval` → all pass; if the eval snapshot changes (a sample column now binds differently), inspect and re-pin only if the new binding is correct.

```bash
git add backend/app/ontology/concepts.yaml backend/tests/test_binding_aliases.py
git commit -m "feat: ontology aliases for Olist and Stripe export headers"
```

---

## Phase 2 — Productization infrastructure

Each item below is a **separate implementation plan** (write it with the same format as this document when picked up). Listed here with scope and acceptance criteria so nothing gets lost.

### 2.1 Authentication & organizations
**Implemented** — see [docs/plans/2026-07-18-auth-and-orgs.md](docs/plans/2026-07-18-auth-and-orgs.md)
(implementation plan) and the auth section of [docs/DEPLOY.md](docs/DEPLOY.md).
Delivered: self-rolled email-OTP sign-in (codes and session tokens sha256-hashed
at rest, rate-limited, single-use) with httpOnly cookie sessions; Org=Account
membership with owner/analyst roles; global endpoint lockdown via the rewritten
`require_account` dependency; one-time legacy-UUID claim migration; decision-log
user attribution + `Rule.created_by`; HMAC-signed 5-minute export tokens; login
gate UI with dev-mode codes for local work.
- **Done when:** no endpoint is reachable without a session; export links expire; decision log rows carry user identity. ✓ (all three test-enforced; E2E verified against a live server)

### 2.2 Postgres + object storage
- SQLAlchemy models mirroring the existing Pydantic schemas (accounts, jobs, rules, triage items, decisions, metrics); Alembic migrations; a one-shot importer for existing JSON data. Uploaded files to S3-compatible storage with server-side encryption.
- **Done when:** eval passes against Postgres-backed stores; `data/` contains nothing but local cache; deleting an account cascades.

### 2.3 Async job execution
- Background worker (start with FastAPI `BackgroundTasks` + persisted `processing` status; graduate to RQ/arq if concurrency demands). Upload returns immediately; frontend polls `GET /api/status/{job_id}` (already exists). Persist partial progress (rows processed) for the progress bar; enforce a per-job wall-clock timeout.
- **Done when:** a 100k-row file completes without an open HTTP request; a killed worker leaves a resumable/failed job, never a lost one.

### 2.4 Production deployment
**Implemented** — see [docs/DEPLOY.md](docs/DEPLOY.md) (runbook) and
[docs/plans/2026-07-13-production-deployment.md](docs/plans/2026-07-13-production-deployment.md)
(implementation plan). Delivered: Caddy edge with auto-HTTPS + built frontend,
env-driven CORS, JSON access logs with request IDs, env-gated Sentry with
account/job tags, hourly retention scheduler, CI-built prod images.
Frontend Sentry was scoped out (backend coverage satisfies the done-criterion).
- **Done when:** a client can be onboarded on a URL you'd put in an email; an exception in production shows up in Sentry with a job ID. ✓ (pending only the local stack run-through after the next reboot)

### 2.5 Data governance packet
- `DELETE /api/accounts/me` (full purge), configurable retention per account, and a one-page data-handling doc: data flow diagram, subprocessor list (Anthropic — API data not used for training), retention/deletion policy, plain-language DPA template, "decision support, not accounting advice" disclaimer.
- **Done when:** you can answer a client's security questionnaire from the doc without improvising.

## Phase 3 — Go-to-market

Also separate plans; sequenced after Phase 2.1–2.3.

- **3.1 Integrations:** read-only Shopify app + Stripe Connect; scheduled weekly auto-pull → auto-reconcile → email. This is the retention moat; CSV upload remains the wedge for 3PL/supplier files.
- **3.2 Report artifact:** white-label monthly-close PDF/email — "$X verified, $Y fees confirmed normal, N items need you (top item $Z)". Accountant-forwardable.
- **3.3 Billing & quotas:** plans with included jobs/rows; per-account monthly LLM budget enforced at the `call_claude` chokepoint (usage log already exists); Stripe Billing.
- **3.4 Insight-quality rubric:** scored checklist (cites $? names specific orders? actions ranked by impact?) run against eval outputs so "better insights" is measurable, not vibes.
- **3.5 Document-grounded context (fee schedules first):** upload a processor statement/fee schedule → `extract_from_text` proposes `fee_pattern` rules for the user to confirm. Narrow scope deliberately; generic "upload any document" is a demo trap. The Olist data-dictionary `.docx` files in `samples/Kaggle/` are good test inputs for a later "data dictionary improves bindings" experiment.

---

## Execution order & definition of sellable

Phases 0 and 1 are sequential as written (Tasks 1–4 unblock everything; Task 9 before 10). Phase 2 plans can be written in parallel once Phase 1 lands.

**Minimum bar to put this in front of a paying client (concierge mode):** Phase 0 complete + Tasks 9–12. You drive the uploads; auth and async can wait until self-serve.

**Minimum bar for self-serve:** all of Phase 0–1 + 2.1 + 2.3 + 2.4.
