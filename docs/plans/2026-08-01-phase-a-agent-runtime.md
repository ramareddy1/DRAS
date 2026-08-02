# Phase A — Agent Runtime Beside the Pipeline

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up a durable, resumable agent runtime alongside the existing reconciliation pipeline, so a user can POST a goal and watch a planned run execute over SSE — with the classic recon path intact and reachable as a single macro-tool.

**Architecture:** A new `backend/app/agent_runtime/` package drives the Anthropic SDK Tool Runner over handle-based wrappers of the existing deterministic tools. Run state lives in Postgres (`runs`, `run_events`, `run_artifacts`); the loop mirrors the LLM message history into `runs.transcript` so a run can suspend on a question and resume in a different process. `run_events` is the single append-only log serving SSE, audit, and replay. `backend/app/agent.py` is untouched and registered as the `run_reconciliation` macro-tool.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy 2.0 + Alembic, Postgres, `anthropic` SDK (Tool Runner beta), pandas, pytest.

**Spec:** [`docs/specs/2026-08-01-agent-runtime-architecture.md`](../specs/2026-08-01-agent-runtime-architecture.md) — read §1 (data model), §2 (tool contract), §3.6 (transport) before starting.

## Global Constraints

Every task's requirements implicitly include this section.

- **Package name is `backend/app/agent_runtime/`** — not `app/agent/`. The plan doc drifts on this; the spec settles it.
- **`backend/app/agent.py` is not modified in this phase.** It is wrapped, not edited. Zero regression is the point.
- **Model ID is exactly `claude-opus-4-8`.** Never append a date suffix.
- **Use `thinking={"type": "adaptive"}` and `output_config={"effort": "xhigh"}`.** `budget_tokens` returns HTTP 400 on this model — it must not appear anywhere in new code.
- **The LLM never computes money.** Every number in a result originates from a deterministic tool. Any new code path that lets model output become a verdict is a defect, not a tradeoff.
- **Tools take handles, never row data** — `dataset_id`, `job_id`, `concept_id`, scalars, column names. Every tool return value has an explicit bound.
- **Every tool declares `effect` ∈ `read | external | write`.**
- **The tool registry serializes sorted by tool name.** Prompt caching is a byte-exact prefix match; unstable ordering silently destroys it.
- **One write path:** the loop writes `runs.transcript` and `run_events` in the same transaction.
- **All new tables carry `account_id`** with `ForeignKey("accounts.id", ondelete="CASCADE")` and are covered by retention/purge.
- **ORM convention:** real columns only for what is queried, filtered, or foreign-keyed; everything else in a `payload` JSONB column holding `<Model>.model_dump(mode="json")`.
- **Feature flag:** every new HTTP route is gated on `RECONOPS_AGENT_RUNTIME=1`.
- **Tests run from `backend/`** via `pytest` (`pythonpath = .`, `testpaths = tests`). The autouse `_clean_db` fixture in `tests/conftest.py` truncates all ORM tables between tests — new tables are covered automatically once their ORM classes are imported by `app.db.models`.
- **LLM stubbing in tests uses the existing gate:** `RECONOPS_STUB_LLM=1`. Do not invent a second mechanism.
- **`app/llm.py` remains the single usage-logging chokepoint.** New LLM calls log through it.

---

## File Structure

**New package — `backend/app/agent_runtime/`**

| File | Responsibility |
|---|---|
| `__init__.py` | Public surface: `start_run`, `resume_run`, `RunHandle` |
| `store.py` | All Postgres reads/writes for `runs` / `run_events`. The only module that touches those tables. |
| `artifacts.py` | Dataset persistence — `put_dataset(df) → dataset_id`, `get_dataset(dataset_id) → df`, via `storage_s3.py` |
| `registry.py` | Tool registration, effect classification, deterministic serialization |
| `tools_core.py` | Handle-based wrappers around `app/tools/*` — the tier-1 registry contents |
| `tools_macro.py` | `run_reconciliation` — wraps `app/agent.py:run_job` |
| `critic.py` | Post-condition registry keyed by tool name; `check(tool_name, output)` |
| `budget.py` | `Budget`, `Spend`, cap enforcement |
| `runtime.py` | The Tool Runner loop: transcript mirroring, event emission, gates |
| `routes.py` | `POST /api/agent/runs`, `GET /api/agent/runs/{id}/events` (SSE) |

**Modified**

| File | Change |
|---|---|
| `backend/requirements.txt` | `anthropic` version bump |
| `backend/app/llm.py:19` | `DEFAULT_MODEL` → `claude-opus-4-8` |
| `backend/app/models.py` | Add `Run`, `RunEvent`, `RunArtifact` Pydantic models |
| `backend/app/db/models.py` | Add `RunORM`, `RunEventORM`, `RunArtifactORM` |
| `backend/app/main.py` | Mount `agent_runtime.routes` router |
| `backend/app/eval.py` | Add planner-vs-macro-tool parity gate |

**New migrations:** `backend/alembic/versions/0008_runs.py`, `0009_run_artifacts.py`

**New tests:** one `backend/tests/test_agent_*.py` per task.

---

## Task 1: SDK bump and Tool Runner assumption check

The spec (§2.6) rests on two unverified assumptions. Prove them before anything is built on top. `anthropic==0.39.0` predates the Tool Runner entirely.

**Files:**
- Modify: `backend/requirements.txt`
- Modify: `backend/app/llm.py:19`
- Test: `backend/tests/test_agent_sdk_assumptions.py`

**Interfaces:**
- Consumes: nothing
- Produces: a working `anthropic` with `client.beta.messages.tool_runner` and `@beta_tool`; `DEFAULT_MODEL == "claude-opus-4-8"`

- [ ] **Step 1: Bump the SDK and verify the Tool Runner symbol exists**

```bash
cd backend
.venv/Scripts/pip install --upgrade 'anthropic>=0.116.0'
.venv/Scripts/python -c "import anthropic; print(anthropic.__version__); from anthropic import beta_tool; print('beta_tool ok'); import inspect; print('tool_runner ok' if hasattr(anthropic.Anthropic().beta.messages, 'tool_runner') else 'MISSING')"
```

Expected: a version ≥ 0.116.0, `beta_tool ok`, `tool_runner ok`.
If `MISSING`, stop and report — every later task depends on it.

- [ ] **Step 2: Pin the resolved version in requirements.txt**

Replace the `anthropic==0.39.0` line with the exact version printed in Step 1, e.g.:

```
anthropic==0.116.0
```

- [ ] **Step 3: Write the failing assumption test**

Create `backend/tests/test_agent_sdk_assumptions.py`:

```python
"""Guards two assumptions the tool registry design rests on (spec §2.6).

If either breaks on an SDK upgrade, the prompt-cache strategy and the
pack-tool layering silently stop working — with no error at runtime.
"""
from __future__ import annotations

import json

from anthropic import beta_tool

from app.llm import DEFAULT_MODEL


@beta_tool
def _sample_tool(dataset_id: str, limit: int = 10) -> str:
    """A sample tool used only to inspect generated schema.

    Args:
        dataset_id: Handle for a stored dataset.
        limit: Maximum rows to consider.
    """
    return "ok"


def test_default_model_is_opus_4_8():
    assert DEFAULT_MODEL == "claude-opus-4-8"


def test_generated_schema_is_byte_stable_across_calls():
    """Prompt caching is a byte-exact prefix match on the serialized tools."""
    first = json.dumps(_sample_tool.to_dict(), sort_keys=True)
    second = json.dumps(_sample_tool.to_dict(), sort_keys=True)
    assert first == second


def test_generated_schema_carries_docstring_and_params():
    schema = _sample_tool.to_dict()
    assert schema["name"] == "_sample_tool"
    assert "dataset_id" in schema["input_schema"]["properties"]
    assert "limit" in schema["input_schema"]["properties"]
    assert schema["input_schema"]["required"] == ["dataset_id"]


def test_raw_tool_definitions_mix_with_decorated_tools():
    """Pack tools ride behind tool search; both kinds share one list."""
    raw = {
        "type": "tool_search_tool_regex_20251119",
        "name": "tool_search_tool_regex",
    }
    tools = [_sample_tool, raw]
    assert len(tools) == 2
    assert callable(tools[0])
    assert tools[1]["name"] == "tool_search_tool_regex"
```

- [ ] **Step 4: Run it and watch it fail on the model ID**

Run: `cd backend && .venv/Scripts/python -m pytest tests/test_agent_sdk_assumptions.py -v`
Expected: `test_default_model_is_opus_4_8` FAILS (`claude-opus-4-7 != claude-opus-4-8`); the schema tests pass.

If `to_dict()` raises `AttributeError`, the accessor differs in your SDK version — find the real one with `dir(_sample_tool)` and update all three schema tests. Record the correct accessor in the commit message; Task 5 uses it.

- [ ] **Step 5: Fix the model ID**

In `backend/app/llm.py:19`:

```python
DEFAULT_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-8")
```

- [ ] **Step 6: Run the whole suite for upgrade fallout**

Run: `cd backend && .venv/Scripts/python -m pytest -q`
Expected: all pass. The SDK jump spans many versions; `app/llm.py`'s three call sites (`call_claude`, `call_claude_json`, `_stub_response`) are the likely breakage points. Fix any failures in `llm.py` only — do not change tool or agent behaviour.

- [ ] **Step 7: Commit**

```bash
git add backend/requirements.txt backend/app/llm.py backend/tests/test_agent_sdk_assumptions.py
git commit -m "chore: bump anthropic SDK, target claude-opus-4-8, guard tool-runner assumptions"
```

---

## Task 2: Run tables — Pydantic models, ORM, migration

**Files:**
- Modify: `backend/app/models.py`
- Modify: `backend/app/db/models.py`
- Create: `backend/alembic/versions/0008_runs.py`
- Test: `backend/tests/test_agent_run_models.py`

**Interfaces:**
- Consumes: Task 1 (nothing structural)
- Produces:
  - `app.models.Run` — fields `id, account_id, goal, status, autonomy, playbook_id, budget, spend, suspended_on, transcript, created_at, ended_at, error`
  - `app.models.RunEvent` — `id, run_id, account_id, seq, type, payload, at`
  - `app.db.models.RunORM` (table `runs`), `RunEventORM` (table `run_events`)
  - `RunStatus`, `AutonomyLevel`, `RunEventType` string enums

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_agent_run_models.py`:

```python
from __future__ import annotations

import uuid

from sqlalchemy import select

from app.db.base import session_scope
from app.db.models import RunEventORM, RunORM
from app.models import AutonomyLevel, Run, RunEvent, RunEventType, RunStatus


def _account_row():
    """Runs FK to accounts; create a bare account row to satisfy it."""
    from app.db.models import AccountORM

    acct_id = str(uuid.uuid4())
    with session_scope() as s:
        s.add(AccountORM(id=acct_id, payload={}))
    return acct_id


def test_run_round_trips_through_postgres():
    acct = _account_row()
    run = Run(
        id=str(uuid.uuid4()),
        account_id=acct,
        goal={"intent": "close_month", "entities": {"month": "2026-06"}},
        status=RunStatus.pending,
        autonomy=AutonomyLevel.assist,
        budget={"usd_cap": 0.40, "tool_call_cap": 30, "wall_clock_s": 120},
    )
    with session_scope() as s:
        s.add(RunORM(
            id=run.id, account_id=run.account_id, status=run.status.value,
            payload=run.model_dump(mode="json"),
        ))

    with session_scope() as s:
        row = s.get(RunORM, run.id)
        assert row is not None
        loaded = Run.model_validate(row.payload)
    assert loaded.goal["intent"] == "close_month"
    assert loaded.autonomy is AutonomyLevel.assist
    assert loaded.budget["tool_call_cap"] == 30


def test_run_events_are_ordered_by_monotonic_id():
    """SSE Last-Event-ID resume depends on a total order."""
    acct = _account_row()
    run_id = str(uuid.uuid4())
    with session_scope() as s:
        s.add(RunORM(id=run_id, account_id=acct, status="running", payload={}))

    with session_scope() as s:
        for seq, etype in enumerate([
            RunEventType.goal_received,
            RunEventType.plan_proposed,
            RunEventType.tool_called,
        ]):
            s.add(RunEventORM(
                run_id=run_id, account_id=acct, seq=seq,
                type=etype.value, payload={"n": seq},
            ))

    # Capture values INSIDE the block. session_scope() commits and closes on
    # exit, which expires every loaded attribute — reading them afterwards
    # raises DetachedInstanceError. All seven existing stores follow this
    # same convention.
    with session_scope() as s:
        rows = list(s.scalars(
            select(RunEventORM).where(RunEventORM.run_id == run_id)
            .order_by(RunEventORM.id)
        ))
        observed = [(r.type, r.id) for r in rows]

    assert [t for t, _ in observed] == [
        "goal_received", "plan_proposed", "tool_called",
    ]
    ids = [i for _, i in observed]
    assert ids == sorted(ids)
    assert ids[0] < ids[1] < ids[2]


def test_run_event_model_validates_payload():
    ev = RunEvent(
        id=1, run_id="r", account_id="a", seq=0,
        type=RunEventType.tool_returned,
        payload={"tool": "bind_columns", "bound_count": 24},
        at="2026-08-01T00:00:00Z",
    )
    assert ev.type is RunEventType.tool_returned
    assert ev.payload["bound_count"] == 24
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd backend && .venv/Scripts/python -m pytest tests/test_agent_run_models.py -v`
Expected: FAIL — `ImportError: cannot import name 'Run' from 'app.models'`

- [ ] **Step 3: Add the Pydantic models**

Append to `backend/app/models.py`:

```python
class RunStatus(str, Enum):
    pending = "pending"
    running = "running"
    suspended = "suspended"
    done = "done"
    failed = "failed"
    aborted = "aborted"


class AutonomyLevel(str, Enum):
    observe = "observe"
    assist = "assist"
    auto = "auto"


class RunEventType(str, Enum):
    goal_received = "goal_received"
    plan_proposed = "plan_proposed"
    plan_approved = "plan_approved"
    step_started = "step_started"
    step_completed = "step_completed"
    tool_called = "tool_called"
    tool_returned = "tool_returned"
    tool_failed = "tool_failed"
    assistant_text = "assistant_text"
    render = "render"
    question_asked = "question_asked"
    question_answered = "question_answered"
    proposal_emitted = "proposal_emitted"
    proposal_accepted = "proposal_accepted"
    proposal_rejected = "proposal_rejected"
    critic_check = "critic_check"
    budget_exceeded = "budget_exceeded"
    run_finished = "run_finished"


class Run(BaseModel):
    id: str
    account_id: str
    goal: Dict[str, Any] = Field(default_factory=dict)
    status: RunStatus = RunStatus.pending
    autonomy: AutonomyLevel = AutonomyLevel.assist
    playbook_id: Optional[str] = None
    budget: Dict[str, Any] = Field(default_factory=dict)
    spend: Dict[str, Any] = Field(default_factory=dict)
    suspended_on: Optional[int] = None
    transcript: List[Dict[str, Any]] = Field(default_factory=list)
    created_at: Optional[str] = None
    ended_at: Optional[str] = None
    error: Optional[str] = None


class RunEvent(BaseModel):
    id: Optional[int] = None
    run_id: str
    account_id: str
    seq: int
    type: RunEventType
    payload: Dict[str, Any] = Field(default_factory=dict)
    at: Optional[str] = None
```

If `Enum`, `Dict`, `Any`, `List`, `Optional`, or `Field` are not already imported at the top of `models.py`, add them (`from enum import Enum`, `from typing import Any, Dict, List, Optional`, `from pydantic import BaseModel, Field`).

- [ ] **Step 4: Add the ORM classes**

Append to `backend/app/db/models.py`. Note `BigInteger` and `DateTime` must be added to the `sqlalchemy` import line:

```python
class RunORM(Base):
    __tablename__ = "runs"

    id = Column(String(36), primary_key=True)
    account_id = Column(String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    status = Column(String, nullable=False, default="pending", index=True)
    created_at = Column(String, nullable=True, index=True)
    payload = Column(JSONB, nullable=False, default=dict)


class RunEventORM(Base):
    __tablename__ = "run_events"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    run_id = Column(String(36), ForeignKey("runs.id", ondelete="CASCADE"),
                    nullable=False, index=True)
    account_id = Column(String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    seq = Column(Integer, nullable=False)
    type = Column(String, nullable=False, index=True)
    payload = Column(JSONB, nullable=False, default=dict)
    at = Column(String, nullable=True)

    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="uq_run_event_run_seq"),
    )
```

`transcript` lives inside `RunORM.payload` per the ORM convention — it is never queried by field.

- [ ] **Step 5: Run the test to verify it passes**

Run: `cd backend && .venv/Scripts/python -m pytest tests/test_agent_run_models.py -v`
Expected: PASS (the autouse fixture calls `Base.metadata.create_all`, so tests do not need the migration).

- [ ] **Step 6: Write the migration**

Create `backend/alembic/versions/0008_runs.py`:

```python
"""create runs and run_events tables

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-01
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("account_id", sa.String(length=36),
                  sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.String(), nullable=True),
        sa.Column("payload", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.create_index("ix_runs_account_id", "runs", ["account_id"])
    op.create_index("ix_runs_status", "runs", ["status"])
    op.create_index("ix_runs_created_at", "runs", ["created_at"])

    op.create_table(
        "run_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(length=36),
                  sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("account_id", sa.String(length=36),
                  sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("payload", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("at", sa.String(), nullable=True),
        sa.UniqueConstraint("run_id", "seq", name="uq_run_event_run_seq"),
    )
    op.create_index("ix_run_events_run_id", "run_events", ["run_id"])
    op.create_index("ix_run_events_account_id", "run_events", ["account_id"])
    op.create_index("ix_run_events_type", "run_events", ["type"])


def downgrade() -> None:
    op.drop_table("run_events")
    op.drop_table("runs")
```

- [ ] **Step 7: Apply and roll back the migration**

```bash
cd backend
.venv/Scripts/python -m alembic upgrade head
.venv/Scripts/python -m alembic downgrade -1
.venv/Scripts/python -m alembic upgrade head
```

Expected: all three succeed with no error. This proves the migration is reversible.

- [ ] **Step 8: Commit**

```bash
git add backend/app/models.py backend/app/db/models.py backend/alembic/versions/0008_runs.py backend/tests/test_agent_run_models.py
git commit -m "feat(agent): runs and run_events tables"
```

---

## Task 3: Run store

The only module that touches `runs` / `run_events`. Everything else goes through it, which is what keeps "one write path" enforceable by review.

**Files:**
- Create: `backend/app/agent_runtime/__init__.py` (empty for now)
- Create: `backend/app/agent_runtime/store.py`
- Test: `backend/tests/test_agent_store.py`

**Interfaces:**
- Consumes: `app.models.Run`, `RunEvent`, `RunStatus`, `RunEventType`; `app.db.models.RunORM`, `RunEventORM`
- Produces:
  - `create_run(*, account_id: str, goal: dict, autonomy: AutonomyLevel, budget: dict) -> Run`
  - `load_run(run_id: str, account_id: str) -> Run | None`
  - `append_event(*, run: Run, type: RunEventType, payload: dict) -> RunEvent`
  - `events_since(*, run_id: str, account_id: str, after_id: int = 0, limit: int = 500) -> list[RunEvent]`
  - `save_transcript(*, run_id: str, account_id: str, transcript: list[dict], spend: dict) -> None`
  - `set_status(*, run_id: str, account_id: str, status: RunStatus, error: str | None = None, suspended_on: int | None = None) -> None`

Every function takes `account_id` and filters on it. A run is never loaded by id alone.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_agent_store.py`:

```python
from __future__ import annotations

import uuid

import pytest

from app.agent_runtime import store
from app.db.base import session_scope
from app.db.models import AccountORM
from app.models import AutonomyLevel, RunEventType, RunStatus


@pytest.fixture()
def account_id() -> str:
    acct = str(uuid.uuid4())
    with session_scope() as s:
        s.add(AccountORM(id=acct, payload={}))
    return acct


def test_create_and_load_run(account_id):
    run = store.create_run(
        account_id=account_id,
        goal={"intent": "close_month"},
        autonomy=AutonomyLevel.assist,
        budget={"tool_call_cap": 30},
    )
    assert run.status is RunStatus.pending
    assert run.created_at is not None

    loaded = store.load_run(run.id, account_id)
    assert loaded is not None
    assert loaded.goal["intent"] == "close_month"


def test_load_run_is_account_scoped(account_id):
    """Cross-account reads must return None, not raise — no existence oracle."""
    run = store.create_run(
        account_id=account_id, goal={}, autonomy=AutonomyLevel.assist, budget={},
    )
    other = str(uuid.uuid4())
    with session_scope() as s:
        s.add(AccountORM(id=other, payload={}))
    assert store.load_run(run.id, other) is None


def test_append_event_assigns_increasing_seq_and_id(account_id):
    run = store.create_run(
        account_id=account_id, goal={}, autonomy=AutonomyLevel.assist, budget={},
    )
    a = store.append_event(run=run, type=RunEventType.goal_received, payload={})
    b = store.append_event(run=run, type=RunEventType.plan_proposed, payload={"steps": []})

    assert a.seq == 0 and b.seq == 1
    assert a.id is not None and b.id is not None
    assert b.id > a.id


def test_events_since_supports_sse_resume(account_id):
    run = store.create_run(
        account_id=account_id, goal={}, autonomy=AutonomyLevel.assist, budget={},
    )
    first = store.append_event(run=run, type=RunEventType.goal_received, payload={})
    store.append_event(run=run, type=RunEventType.tool_called, payload={"tool": "x"})
    store.append_event(run=run, type=RunEventType.tool_returned, payload={"tool": "x"})

    resumed = store.events_since(
        run_id=run.id, account_id=account_id, after_id=first.id,
    )
    assert [e.type for e in resumed] == [
        RunEventType.tool_called, RunEventType.tool_returned,
    ]


def test_save_transcript_and_status_round_trip(account_id):
    run = store.create_run(
        account_id=account_id, goal={}, autonomy=AutonomyLevel.assist, budget={},
    )
    transcript = [{"role": "user", "content": "did June get paid?"}]
    store.save_transcript(
        run_id=run.id, account_id=account_id,
        transcript=transcript, spend={"tool_calls": 3},
    )
    store.set_status(run_id=run.id, account_id=account_id, status=RunStatus.running)

    loaded = store.load_run(run.id, account_id)
    assert loaded.transcript == transcript
    assert loaded.spend["tool_calls"] == 3
    assert loaded.status is RunStatus.running


def test_suspend_records_the_pending_question(account_id):
    run = store.create_run(
        account_id=account_id, goal={}, autonomy=AutonomyLevel.assist, budget={},
    )
    q = store.append_event(
        run=run, type=RunEventType.question_asked,
        payload={"text": "order date or payout date?"},
    )
    store.set_status(
        run_id=run.id, account_id=account_id,
        status=RunStatus.suspended, suspended_on=q.id,
    )

    loaded = store.load_run(run.id, account_id)
    assert loaded.status is RunStatus.suspended
    assert loaded.suspended_on == q.id
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd backend && .venv/Scripts/python -m pytest tests/test_agent_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.agent_runtime'`

- [ ] **Step 3: Create the package and the store**

Create `backend/app/agent_runtime/__init__.py` as an empty file.

Create `backend/app/agent_runtime/store.py`:

```python
"""Postgres persistence for agent runs and their event log.

The only module that reads or writes `runs` / `run_events`. Everything in
`agent_runtime` goes through here, which is what makes "one write path"
(spec §1.2) reviewable rather than aspirational.

Every function is account-scoped. A run is never loaded by id alone — a
cross-account read returns None rather than raising, so the API surface
never becomes an existence oracle.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select

from ..db.base import session_scope
from ..db.models import RunEventORM, RunORM
from ..models import AutonomyLevel, Run, RunEvent, RunEventType, RunStatus


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_run(
    *,
    account_id: str,
    goal: Dict[str, Any],
    autonomy: AutonomyLevel,
    budget: Dict[str, Any],
    playbook_id: Optional[str] = None,
) -> Run:
    run = Run(
        id=str(uuid.uuid4()),
        account_id=account_id,
        goal=goal,
        status=RunStatus.pending,
        autonomy=autonomy,
        playbook_id=playbook_id,
        budget=budget,
        created_at=_now(),
    )
    with session_scope() as s:
        s.add(RunORM(
            id=run.id,
            account_id=run.account_id,
            status=run.status.value,
            created_at=run.created_at,
            payload=run.model_dump(mode="json"),
        ))
    return run


def load_run(run_id: str, account_id: str) -> Optional[Run]:
    with session_scope() as s:
        row = s.scalar(
            select(RunORM).where(
                RunORM.id == run_id, RunORM.account_id == account_id,
            )
        )
        if row is None:
            return None
        return Run.model_validate(row.payload)


def _mutate(run_id: str, account_id: str, apply) -> None:
    """Load, mutate, and persist a run's payload inside one transaction."""
    with session_scope() as s:
        row = s.scalar(
            select(RunORM).where(
                RunORM.id == run_id, RunORM.account_id == account_id,
            ).with_for_update()
        )
        if row is None:
            raise KeyError(f"run {run_id} not found for account {account_id}")
        run = Run.model_validate(row.payload)
        apply(run)
        row.payload = run.model_dump(mode="json")
        row.status = run.status.value


def append_event(
    *, run: Run, type: RunEventType, payload: Dict[str, Any],
) -> RunEvent:
    with session_scope() as s:
        # Serialize appends for this run. Without this lock two concurrent
        # appends read the same max(seq) and collide on
        # UniqueConstraint("run_id", "seq"), losing one event to an
        # IntegrityError. Every append for a run goes through here, so
        # locking the parent row is sufficient.
        s.execute(
            select(RunORM.id).where(RunORM.id == run.id).with_for_update()
        )
        next_seq = s.scalar(
            select(func.coalesce(func.max(RunEventORM.seq), -1) + 1)
            .where(RunEventORM.run_id == run.id)
        )
        orm = RunEventORM(
            run_id=run.id,
            account_id=run.account_id,
            seq=int(next_seq),
            type=type.value,
            payload=payload,
            at=_now(),
        )
        s.add(orm)
        s.flush()  # assign the bigserial id before the session closes
        return RunEvent(
            id=orm.id, run_id=orm.run_id, account_id=orm.account_id,
            seq=orm.seq, type=type, payload=payload, at=orm.at,
        )


def events_since(
    *, run_id: str, account_id: str, after_id: int = 0, limit: int = 500,
) -> List[RunEvent]:
    with session_scope() as s:
        rows = s.scalars(
            select(RunEventORM)
            .where(
                RunEventORM.run_id == run_id,
                RunEventORM.account_id == account_id,
                RunEventORM.id > after_id,
            )
            .order_by(RunEventORM.id)
            .limit(limit)
        ).all()
        return [
            RunEvent(
                id=r.id, run_id=r.run_id, account_id=r.account_id, seq=r.seq,
                type=RunEventType(r.type), payload=r.payload, at=r.at,
            )
            for r in rows
        ]


def save_transcript(
    *, run_id: str, account_id: str,
    transcript: List[Dict[str, Any]], spend: Dict[str, Any],
) -> None:
    def apply(run: Run) -> None:
        run.transcript = transcript
        run.spend = spend

    _mutate(run_id, account_id, apply)


def set_status(
    *, run_id: str, account_id: str, status: RunStatus,
    error: Optional[str] = None, suspended_on: Optional[int] = None,
) -> None:
    def apply(run: Run) -> None:
        run.status = status
        if error is not None:
            run.error = error
        if suspended_on is not None:
            run.suspended_on = suspended_on
        if status in (RunStatus.done, RunStatus.failed, RunStatus.aborted):
            run.ended_at = _now()

    _mutate(run_id, account_id, apply)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd backend && .venv/Scripts/python -m pytest tests/test_agent_store.py -v`
Expected: PASS — all six tests.

- [ ] **Step 5: Commit**

```bash
git add backend/app/agent_runtime/ backend/tests/test_agent_store.py
git commit -m "feat(agent): account-scoped run store with append-only event log"
```

---

## Task 4: Artifact store

Decision 1 means a run resumes in a *different process*, so a `dataset_id` cannot be a key into an in-memory dict. Datasets persist to object storage and rehydrate.

`app/storage_s3.py` currently has `put_object` but **no read path** — this task adds one. Artifact keys must live under `accounts/{account_id}/` so the existing `delete_prefix` account purge covers them.

**Files:**
- Modify: `backend/requirements.txt` (add `pyarrow`)
- Modify: `backend/app/storage_s3.py` (add `get_object`)
- Modify: `backend/app/models.py` (add `RunArtifact`)
- Modify: `backend/app/db/models.py` (add `RunArtifactORM`)
- Create: `backend/app/agent_runtime/artifacts.py`
- Create: `backend/alembic/versions/0009_run_artifacts.py`
- Test: `backend/tests/test_agent_artifacts.py`

**Interfaces:**
- Consumes: `store.create_run` (Task 3)
- Produces:
  - `put_dataset(*, run_id: str, account_id: str, df: pd.DataFrame, label: str) -> str` (returns `dataset_id`)
  - `get_dataset(*, dataset_id: str, account_id: str) -> pd.DataFrame`
  - `describe(*, dataset_id: str, account_id: str) -> dict` — `{dataset_id, label, row_count, columns}`
  - `storage_key(*, dataset_id: str, account_id: str) -> str`
  - `app.storage_s3.get_object(key: str) -> bytes`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_agent_artifacts.py`:

```python
"""Datasets must survive a process boundary — a suspended run resumes elsewhere."""
from __future__ import annotations

import uuid

import pandas as pd
import pytest

from app.agent_runtime import artifacts, store
from app.db.base import session_scope
from app.db.models import AccountORM
from app.models import AutonomyLevel


@pytest.fixture()
def account_id() -> str:
    acct = str(uuid.uuid4())
    with session_scope() as s:
        s.add(AccountORM(id=acct, payload={}))
    return acct


@pytest.fixture()
def run(account_id):
    return store.create_run(
        account_id=account_id, goal={}, autonomy=AutonomyLevel.assist, budget={},
    )


def _frame() -> pd.DataFrame:
    return pd.DataFrame({
        "order_id": ["A-1", "A-2", "A-3"],
        "gross_total": [10.50, 20.25, 30.00],
        "placed_at": pd.to_datetime(["2026-06-01", "2026-06-02", "2026-06-03"]),
    })


def test_dataset_round_trips_with_dtypes_intact(run, account_id):
    dataset_id = artifacts.put_dataset(
        run_id=run.id, account_id=account_id, df=_frame(), label="orders",
    )
    loaded = artifacts.get_dataset(dataset_id=dataset_id, account_id=account_id)
    pd.testing.assert_frame_equal(loaded, _frame())


def test_get_dataset_is_account_scoped(run, account_id):
    dataset_id = artifacts.put_dataset(
        run_id=run.id, account_id=account_id, df=_frame(), label="orders",
    )
    other = str(uuid.uuid4())
    with session_scope() as s:
        s.add(AccountORM(id=other, payload={}))

    with pytest.raises(KeyError):
        artifacts.get_dataset(dataset_id=dataset_id, account_id=other)


def test_describe_is_bounded_and_carries_no_row_data(run, account_id):
    """Tool returns cross the model boundary — they carry no rows (spec 2.1)."""
    dataset_id = artifacts.put_dataset(
        run_id=run.id, account_id=account_id, df=_frame(), label="orders",
    )
    desc = artifacts.describe(dataset_id=dataset_id, account_id=account_id)

    assert desc["row_count"] == 3
    assert desc["columns"] == ["order_id", "gross_total", "placed_at"]
    assert desc["label"] == "orders"
    serialized = str(desc)
    assert "A-1" not in serialized
    assert "20.25" not in serialized


def test_storage_key_sits_under_the_account_purge_prefix(run, account_id):
    """delete_prefix purges accounts/{id}/ — artifacts must be inside it."""
    dataset_id = artifacts.put_dataset(
        run_id=run.id, account_id=account_id, df=_frame(), label="orders",
    )
    key = artifacts.storage_key(dataset_id=dataset_id, account_id=account_id)
    assert key.startswith("accounts/" + account_id + "/")
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd backend && .venv/Scripts/python -m pytest tests/test_agent_artifacts.py -v`
Expected: FAIL — `ImportError: cannot import name 'artifacts'`

- [ ] **Step 3: Add pyarrow and the S3 read path**

Add to `backend/requirements.txt`:

```
pyarrow==18.1.0
```

Install: `cd backend && .venv/Scripts/pip install pyarrow==18.1.0`

Add to `backend/app/storage_s3.py`, directly after `put_object`:

```python
def get_object(key: str) -> bytes:
    """Read one object's bytes. Raises botocore ClientError if absent."""
    response = _client().get_object(Bucket=bucket_name(), Key=key)
    return response["Body"].read()
```

- [ ] **Step 4: Add the RunArtifact model and ORM**

Append to `backend/app/models.py`:

```python
class RunArtifact(BaseModel):
    id: str
    run_id: str
    account_id: str
    kind: str = "dataset"
    label: str = ""
    storage_key: str
    schema_fingerprint: Dict[str, Any] = Field(default_factory=dict)
    row_count: int = 0
    columns: List[str] = Field(default_factory=list)
    created_at: Optional[str] = None
```

Append to `backend/app/db/models.py`:

```python
class RunArtifactORM(Base):
    __tablename__ = "run_artifacts"

    id = Column(String(36), primary_key=True)
    run_id = Column(String(36), ForeignKey("runs.id", ondelete="CASCADE"),
                    nullable=False, index=True)
    account_id = Column(String(36), ForeignKey("accounts.id", ondelete="CASCADE"),
                        nullable=False, index=True)
    kind = Column(String, nullable=False, default="dataset", index=True)
    payload = Column(JSONB, nullable=False, default=dict)
```

- [ ] **Step 5: Write the artifact store**

Create `backend/app/agent_runtime/artifacts.py`:

```python
"""Durable dataset handles for agent runs.

A run can suspend on a question and resume in a different process, so a
`dataset_id` cannot be a key into an in-memory dict (spec 1.4). Frames
persist as Parquet — which preserves dtypes, unlike CSV — and row-level data
never crosses the model boundary. Tools receive the id; only deterministic
code inside the process ever sees the frame.

Keys live under `accounts/{account_id}/` so the existing
`storage_s3.delete_prefix` account purge covers them.
"""
from __future__ import annotations

import io
import uuid
from datetime import datetime, timezone
from typing import Any, Dict

import pandas as pd
from sqlalchemy import select

from .. import storage_s3
from ..db.base import session_scope
from ..db.models import RunArtifactORM
from ..models import RunArtifact


def storage_key(*, dataset_id: str, account_id: str) -> str:
    return f"accounts/{account_id}/artifacts/{dataset_id}.parquet"


def put_dataset(
    *, run_id: str, account_id: str, df: pd.DataFrame, label: str,
) -> str:
    dataset_id = str(uuid.uuid4())
    key = storage_key(dataset_id=dataset_id, account_id=account_id)

    buffer = io.BytesIO()
    df.to_parquet(buffer, index=False)
    storage_s3.put_object(key, buffer.getvalue())

    artifact = RunArtifact(
        id=dataset_id,
        run_id=run_id,
        account_id=account_id,
        kind="dataset",
        label=label,
        storage_key=key,
        row_count=int(len(df)),
        columns=[str(c) for c in df.columns],
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    with session_scope() as s:
        s.add(RunArtifactORM(
            id=artifact.id, run_id=run_id, account_id=account_id,
            kind=artifact.kind, payload=artifact.model_dump(mode="json"),
        ))
    return dataset_id


def _load_row(*, dataset_id: str, account_id: str) -> RunArtifact:
    with session_scope() as s:
        row = s.scalar(
            select(RunArtifactORM).where(
                RunArtifactORM.id == dataset_id,
                RunArtifactORM.account_id == account_id,
            )
        )
        if row is None:
            raise KeyError(f"dataset {dataset_id} not found for this account")
        return RunArtifact.model_validate(row.payload)


def get_dataset(*, dataset_id: str, account_id: str) -> pd.DataFrame:
    artifact = _load_row(dataset_id=dataset_id, account_id=account_id)
    raw = storage_s3.get_object(artifact.storage_key)
    return pd.read_parquet(io.BytesIO(raw))


def describe(*, dataset_id: str, account_id: str) -> Dict[str, Any]:
    """Bounded summary safe to return to the model — no row data."""
    artifact = _load_row(dataset_id=dataset_id, account_id=account_id)
    return {
        "dataset_id": artifact.id,
        "label": artifact.label,
        "row_count": artifact.row_count,
        "columns": artifact.columns,
    }
```

- [ ] **Step 6: Write the migration**

Create `backend/alembic/versions/0009_run_artifacts.py`:

```python
"""create run_artifacts table

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-01
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "run_artifacts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("run_id", sa.String(length=36),
                  sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("account_id", sa.String(length=36),
                  sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(), nullable=False, server_default="dataset"),
        sa.Column("payload", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.create_index("ix_run_artifacts_run_id", "run_artifacts", ["run_id"])
    op.create_index("ix_run_artifacts_account_id", "run_artifacts", ["account_id"])
    op.create_index("ix_run_artifacts_kind", "run_artifacts", ["kind"])


def downgrade() -> None:
    op.drop_table("run_artifacts")
```

- [ ] **Step 7: Run the tests**

Run: `cd backend && .venv/Scripts/python -m pytest tests/test_agent_artifacts.py -v`
Expected: PASS — all four tests.

**Mock S3 — do not depend on a live store.** CI runs no MinIO and sets no `RECONOPS_S3_*` variables, so `storage_s3.bucket_name()` (a plain `os.environ[...]`) raises `KeyError` and the tests error rather than skip. Follow the convention every other storage-touching test in this repo already uses: an autouse fixture that monkeypatches the `RECONOPS_S3_*` vars, plus moto's `@mock_aws` on each test, creating the bucket inside the mock. Copy the exact shape from `tests/test_storage_s3.py`, `tests/test_async_jobs.py`, or `tests/test_membership.py`.

Moto intercepts at the botocore layer, so `put_dataset`/`get_dataset` still round-trip real bytes through the mocked store — the dtype-preservation assertion remains meaningful.

Note: against a *real* MinIO, `put_object`'s unconditional `ServerSideEncryption="AES256"` fails with "KMS not configured" on recent releases unless the server has a KMS backend (`MINIO_KMS_SECRET_KEY`). That is a local-dev environment concern, not a code change.

- [ ] **Step 8: Verify the migration round-trips**

```bash
cd backend
.venv/Scripts/python -m alembic upgrade head
.venv/Scripts/python -m alembic downgrade -1
.venv/Scripts/python -m alembic upgrade head
```

Expected: all three succeed.

- [ ] **Step 9: Commit**

```bash
git add backend/requirements.txt backend/app/storage_s3.py backend/app/models.py backend/app/db/models.py backend/app/agent_runtime/artifacts.py backend/alembic/versions/0009_run_artifacts.py backend/tests/test_agent_artifacts.py
git commit -m "feat(agent): durable dataset handles backed by object storage"
```

---

## Task 5: Tool registry

Effect classification and byte-stable serialization. This is where the prompt-cache strategy becomes mechanical rather than a matter of discipline.

**Files:**
- Create: `backend/app/agent_runtime/registry.py`
- Test: `backend/tests/test_agent_registry.py`

**Interfaces:**
- Consumes: `anthropic.beta_tool` (Task 1), `app.models.AutonomyLevel` (Task 2)
- Produces:
  - `Effect` — string enum `read | external | write`
  - `register(effect: Effect)` — decorator wrapping `@beta_tool`, records the effect
  - `record_effect(tool_name: str, effect: Effect) -> None` — for tier-2 tools defined elsewhere
  - `tier1_tools() -> list` — registered tools, **sorted by name**
  - `effect_of(tool_name: str) -> Effect`
  - `serialize_tools() -> str`
  - `requires_gate(tool_name: str, autonomy: AutonomyLevel) -> bool`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_agent_registry.py`:

```python
from __future__ import annotations

from app.agent_runtime import registry
from app.agent_runtime.registry import Effect
from app.models import AutonomyLevel


def test_registry_serializes_sorted_by_name():
    """Prompt caching is a byte-exact prefix match (spec 2.5)."""
    names = [t.__name__ for t in registry.tier1_tools()]
    assert names == sorted(names)


def test_serialization_is_byte_stable_across_calls():
    assert registry.serialize_tools() == registry.serialize_tools()


def test_every_registered_tool_declares_an_effect():
    for tool in registry.tier1_tools():
        assert registry.effect_of(tool.__name__) in set(Effect)


def test_read_tools_run_freely_at_assist():
    registry.record_effect("profile_schema", Effect.read)
    assert registry.requires_gate("profile_schema", AutonomyLevel.assist) is False


def test_external_tools_pause_at_assist_but_not_auto():
    registry.record_effect("shopify.pull_orders", Effect.external)
    assert registry.requires_gate("shopify.pull_orders", AutonomyLevel.assist) is True
    assert registry.requires_gate("shopify.pull_orders", AutonomyLevel.auto) is False


def test_write_tools_gate_at_every_level():
    registry.record_effect("create_rule", Effect.write)
    for level in AutonomyLevel:
        assert registry.requires_gate("create_rule", level) is True


def test_observe_gates_everything_including_reads():
    registry.record_effect("profile_schema", Effect.read)
    assert registry.requires_gate("profile_schema", AutonomyLevel.observe) is True


def test_unknown_tool_is_gated_rather_than_allowed():
    """Fail closed: an unregistered tool must never run unsupervised."""
    assert registry.requires_gate("not_a_real_tool", AutonomyLevel.auto) is True
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd backend && .venv/Scripts/python -m pytest tests/test_agent_registry.py -v`
Expected: FAIL — `ImportError: cannot import name 'registry'`

- [ ] **Step 3: Write the registry**

Create `backend/app/agent_runtime/registry.py`:

```python
"""Tool registration, effect classification, and canonical serialization.

Two invariants this module exists to enforce:

1. Sorted, byte-stable serialization. Prompt caching is a byte-exact prefix
   match and `tools` renders before `system`, so unstable ordering silently
   destroys the cache for every account at once — no error, just
   `cache_creation_input_tokens: 0` forever.

2. Every tool declares an effect, which is what makes the autonomy dial
   enforceable rather than decorative (spec 2.3).

Unknown tools fail closed: `requires_gate` returns True for anything it has
not seen, so a tool that skips registration cannot run unsupervised.
"""
from __future__ import annotations

import json
from enum import Enum
from typing import Any, Callable, Dict, List

from anthropic import beta_tool

from ..models import AutonomyLevel


class Effect(str, Enum):
    read = "read"
    external = "external"
    write = "write"


_TOOLS: Dict[str, Any] = {}
_EFFECTS: Dict[str, Effect] = {}


def record_effect(tool_name: str, effect: Effect) -> None:
    """Declare an effect for a tool defined outside this registry.

    Connection tools (`provider.verb`) and pack tools are tier-2 and reach
    the model through tool search, but the gate still needs their effect.
    """
    _EFFECTS[tool_name] = effect


def register(effect: Effect) -> Callable:
    """Decorate a plain function as a tier-1 agent tool."""

    def wrap(fn: Callable):
        tool = beta_tool(fn)
        name = fn.__name__
        _TOOLS[name] = tool
        _EFFECTS[name] = effect
        return tool

    return wrap


def tier1_tools() -> List[Any]:
    """Registered tools, sorted by name. The sort is load-bearing."""
    return [_TOOLS[name] for name in sorted(_TOOLS)]


def effect_of(tool_name: str) -> Effect:
    return _EFFECTS.get(tool_name, Effect.write)


def serialize_tools() -> str:
    """Canonical JSON of the tier-1 registry — the cache-stability check."""
    return json.dumps(
        [t.to_dict() for t in tier1_tools()],
        sort_keys=True,
        separators=(",", ":"),
    )


def requires_gate(tool_name: str, autonomy: AutonomyLevel) -> bool:
    """True when this call must pause for the user before executing."""
    if autonomy is AutonomyLevel.observe:
        return True

    effect = _EFFECTS.get(tool_name)
    if effect is None:
        return True  # fail closed

    if effect is Effect.read:
        return False
    if effect is Effect.external:
        return autonomy is not AutonomyLevel.auto
    return True  # write always gates
```

If Task 1 recorded a schema accessor other than `.to_dict()`, use that accessor in `serialize_tools`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd backend && .venv/Scripts/python -m pytest tests/test_agent_registry.py -v`
Expected: PASS. The first three tests pass trivially against an empty registry — Task 6 gives them real content.

- [ ] **Step 5: Commit**

```bash
git add backend/app/agent_runtime/registry.py backend/tests/test_agent_registry.py
git commit -m "feat(agent): tool registry with effect gating and stable serialization"
```

---

## Task 6: Run context and handle-based core tools

**The model must never supply `account_id`.** If it were a tool parameter, a hallucinated or injected value would cross account boundaries. Scope comes from a context variable the runtime sets before the loop starts; tools read it, the model cannot reach it.

**On sample values:** the constraint is *bounded* output, not zero row data. Concept induction needs a few sample values (the prototype's concept card shows `SHORT-01 · DMG-02`). Samples are capped at 3 per column and truncated to 32 characters. Anything that grows with row count is still forbidden.

**Files:**
- Create: `backend/app/agent_runtime/context.py`
- Create: `backend/app/agent_runtime/tools_core.py`
- Test: `backend/tests/test_agent_tools_core.py`

**Interfaces:**
- Consumes: `artifacts.get_dataset` / `describe` (Task 4), `registry.register` / `Effect` (Task 5), `app.tools.binding.bind_columns`, `app.tools.matching.match_by_key`
- Produces:
  - `context.RunContext` — dataclass `{run_id: str, account_id: str}`
  - `context.set_run_context(ctx)` / `context.current_run() -> RunContext`
  - `tools_core.profile_schema(dataset_id: str) -> dict`
  - `tools_core.bind_columns(dataset_id: str) -> dict`
  - `tools_core.match_datasets(dataset_a_id: str, dataset_b_id: str, key_a_column: str, key_b_column: str) -> dict`

`match_datasets` returns `{rows_in_a, rows_in_b, matched, unmatched_a, unmatched_b, fuzzy_count}` — Task 7's critic checks conservation against these exact keys.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_agent_tools_core.py`:

```python
from __future__ import annotations

import uuid

import pandas as pd
import pytest

from app.agent_runtime import artifacts, context, store, tools_core
from app.db.base import session_scope
from app.db.models import AccountORM
from app.models import AutonomyLevel


@pytest.fixture()
def run_ctx():
    acct = str(uuid.uuid4())
    with session_scope() as s:
        s.add(AccountORM(id=acct, payload={}))
    run = store.create_run(
        account_id=acct, goal={}, autonomy=AutonomyLevel.assist, budget={},
    )
    ctx = context.RunContext(run_id=run.id, account_id=acct)
    token = context.set_run_context(ctx)
    yield ctx
    context.reset_run_context(token)


def _orders() -> pd.DataFrame:
    return pd.DataFrame({
        "order_id": ["A-1", "A-2", "A-3", "A-4"],
        "gross_total": [10.0, 20.0, 30.0, 40.0],
    })


def _payments() -> pd.DataFrame:
    return pd.DataFrame({
        "reference": ["A-1", "A-2", "A-9"],
        "amount": [10.0, 20.0, 90.0],
    })


def test_tools_take_no_account_id_parameter():
    """Scope comes from context, never from model-supplied arguments."""
    for fn in (tools_core.profile_schema, tools_core.bind_columns,
               tools_core.match_datasets):
        schema = fn.to_dict()
        assert "account_id" not in schema["input_schema"]["properties"]


def test_profile_schema_returns_bounded_per_column_stats(run_ctx):
    ds = artifacts.put_dataset(
        run_id=run_ctx.run_id, account_id=run_ctx.account_id,
        df=_orders(), label="orders",
    )
    out = tools_core.profile_schema(ds)

    assert out["row_count"] == 4
    assert len(out["columns"]) == 2
    col = next(c for c in out["columns"] if c["name"] == "order_id")
    assert col["null_rate"] == 0.0
    assert col["cardinality"] == 4
    assert len(col["samples"]) <= 3


def test_profile_schema_caps_samples_regardless_of_row_count(run_ctx):
    big = pd.DataFrame({"x": [f"v{i}" for i in range(5000)]})
    ds = artifacts.put_dataset(
        run_id=run_ctx.run_id, account_id=run_ctx.account_id,
        df=big, label="big",
    )
    out = tools_core.profile_schema(ds)
    assert out["row_count"] == 5000
    assert len(out["columns"][0]["samples"]) == 3


def test_bind_columns_returns_mappings_not_rows(run_ctx):
    ds = artifacts.put_dataset(
        run_id=run_ctx.run_id, account_id=run_ctx.account_id,
        df=_orders(), label="orders",
    )
    out = tools_core.bind_columns(ds)

    assert out["dataset_id"] == ds
    assert out["total_count"] == 2
    assert isinstance(out["mappings"], list)
    for m in out["mappings"]:
        assert set(m) == {"column", "concept", "confidence"}


def test_match_datasets_conserves_rows(run_ctx):
    a = artifacts.put_dataset(
        run_id=run_ctx.run_id, account_id=run_ctx.account_id,
        df=_orders(), label="orders",
    )
    b = artifacts.put_dataset(
        run_id=run_ctx.run_id, account_id=run_ctx.account_id,
        df=_payments(), label="payments",
    )
    out = tools_core.match_datasets(a, b, "order_id", "reference")

    assert out["rows_in_a"] == 4
    assert out["rows_in_b"] == 3
    assert out["matched"] + out["unmatched_a"] == out["rows_in_a"]
    assert out["matched"] + out["unmatched_b"] == out["rows_in_b"]


def test_current_run_raises_outside_a_run():
    context.reset_run_context(context.set_run_context(None))
    with pytest.raises(RuntimeError):
        context.current_run()
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd backend && .venv/Scripts/python -m pytest tests/test_agent_tools_core.py -v`
Expected: FAIL — `ImportError: cannot import name 'context'`

- [ ] **Step 3: Write the run context**

Create `backend/app/agent_runtime/context.py`:

```python
"""Ambient run scope for tool execution.

`account_id` is deliberately NOT a tool parameter. If the model could supply
it, a hallucinated or injected value would cross account boundaries. The
runtime sets this context variable before the loop starts; tools read it and
the model cannot reach it.
"""
from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class RunContext:
    run_id: str
    account_id: str


_CURRENT: ContextVar[Optional[RunContext]] = ContextVar(
    "agent_run_context", default=None,
)


def set_run_context(ctx: Optional[RunContext]) -> Token:
    return _CURRENT.set(ctx)


def reset_run_context(token: Token) -> None:
    _CURRENT.reset(token)


def current_run() -> RunContext:
    ctx = _CURRENT.get()
    if ctx is None:
        raise RuntimeError("no active agent run context")
    return ctx
```

- [ ] **Step 4: Write the core tools**

Create `backend/app/agent_runtime/tools_core.py`:

```python
"""Tier-1 tools: handle-based wrappers over the deterministic tools.

Every function takes references and returns a bounded summary (spec 2.1).
Row data never crosses the model boundary, with one deliberate exception:
capped, truncated sample values, which concept induction needs.

Account scope comes from `context.current_run()`, never from a parameter.
"""
from __future__ import annotations

from typing import Any, Dict, List

import pandas as pd

from ..tools import binding as binding_tool
from ..tools import matching as matching_tool
from . import artifacts
from .context import current_run
from .registry import Effect, register

MAX_SAMPLES = 3
MAX_SAMPLE_CHARS = 32
MAX_UNBOUND_REPORTED = 25


def _samples(series: pd.Series) -> List[str]:
    out: List[str] = []
    for value in series.dropna().unique()[:MAX_SAMPLES]:
        out.append(str(value)[:MAX_SAMPLE_CHARS])
    return out


@register(Effect.read)
def profile_schema(dataset_id: str) -> Dict[str, Any]:
    """Fingerprint a dataset's columns: dtype, null rate, cardinality, samples.

    Call this first on any dataset you have not seen, before bind_columns.
    Returns bounded per-column statistics — never the underlying rows.

    Args:
        dataset_id: Handle returned when the dataset was loaded.
    """
    ctx = current_run()
    df = artifacts.get_dataset(dataset_id=dataset_id, account_id=ctx.account_id)

    columns: List[Dict[str, Any]] = []
    for name in df.columns:
        series = df[name]
        columns.append({
            "name": str(name),
            "dtype": str(series.dtype),
            "null_rate": round(float(series.isna().mean()), 4),
            "cardinality": int(series.nunique(dropna=True)),
            "samples": _samples(series),
        })

    return {
        "dataset_id": dataset_id,
        "row_count": int(len(df)),
        "columns": columns,
    }


@register(Effect.read)
def bind_columns(dataset_id: str) -> Dict[str, Any]:
    """Map a dataset's columns onto ontology concepts.

    Call this after profile_schema on any newly loaded dataset, and before
    matching or classifying. Returns which columns bound and which did not.

    Args:
        dataset_id: Handle returned when the dataset was loaded.
    """
    ctx = current_run()
    df = artifacts.get_dataset(dataset_id=dataset_id, account_id=ctx.account_id)

    bindings = binding_tool.bind_columns(df, account_id=ctx.account_id)
    bound_names = {b.column_name for b in bindings}
    unbound = [str(c) for c in df.columns if str(c) not in bound_names]

    return {
        "dataset_id": dataset_id,
        "total_count": int(len(df.columns)),
        "bound_count": len(bound_names),
        "mappings": [
            {
                "column": b.column_name,
                "concept": b.concept_id,
                "confidence": round(float(b.confidence), 3),
            }
            for b in bindings
        ],
        "unbound": unbound[:MAX_UNBOUND_REPORTED],
        "unbound_truncated": len(unbound) > MAX_UNBOUND_REPORTED,
    }


@register(Effect.read)
def match_datasets(
    dataset_a_id: str,
    dataset_b_id: str,
    key_a_column: str,
    key_b_column: str,
) -> Dict[str, Any]:
    """Join two datasets on a key column, exact first then fuzzy.

    Call this once both datasets are bound and you have chosen a key column
    on each side. Returns counts only — use the run's artifacts to inspect
    individual rows.

    Args:
        dataset_a_id: Handle for the left dataset.
        dataset_b_id: Handle for the right dataset.
        key_a_column: Column in the left dataset to join on.
        key_b_column: Column in the right dataset to join on.
    """
    ctx = current_run()
    df_a = artifacts.get_dataset(dataset_id=dataset_a_id, account_id=ctx.account_id)
    df_b = artifacts.get_dataset(dataset_id=dataset_b_id, account_id=ctx.account_id)

    result = matching_tool.match_by_key(df_a, df_b, key_a_column, key_b_column)

    return {
        "rows_in_a": int(len(df_a)),
        "rows_in_b": int(len(df_b)),
        "matched": len(result.matches),
        "unmatched_a": len(result.unmatched_a_idx),
        "unmatched_b": len(result.unmatched_b_idx),
        "fuzzy_count": int(result.fuzzy_count),
    }
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd backend && .venv/Scripts/python -m pytest tests/test_agent_tools_core.py tests/test_agent_registry.py -v`
Expected: PASS. The registry tests now run against three real tools, so the sorted-order and byte-stability assertions have actual content.

- [ ] **Step 6: Commit**

```bash
git add backend/app/agent_runtime/context.py backend/app/agent_runtime/tools_core.py backend/tests/test_agent_tools_core.py
git commit -m "feat(agent): run context and handle-based core tools"
```

---

## Task 7: Critic

Deterministic post-conditions on tool output, keyed by tool name. A failed check is never swallowed — it emits an event and stops the run. Pack invariants will register into this same table in Phase E, which is what lets a domain pack extend the critic with zero core edits.

**Files:**
- Create: `backend/app/agent_runtime/critic.py`
- Test: `backend/tests/test_agent_critic.py`

**Interfaces:**
- Consumes: the output dicts produced by `tools_core` (Task 6)
- Produces:
  - `CriticResult` — dataclass `{passed: bool, failures: list[str]}`
  - `register_check(tool_name: str, fn: Callable[[dict], str | None]) -> None`
  - `check(tool_name: str, output: dict) -> CriticResult`

A check returns `None` when it passes, or a human-readable failure string.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_agent_critic.py`:

```python
from __future__ import annotations

from app.agent_runtime import critic


def test_unknown_tool_passes_vacuously():
    result = critic.check("some_tool_with_no_checks", {"anything": 1})
    assert result.passed is True
    assert result.failures == []


def test_match_conservation_passes_on_balanced_counts():
    result = critic.check("match_datasets", {
        "rows_in_a": 4, "rows_in_b": 3,
        "matched": 2, "unmatched_a": 2, "unmatched_b": 1,
        "fuzzy_count": 0,
    })
    assert result.passed is True


def test_match_conservation_fails_when_rows_vanish():
    """The whole point: a tool cannot lose rows without the run aborting."""
    result = critic.check("match_datasets", {
        "rows_in_a": 4, "rows_in_b": 3,
        "matched": 2, "unmatched_a": 1, "unmatched_b": 1,
        "fuzzy_count": 0,
    })
    assert result.passed is False
    assert any("side A" in f for f in result.failures)


def test_bind_counts_must_not_exceed_total():
    result = critic.check("bind_columns", {
        "dataset_id": "d", "total_count": 2, "bound_count": 5,
        "mappings": [], "unbound": [],
    })
    assert result.passed is False


def test_custom_checks_register_and_run():
    """Pack invariants use this path in Phase E."""
    critic.register_check(
        "pack_tool",
        lambda out: None if out.get("ok") else "pack invariant violated",
    )
    assert critic.check("pack_tool", {"ok": True}).passed is True

    failed = critic.check("pack_tool", {"ok": False})
    assert failed.passed is False
    assert failed.failures == ["pack invariant violated"]


def test_a_raising_check_is_reported_not_propagated():
    """A broken check must fail the run, not crash the loop."""
    critic.register_check("boom", lambda out: 1 / 0)
    result = critic.check("boom", {})
    assert result.passed is False
    assert any("ZeroDivisionError" in f for f in result.failures)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd backend && .venv/Scripts/python -m pytest tests/test_agent_critic.py -v`
Expected: FAIL — `ImportError: cannot import name 'critic'`

- [ ] **Step 3: Write the critic**

Create `backend/app/agent_runtime/critic.py`:

```python
"""Deterministic post-conditions on tool output.

The agent orchestrates; tools decide. The critic is what makes that
enforceable — every tool's output is checked against invariants that do not
involve the model, and a failure aborts rather than being swallowed
(spec 2.4).

Checks are keyed by tool name so pack invariants can register into the same
table in Phase E without touching core.
"""
from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

Check = Callable[[Dict[str, Any]], Optional[str]]

_CHECKS: Dict[str, List[Check]] = {}


@dataclass
class CriticResult:
    passed: bool
    failures: List[str] = field(default_factory=list)


def register_check(tool_name: str, fn: Check) -> None:
    _CHECKS.setdefault(tool_name, []).append(fn)


def check(tool_name: str, output: Dict[str, Any]) -> CriticResult:
    failures: List[str] = []
    for fn in _CHECKS.get(tool_name, []):
        try:
            problem = fn(output)
        except Exception as exc:  # a broken check fails the run, not the loop
            problem = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
        if problem:
            failures.append(problem)
    return CriticResult(passed=not failures, failures=failures)


# --- built-in checks -------------------------------------------------------

def _match_conserves_rows(out: Dict[str, Any]) -> Optional[str]:
    a_in = out.get("rows_in_a", 0)
    b_in = out.get("rows_in_b", 0)
    matched = out.get("matched", 0)
    if matched + out.get("unmatched_a", 0) != a_in:
        return (
            f"row conservation failed on side A: matched {matched} + "
            f"unmatched {out.get('unmatched_a', 0)} != {a_in} rows in"
        )
    if matched + out.get("unmatched_b", 0) != b_in:
        return (
            f"row conservation failed on side B: matched {matched} + "
            f"unmatched {out.get('unmatched_b', 0)} != {b_in} rows in"
        )
    return None


def _bind_counts_are_sane(out: Dict[str, Any]) -> Optional[str]:
    total = out.get("total_count", 0)
    bound = out.get("bound_count", 0)
    if bound > total:
        return f"bound_count {bound} exceeds total_count {total}"
    return None


register_check("match_datasets", _match_conserves_rows)
register_check("bind_columns", _bind_counts_are_sane)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd backend && .venv/Scripts/python -m pytest tests/test_agent_critic.py -v`
Expected: PASS — all six tests.

- [ ] **Step 5: Commit**

```bash
git add backend/app/agent_runtime/critic.py backend/tests/test_agent_critic.py
git commit -m "feat(agent): critic registry with row-conservation invariants"
```

---

## Task 8: Budget enforcement

Two budgets exist (spec §2.7). This task builds the hard caps we enforce ourselves; `task_budget` is a request parameter handled in Task 9.

**Files:**
- Create: `backend/app/agent_runtime/budget.py`
- Test: `backend/tests/test_agent_budget.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `Budget` — dataclass `{usd_cap: float | None, tool_call_cap: int | None, wall_clock_s: int | None, task_budget_tokens: int | None}`, with `Budget.from_dict(d)` and `Budget.default()`
  - `Spend` — dataclass `{tool_calls: int, usd: float, started_at: float}`, with `record_tool_call()`, `record_llm(usd)`, `elapsed_s()`, `to_dict()`
  - `exceeded(budget: Budget, spend: Spend) -> str | None` — reason string or None

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_agent_budget.py`:

```python
from __future__ import annotations

import time

from app.agent_runtime.budget import Budget, Spend, exceeded


def test_default_budget_matches_the_prototype_plan_block():
    b = Budget.default()
    assert b.usd_cap == 0.40
    assert b.tool_call_cap == 30
    assert b.wall_clock_s == 120


def test_from_dict_falls_back_to_defaults_for_missing_keys():
    b = Budget.from_dict({"tool_call_cap": 5})
    assert b.tool_call_cap == 5
    assert b.usd_cap == 0.40


def test_within_budget_returns_none():
    assert exceeded(Budget.default(), Spend()) is None


def test_tool_call_cap_trips():
    spend = Spend()
    for _ in range(6):
        spend.record_tool_call()
    reason = exceeded(Budget.from_dict({"tool_call_cap": 5}), spend)
    assert reason is not None
    assert "tool call" in reason


def test_usd_cap_trips():
    spend = Spend()
    spend.record_llm(0.51)
    reason = exceeded(Budget.from_dict({"usd_cap": 0.50}), spend)
    assert reason is not None
    assert "budget" in reason


def test_wall_clock_cap_trips():
    spend = Spend(started_at=time.monotonic() - 10)
    reason = exceeded(Budget.from_dict({"wall_clock_s": 5}), spend)
    assert reason is not None
    assert "wall clock" in reason


def test_none_cap_means_unlimited():
    spend = Spend()
    for _ in range(1000):
        spend.record_tool_call()
    assert exceeded(Budget(tool_call_cap=None), spend) is None


def test_spend_serializes_for_persistence():
    spend = Spend()
    spend.record_tool_call()
    spend.record_llm(0.02)
    d = spend.to_dict()
    assert d["tool_calls"] == 1
    assert d["usd"] == 0.02
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd backend && .venv/Scripts/python -m pytest tests/test_agent_budget.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.agent_runtime.budget'`

- [ ] **Step 3: Write the budget module**

Create `backend/app/agent_runtime/budget.py`:

```python
"""Hard caps on a run, enforced by us.

Distinct from `output_config.task_budget`, which is model-aware and makes
the agent pace itself. These caps are the backstop: they stop a run that is
overreaching regardless of what the model intends (spec 2.7). The defaults
match the budget line shown in the prototype's plan block.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

DEFAULT_USD_CAP = 0.40
DEFAULT_TOOL_CALL_CAP = 30
DEFAULT_WALL_CLOCK_S = 120
DEFAULT_TASK_BUDGET_TOKENS = 64_000


@dataclass(frozen=True)
class Budget:
    usd_cap: Optional[float] = DEFAULT_USD_CAP
    tool_call_cap: Optional[int] = DEFAULT_TOOL_CALL_CAP
    wall_clock_s: Optional[int] = DEFAULT_WALL_CLOCK_S
    task_budget_tokens: Optional[int] = DEFAULT_TASK_BUDGET_TOKENS

    @staticmethod
    def default() -> "Budget":
        return Budget()

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Budget":
        base = Budget()
        return Budget(
            usd_cap=d.get("usd_cap", base.usd_cap),
            tool_call_cap=d.get("tool_call_cap", base.tool_call_cap),
            wall_clock_s=d.get("wall_clock_s", base.wall_clock_s),
            task_budget_tokens=d.get("task_budget_tokens", base.task_budget_tokens),
        )


@dataclass
class Spend:
    tool_calls: int = 0
    usd: float = 0.0
    started_at: float = field(default_factory=time.monotonic)

    def record_tool_call(self) -> None:
        self.tool_calls += 1

    def record_llm(self, usd: float) -> None:
        self.usd += usd

    def elapsed_s(self) -> float:
        return time.monotonic() - self.started_at

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool_calls": self.tool_calls,
            "usd": round(self.usd, 6),
            "elapsed_s": round(self.elapsed_s(), 3),
        }


def exceeded(budget: Budget, spend: Spend) -> Optional[str]:
    """Return a human-readable reason, or None when still within budget."""
    if budget.tool_call_cap is not None and spend.tool_calls > budget.tool_call_cap:
        return (
            f"tool call cap reached: {spend.tool_calls} of "
            f"{budget.tool_call_cap}"
        )
    if budget.usd_cap is not None and spend.usd > budget.usd_cap:
        return f"budget reached: ${spend.usd:.2f} of ${budget.usd_cap:.2f}"
    if budget.wall_clock_s is not None and spend.elapsed_s() > budget.wall_clock_s:
        return (
            f"wall clock cap reached: {spend.elapsed_s():.0f}s of "
            f"{budget.wall_clock_s}s"
        )
    return None
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd backend && .venv/Scripts/python -m pytest tests/test_agent_budget.py -v`
Expected: PASS — all eight tests.

- [ ] **Step 5: Commit**

```bash
git add backend/app/agent_runtime/budget.py backend/tests/test_agent_budget.py
git commit -m "feat(agent): per-run hard budget caps"
```

---

## Task 9: The runtime loop

Ties everything together: Tool Runner iteration, transcript mirroring, event emission, critic gates, budget enforcement, and suspend-on-gate.

**A seam for testability.** The loop talks to a `Driver`, not to the SDK directly. `AnthropicDriver` wraps `client.beta.messages.tool_runner`; `FakeDriver` lets the whole loop be tested without network. This is not indirection for its own sake — every gate, critic path, and budget trip needs deterministic tests, and scripting them through a real model is not possible.

**Transcript mirroring** (spec §2.6): the Python Tool Runner keeps its own message history and does not expose it, so we maintain `messages` ourselves and persist it. On resume we build a **fresh** runner from the persisted history.

**Files:**
- Create: `backend/app/agent_runtime/runtime.py`
- Modify: `backend/app/agent_runtime/__init__.py`
- Test: `backend/tests/test_agent_runtime.py`

**Interfaces:**
- Consumes: `store` (Task 3), `artifacts` (Task 4), `registry` (Task 5), `context` + `tools_core` (Task 6), `critic` (Task 7), `budget` (Task 8)
- Produces:
  - `Turn` — dataclass `{text: str | None, tool_calls: list[ToolCall]}`
  - `ToolCall` — dataclass `{id: str, name: str, input: dict}`
  - `Driver` — protocol with `next_turn(*, system, messages, tools, task_budget_tokens) -> Turn`
  - `AnthropicDriver` — the real implementation
  - `execute_run(*, run_id: str, account_id: str, driver: Driver | None = None) -> Run`
  - `SYSTEM_PROMPT` — the fixed instruction block

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_agent_runtime.py`:

```python
from __future__ import annotations

import uuid

import pandas as pd
import pytest

from app.agent_runtime import artifacts, runtime, store
from app.agent_runtime.runtime import Turn, ToolCall
from app.db.base import session_scope
from app.db.models import AccountORM
from app.models import AutonomyLevel, RunEventType, RunStatus


class FakeDriver:
    """Replays scripted turns so gates and critic paths are deterministic."""

    def __init__(self, turns):
        self._turns = list(turns)
        self.seen_systems = []
        self.seen_tool_counts = []

    def next_turn(self, *, system, messages, tools, task_budget_tokens):
        self.seen_systems.append(system)
        self.seen_tool_counts.append(len(tools))
        if not self._turns:
            return Turn(text="done", tool_calls=[])
        return self._turns.pop(0)


@pytest.fixture()
def account_id() -> str:
    acct = str(uuid.uuid4())
    with session_scope() as s:
        s.add(AccountORM(id=acct, payload={}))
    return acct


def _make_run(account_id, autonomy=AutonomyLevel.assist, budget=None):
    return store.create_run(
        account_id=account_id, goal={"intent": "test"},
        autonomy=autonomy, budget=budget or {},
    )


def _dataset(run, account_id):
    return artifacts.put_dataset(
        run_id=run.id, account_id=account_id,
        df=pd.DataFrame({"order_id": ["A-1", "A-2"], "gross_total": [1.0, 2.0]}),
        label="orders",
    )


def test_text_only_run_finishes_and_logs_events(account_id):
    run = _make_run(account_id)
    driver = FakeDriver([Turn(text="June is settled.", tool_calls=[])])

    result = runtime.execute_run(
        run_id=run.id, account_id=account_id, driver=driver,
    )

    assert result.status is RunStatus.done
    types = [e.type for e in store.events_since(
        run_id=run.id, account_id=account_id,
    )]
    assert RunEventType.goal_received in types
    assert RunEventType.assistant_text in types
    assert RunEventType.run_finished in types


def test_read_tool_executes_and_emits_call_and_return(account_id):
    run = _make_run(account_id)
    ds = _dataset(run, account_id)
    driver = FakeDriver([
        Turn(text=None, tool_calls=[
            ToolCall(id="t1", name="profile_schema", input={"dataset_id": ds}),
        ]),
        Turn(text="profiled", tool_calls=[]),
    ])

    runtime.execute_run(run_id=run.id, account_id=account_id, driver=driver)

    events = store.events_since(run_id=run.id, account_id=account_id)
    types = [e.type for e in events]
    assert RunEventType.tool_called in types
    assert RunEventType.tool_returned in types
    returned = next(e for e in events if e.type == RunEventType.tool_returned)
    assert returned.payload["tool"] == "profile_schema"
    assert returned.payload["output"]["row_count"] == 2


def test_transcript_is_persisted_for_resume(account_id):
    run = _make_run(account_id)
    driver = FakeDriver([Turn(text="hello", tool_calls=[])])

    runtime.execute_run(run_id=run.id, account_id=account_id, driver=driver)

    reloaded = store.load_run(run.id, account_id)
    assert len(reloaded.transcript) >= 2
    assert reloaded.transcript[0]["role"] == "user"


def test_observe_mode_suspends_before_running_a_read_tool(account_id):
    """The dial is enforced by the loop, not by prompting."""
    run = _make_run(account_id, autonomy=AutonomyLevel.observe)
    ds = _dataset(run, account_id)
    driver = FakeDriver([
        Turn(text=None, tool_calls=[
            ToolCall(id="t1", name="profile_schema", input={"dataset_id": ds}),
        ]),
    ])

    result = runtime.execute_run(
        run_id=run.id, account_id=account_id, driver=driver,
    )

    assert result.status is RunStatus.suspended
    assert result.suspended_on is not None
    types = [e.type for e in store.events_since(
        run_id=run.id, account_id=account_id,
    )]
    assert RunEventType.question_asked in types
    assert RunEventType.tool_returned not in types


def test_critic_failure_aborts_the_run(account_id):
    """A failed post-condition is never swallowed (spec 2.4)."""
    from app.agent_runtime import critic

    critic.register_check("profile_schema", lambda out: "synthetic failure")
    run = _make_run(account_id)
    ds = _dataset(run, account_id)
    driver = FakeDriver([
        Turn(text=None, tool_calls=[
            ToolCall(id="t1", name="profile_schema", input={"dataset_id": ds}),
        ]),
    ])

    result = runtime.execute_run(
        run_id=run.id, account_id=account_id, driver=driver,
    )

    assert result.status is RunStatus.aborted
    events = store.events_since(run_id=run.id, account_id=account_id)
    failed = [e for e in events if e.type == RunEventType.critic_check]
    assert failed and failed[-1].payload["passed"] is False


def test_tool_call_cap_stops_the_run(account_id):
    run = _make_run(account_id, budget={"tool_call_cap": 1})
    ds = _dataset(run, account_id)
    call = ToolCall(id="t", name="profile_schema", input={"dataset_id": ds})
    driver = FakeDriver([
        Turn(text=None, tool_calls=[call]),
        Turn(text=None, tool_calls=[call]),
        Turn(text=None, tool_calls=[call]),
    ])

    result = runtime.execute_run(
        run_id=run.id, account_id=account_id, driver=driver,
    )

    assert result.status is RunStatus.aborted
    types = [e.type for e in store.events_since(
        run_id=run.id, account_id=account_id,
    )]
    assert RunEventType.budget_exceeded in types


def test_failing_tool_emits_tool_failed_and_continues(account_id):
    run = _make_run(account_id)
    driver = FakeDriver([
        Turn(text=None, tool_calls=[
            ToolCall(id="t1", name="profile_schema",
                     input={"dataset_id": "does-not-exist"}),
        ]),
        Turn(text="recovered", tool_calls=[]),
    ])

    result = runtime.execute_run(
        run_id=run.id, account_id=account_id, driver=driver,
    )

    assert result.status is RunStatus.done
    types = [e.type for e in store.events_since(
        run_id=run.id, account_id=account_id,
    )]
    assert RunEventType.tool_failed in types


def test_system_prompt_puts_core_instructions_first(account_id):
    """Cache layering: the stable block must precede account-specific text."""
    run = _make_run(account_id)
    driver = FakeDriver([Turn(text="ok", tool_calls=[])])

    runtime.execute_run(run_id=run.id, account_id=account_id, driver=driver)

    system = driver.seen_systems[0]
    assert isinstance(system, list)
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert "never compute money" in system[0]["text"]
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd backend && .venv/Scripts/python -m pytest tests/test_agent_runtime.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.agent_runtime.runtime'`

- [ ] **Step 3: Write the runtime**

Create `backend/app/agent_runtime/runtime.py`:

```python
"""The plan-execute-verify loop.

Drives the Anthropic Tool Runner over the tier-1 registry, mirroring the
message history into `runs.transcript` so a run can suspend on a gate and
resume in a different process (spec 1.2, 2.6).

Every tool call passes three gates before its result reaches the model:
autonomy (registry.requires_gate), post-conditions (critic.check), and hard
caps (budget.exceeded). A gate that trips writes an event and stops the run
— nothing is swallowed.

The `Driver` seam exists so the loop is testable without network. Gates,
critic paths, and budget trips all need deterministic coverage, and those
cannot be scripted through a live model.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol

from ..llm import DEFAULT_MODEL
from ..models import Run, RunEventType, RunStatus
from . import artifacts, critic, registry, store, tools_core  # noqa: F401
from .budget import Budget, Spend, exceeded
from .context import RunContext, reset_run_context, set_run_context

SYSTEM_PROMPT = """You are an operational data agent.

You orchestrate deterministic tools over the user's data. You never compute
money yourself: every number you report must come from a tool result. If a
tool has not produced a figure, you do not have it.

Work from handles. Tools take dataset ids and column names, never rows.

Prefer acting to narrating. When a goal is small enough to answer in a few
tool calls, just make them — do not propose a plan first."""

MAX_ITERATIONS = 40


@dataclass
class ToolCall:
    id: str
    name: str
    input: Dict[str, Any]


@dataclass
class Turn:
    text: Optional[str] = None
    tool_calls: List[ToolCall] = field(default_factory=list)


class Driver(Protocol):
    def next_turn(
        self, *, system, messages, tools, task_budget_tokens,
    ) -> Turn: ...


class AnthropicDriver:
    """Wraps the SDK Tool Runner, surfacing one turn at a time.

    The runner executes registered tools itself; we intercept each yielded
    assistant message so the loop can gate before results are accepted.
    """

    def __init__(self, model: str = DEFAULT_MODEL):
        self.model = model

    def next_turn(self, *, system, messages, tools, task_budget_tokens) -> Turn:
        import os

        # Same gate the rest of the codebase uses (app/llm.py). Without this,
        # any test that reaches the runtime through HTTP makes a live call.
        if os.getenv("RECONOPS_STUB_LLM") == "1":
            return Turn(text="[stubbed] no action taken.", tool_calls=[])

        import anthropic

        client = anthropic.Anthropic()
        runner = client.beta.messages.tool_runner(
            model=self.model,
            max_tokens=16000,
            system=system,
            messages=messages,
            tools=tools,
            thinking={"type": "adaptive"},
            output_config={
                "effort": "xhigh",
                "task_budget": {"type": "tokens", "total": task_budget_tokens},
            },
            betas=["task-budgets-2026-03-13"],
        )
        message = next(iter(runner))

        text_parts = [b.text for b in message.content if b.type == "text"]
        calls = [
            ToolCall(id=b.id, name=b.name, input=dict(b.input))
            for b in message.content if b.type == "tool_use"
        ]
        return Turn(
            text="".join(text_parts) or None,
            tool_calls=calls,
        )


def _system_blocks(run: Run) -> List[Dict[str, Any]]:
    """Stable instructions first, with the cache breakpoint on them.

    Account-specific context is appended after this block in later phases;
    keeping the stable text first is what lets the prefix cache be shared
    across every account (spec 4.1).
    """
    return [{
        "type": "text",
        "text": SYSTEM_PROMPT,
        "cache_control": {"type": "ephemeral"},
    }]


def _goal_text(run: Run) -> str:
    intent = run.goal.get("intent", "")
    entities = run.goal.get("entities") or {}
    if entities:
        return f"{intent}\n\nContext: {json.dumps(entities, sort_keys=True)}"
    return intent or "Help with this account's data."


def _dispatch(name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    fn = getattr(tools_core, name, None)
    if fn is None:
        raise KeyError(f"unknown tool {name}")
    return fn(**payload)


def execute_run(
    *, run_id: str, account_id: str, driver: Optional[Driver] = None,
) -> Run:
    run = store.load_run(run_id, account_id)
    if run is None:
        raise KeyError(f"run {run_id} not found for account {account_id}")

    driver = driver or AnthropicDriver()
    budget = Budget.from_dict(run.budget)
    spend = Spend()
    tools = registry.tier1_tools()

    token = set_run_context(RunContext(run_id=run.id, account_id=account_id))
    store.set_status(
        run_id=run.id, account_id=account_id, status=RunStatus.running,
    )
    store.append_event(
        run=run, type=RunEventType.goal_received, payload=run.goal,
    )

    messages: List[Dict[str, Any]] = [
        {"role": "user", "content": _goal_text(run)},
    ]
    final_status = RunStatus.done

    try:
        for _ in range(MAX_ITERATIONS):
            turn = driver.next_turn(
                system=_system_blocks(run),
                messages=messages,
                tools=tools,
                task_budget_tokens=budget.task_budget_tokens,
            )

            assistant_content: List[Dict[str, Any]] = []
            if turn.text:
                assistant_content.append({"type": "text", "text": turn.text})
                store.append_event(
                    run=run, type=RunEventType.assistant_text,
                    payload={"text": turn.text},
                )
            for call in turn.tool_calls:
                assistant_content.append({
                    "type": "tool_use", "id": call.id,
                    "name": call.name, "input": call.input,
                })
            messages.append({"role": "assistant", "content": assistant_content})

            if not turn.tool_calls:
                break

            results: List[Dict[str, Any]] = []
            for call in turn.tool_calls:
                if registry.requires_gate(call.name, run.autonomy):
                    q = store.append_event(
                        run=run, type=RunEventType.question_asked,
                        payload={
                            "text": f"Run {call.name}?",
                            "tool": call.name, "input": call.input,
                        },
                    )
                    store.save_transcript(
                        run_id=run.id, account_id=account_id,
                        transcript=messages, spend=spend.to_dict(),
                    )
                    store.set_status(
                        run_id=run.id, account_id=account_id,
                        status=RunStatus.suspended, suspended_on=q.id,
                    )
                    return store.load_run(run.id, account_id)

                store.append_event(
                    run=run, type=RunEventType.tool_called,
                    payload={"tool": call.name, "input": call.input},
                )
                spend.record_tool_call()

                try:
                    output = _dispatch(call.name, call.input)
                except Exception as exc:
                    store.append_event(
                        run=run, type=RunEventType.tool_failed,
                        payload={
                            "tool": call.name,
                            "error": f"{type(exc).__name__}: {exc}",
                        },
                    )
                    results.append({
                        "type": "tool_result", "tool_use_id": call.id,
                        "content": f"error: {exc}", "is_error": True,
                    })
                    continue

                verdict = critic.check(call.name, output)
                store.append_event(
                    run=run, type=RunEventType.critic_check,
                    payload={
                        "tool": call.name, "passed": verdict.passed,
                        "failures": verdict.failures,
                    },
                )
                if not verdict.passed:
                    final_status = RunStatus.aborted
                    store.set_status(
                        run_id=run.id, account_id=account_id,
                        status=final_status,
                        error="; ".join(verdict.failures),
                    )
                    return store.load_run(run.id, account_id)

                store.append_event(
                    run=run, type=RunEventType.tool_returned,
                    payload={"tool": call.name, "output": output},
                )
                results.append({
                    "type": "tool_result", "tool_use_id": call.id,
                    "content": json.dumps(output, default=str),
                })

            messages.append({"role": "user", "content": results})

            reason = exceeded(budget, spend)
            if reason:
                store.append_event(
                    run=run, type=RunEventType.budget_exceeded,
                    payload={"reason": reason},
                )
                final_status = RunStatus.aborted
                store.set_status(
                    run_id=run.id, account_id=account_id,
                    status=final_status, error=reason,
                )
                return store.load_run(run.id, account_id)

        store.append_event(
            run=run, type=RunEventType.run_finished,
            payload={"spend": spend.to_dict()},
        )
        store.save_transcript(
            run_id=run.id, account_id=account_id,
            transcript=messages, spend=spend.to_dict(),
        )
        store.set_status(
            run_id=run.id, account_id=account_id, status=final_status,
        )
        return store.load_run(run.id, account_id)

    except Exception as exc:
        store.set_status(
            run_id=run.id, account_id=account_id, status=RunStatus.failed,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    finally:
        reset_run_context(token)
```

- [ ] **Step 4: Export the public surface**

Replace `backend/app/agent_runtime/__init__.py` with:

```python
"""Agent runtime — durable, resumable runs over deterministic tools."""
from .runtime import execute_run  # noqa: F401
from .store import create_run, events_since, load_run  # noqa: F401
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd backend && .venv/Scripts/python -m pytest tests/test_agent_runtime.py -v`
Expected: PASS — all eight tests.

`test_critic_failure_aborts_the_run` registers a check that always fails, and the critic registry is module-global. Run the file on its own to confirm, then run the whole agent suite (`pytest tests/test_agent_*.py -v`) to confirm no cross-test leakage. If leakage appears, add a fixture that snapshots and restores `critic._CHECKS` around that test.

- [ ] **Step 6: Commit**

```bash
git add backend/app/agent_runtime/runtime.py backend/app/agent_runtime/__init__.py backend/tests/test_agent_runtime.py
git commit -m "feat(agent): plan-execute-verify loop with gates, critic, and budget"
```

---

## Task 10: The `run_reconciliation` macro-tool

Today's pipeline becomes one registered tool. This is what makes the migration zero-regression: the deterministic path is called, not reimplemented. `backend/app/agent.py` is **not modified**.

**Files:**
- Create: `backend/app/agent_runtime/job_persist.py`
- Create: `backend/app/agent_runtime/tools_macro.py`
- Modify: `backend/app/main.py` (call the extracted helper)
- Test: `backend/tests/test_agent_macro_tool.py`

**Interfaces:**
- Consumes: `app.agent.run_job`, `app.models.ReconcileConfig` / `BindingSet`, `artifacts.get_dataset`, `context.current_run`, `registry.register`
- Produces:
  - `job_persist.persist_agent_output(*, job_id: str, account_id: str, output) -> None`
  - `tools_macro.run_reconciliation(dataset_a_id: str, dataset_b_id: str, label_a: str, label_b: str) -> dict`

Return shape: `{job_id, matched, unmatched_a, unmatched_b, discrepancies, triage_emitted, insights_status, rule_applications}` — counts only.

- [ ] **Step 1: Extract the existing job persistence**

Open `backend/app/main.py` and read `_run_job_background` (starts at line 335). It calls `agent.run_job(...)` and then writes the result via `storage.update_job` / `storage.save_job`.

Move **only the result-writing portion** — everything after `run_job` returns — into a new `backend/app/agent_runtime/job_persist.py` as:

```python
def persist_agent_output(*, job_id: str, account_id: str, output) -> None:
    ...
```

Copy the existing body verbatim; do not redesign the payload shape. Then change `_run_job_background` to call `persist_agent_output(...)`. The upload path's behaviour must be byte-identical after this step.

- [ ] **Step 2: Verify the extraction changed nothing**

Run: `cd backend && .venv/Scripts/python -m pytest tests/test_async_jobs.py tests/test_job_progress.py -v`
Expected: PASS with no edits to those tests. If they fail, the extraction changed behaviour — revert and redo.

- [ ] **Step 3: Write the failing macro-tool test**

Create `backend/tests/test_agent_macro_tool.py`:

```python
from __future__ import annotations

import uuid

import pandas as pd
import pytest

from app.agent_runtime import artifacts, context, store, tools_macro
from app.agent_runtime.registry import Effect, effect_of
from app.db.base import session_scope
from app.db.models import AccountORM
from app.memory import accounts as accounts_memory
from app.models import AutonomyLevel


@pytest.fixture()
def run_ctx(monkeypatch):
    monkeypatch.setenv("RECONOPS_STUB_LLM", "1")
    acct = accounts_memory.create_account(display_name="Macro Test")
    run = store.create_run(
        account_id=acct.id, goal={}, autonomy=AutonomyLevel.auto, budget={},
    )
    ctx = context.RunContext(run_id=run.id, account_id=acct.id)
    token = context.set_run_context(ctx)
    yield ctx
    context.reset_run_context(token)


def _orders():
    return pd.DataFrame({
        "order_id": ["A-1", "A-2", "A-3"],
        "order_total": [10.0, 20.0, 30.0],
        "order_date": ["2026-06-01", "2026-06-02", "2026-06-03"],
    })


def _payouts():
    return pd.DataFrame({
        "order_ref": ["A-1", "A-2"],
        "amount_paid": [10.0, 20.0],
        "paid_on": ["2026-06-03", "2026-06-04"],
    })


def test_macro_tool_is_registered_as_a_write(run_ctx):
    assert effect_of("run_reconciliation") is Effect.write


def test_macro_tool_returns_counts_not_rows(run_ctx):
    a = artifacts.put_dataset(
        run_id=run_ctx.run_id, account_id=run_ctx.account_id,
        df=_orders(), label="orders",
    )
    b = artifacts.put_dataset(
        run_id=run_ctx.run_id, account_id=run_ctx.account_id,
        df=_payouts(), label="payouts",
    )

    out = tools_macro.run_reconciliation(a, b, "Orders", "Payouts")

    assert out["matched"] == 2
    assert out["unmatched_a"] == 1
    assert out["unmatched_b"] == 0
    assert isinstance(out["job_id"], str)

    serialized = str(out)
    assert "A-1" not in serialized
    assert "order_total" not in serialized


def test_macro_tool_persists_a_readable_job(run_ctx):
    from app import storage

    a = artifacts.put_dataset(
        run_id=run_ctx.run_id, account_id=run_ctx.account_id,
        df=_orders(), label="orders",
    )
    b = artifacts.put_dataset(
        run_id=run_ctx.run_id, account_id=run_ctx.account_id,
        df=_payouts(), label="payouts",
    )

    out = tools_macro.run_reconciliation(a, b, "Orders", "Payouts")
    job = storage.load_job(out["job_id"])

    assert job is not None
    assert job["account_id"] == run_ctx.account_id
```

- [ ] **Step 4: Run it to verify it fails**

Run: `cd backend && .venv/Scripts/python -m pytest tests/test_agent_macro_tool.py -v`
Expected: FAIL — `ImportError: cannot import name 'tools_macro'`

- [ ] **Step 5: Write the macro-tool**

Create `backend/app/agent_runtime/tools_macro.py`:

```python
"""The classic reconciliation pipeline, exposed as one registered tool.

`app/agent.py` is called, never reimplemented. That is what makes the
migration zero-regression and gives the planner a fast path for the goal it
handles best (spec 1.3).
"""
from __future__ import annotations

import uuid
from typing import Any, Dict

from ..agent import run_job
from ..memory import accounts as accounts_memory
from ..models import BindingSet, ReconcileConfig
from ..tools import binding as binding_tool
from . import artifacts
from .context import current_run
from .job_persist import persist_agent_output
from .registry import Effect, register


@register(Effect.write)
def run_reconciliation(
    dataset_a_id: str,
    dataset_b_id: str,
    label_a: str,
    label_b: str,
) -> Dict[str, Any]:
    """Reconcile two datasets end-to-end: bind, match, compare, classify.

    Call this when the goal is the classic two-source reconciliation — "did
    these orders get paid", "match this export against that one". It runs
    the full deterministic pipeline in one step and returns counts plus a
    job id for the detailed result.

    Args:
        dataset_a_id: Handle for the left dataset (e.g. orders).
        dataset_b_id: Handle for the right dataset (e.g. payments).
        label_a: Human-readable name for the left source.
        label_b: Human-readable name for the right source.
    """
    ctx = current_run()
    account = accounts_memory.load_account(ctx.account_id)
    if account is None:
        raise KeyError(f"account {ctx.account_id} not found")

    df_a = artifacts.get_dataset(dataset_id=dataset_a_id, account_id=ctx.account_id)
    df_b = artifacts.get_dataset(dataset_id=dataset_b_id, account_id=ctx.account_id)

    cfg = ReconcileConfig(
        source_a=BindingSet(
            bindings=binding_tool.bind_columns(df_a, account_id=ctx.account_id),
        ),
        source_b=BindingSet(
            bindings=binding_tool.bind_columns(df_b, account_id=ctx.account_id),
        ),
        label_a=label_a,
        label_b=label_b,
    )

    job_id = str(uuid.uuid4())
    output = run_job(
        account=account, df_a=df_a, df_b=df_b, cfg=cfg, job_id=job_id,
    )
    persist_agent_output(
        job_id=job_id, account_id=ctx.account_id, output=output,
    )

    return {
        "job_id": job_id,
        "matched": len(output.matched),
        "unmatched_a": len(output.unmatched_a),
        "unmatched_b": len(output.unmatched_b),
        "discrepancies": len(output.discrepancies),
        "triage_emitted": len(output.triage_emitted),
        "rule_applications": int(output.rule_applications),
        "insights_status": output.insights_status,
    }
```

- [ ] **Step 6: Import the macro-tool so it registers**

Add to `backend/app/agent_runtime/runtime.py`, in the existing import line from `.`:

```python
from . import artifacts, critic, registry, store, tools_core, tools_macro  # noqa: F401
```

Registration happens at import; without this the tool never reaches the model.

- [ ] **Step 7: Run the tests**

Run: `cd backend && .venv/Scripts/python -m pytest tests/test_agent_macro_tool.py tests/test_agent_registry.py -v`
Expected: PASS. The registry's sorted-order test now covers four tools.

- [ ] **Step 8: Commit**

```bash
git add backend/app/agent_runtime/job_persist.py backend/app/agent_runtime/tools_macro.py backend/app/agent_runtime/runtime.py backend/app/main.py backend/tests/test_agent_macro_tool.py
git commit -m "feat(agent): register the reconciliation pipeline as a macro-tool"
```

---

## Task 11: HTTP surface

`POST /api/agent/runs` starts a run; `GET /api/agent/runs/{id}/events` streams it. Both gated on `RECONOPS_AGENT_RUNTIME=1`.

SSE reconnect uses `Last-Event-ID` against the `run_events` bigserial. The client opens the stream *first*, then fetches history — so the list endpoint has to exist alongside the stream.

**Files:**
- Create: `backend/app/agent_runtime/routes.py`
- Modify: `backend/app/main.py` (mount the router)
- Test: `backend/tests/test_agent_routes.py`

**Interfaces:**
- Consumes: `store` (Task 3), `runtime.execute_run` (Task 9), `app.deps.require_account`
- Produces:
  - `POST /api/agent/runs` — body `{goal: dict, autonomy?: str, budget?: dict}` → `{run_id, status}`
  - `GET /api/agent/runs/{run_id}` → the `Run`
  - `GET /api/agent/runs/{run_id}/events?after={id}` → `{events: [...]}`
  - `GET /api/agent/runs/{run_id}/events/stream` → SSE
  - `router` — the `APIRouter` to mount

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_agent_routes.py`:

```python
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


import importlib


def _login(client, email):
    code = client.post(
        "/api/auth/request-code", json={"email": email},
    ).json()["dev_code"]
    r = client.post("/api/auth/verify", json={"email": email, "code": code})
    assert r.status_code == 200


def _account_headers(client, email):
    """Log in and create an account; returns the workspace-selector header."""
    _login(client, email)
    acct = client.post("/api/accounts", json={})
    assert acct.status_code == 200
    return {"X-Account-Id": acct.json()["id"]}


@pytest.fixture()
def app_module(tmp_path, monkeypatch):
    monkeypatch.setenv("RECONOPS_AGENT_RUNTIME", "1")
    monkeypatch.setenv("RECONOPS_STUB_LLM", "1")
    monkeypatch.setenv("RECONOPS_AUTH_DEV", "1")
    monkeypatch.setenv("RECONOPS_DATA_DIR", str(tmp_path))
    from app import main
    importlib.reload(main)
    return main


@pytest.fixture()
def client(app_module):
    with TestClient(app_module.app) as c:
        yield c


@pytest.fixture()
def owner_headers(client):
    return _account_headers(client, "owner@x.co")


@pytest.fixture()
def stranger(app_module):
    """A second session with its own user and its own account."""
    with TestClient(app_module.app) as c:
        yield c, _account_headers(c, "stranger@x.co")


def test_routes_are_404_when_the_flag_is_off(tmp_path, monkeypatch):
    monkeypatch.delenv("RECONOPS_AGENT_RUNTIME", raising=False)
    monkeypatch.setenv("RECONOPS_AUTH_DEV", "1")
    monkeypatch.setenv("RECONOPS_DATA_DIR", str(tmp_path))
    from app import main
    importlib.reload(main)
    with TestClient(main.app) as c:
        headers = _account_headers(c, "flagoff@x.co")
        r = c.post(
            "/api/agent/runs", json={"goal": {"intent": "x"}}, headers=headers,
        )
    assert r.status_code == 404


def test_creating_a_run_requires_an_account_header(client):
    r = client.post("/api/agent/runs", json={"goal": {"intent": "x"}})
    assert r.status_code in (400, 401, 403)


def test_events_endpoint_supports_after_cursor(client, owner_headers):
    created = client.post(
        "/api/agent/runs",
        json={"goal": {"intent": "test"}, "autonomy": "observe"},
        headers=owner_headers,
    )
    assert created.status_code == 200
    run_id = created.json()["run_id"]

    # TestClient runs BackgroundTasks before returning, so the run has
    # already executed (and suspended, in observe mode) by this point.
    first = client.get(f"/api/agent/runs/{run_id}/events", headers=owner_headers)
    assert first.status_code == 200
    events = first.json()["events"]
    assert events, "the run should have logged at least goal_received"

    after = events[0]["id"]
    second = client.get(
        f"/api/agent/runs/{run_id}/events?after={after}", headers=owner_headers,
    )
    assert all(e["id"] > after for e in second.json()["events"])


def test_run_is_not_readable_from_another_account(client, owner_headers, stranger):
    created = client.post(
        "/api/agent/runs", json={"goal": {"intent": "test"}}, headers=owner_headers,
    )
    run_id = created.json()["run_id"]

    other_client, other_headers = stranger
    r = other_client.get(f"/api/agent/runs/{run_id}", headers=other_headers)
    assert r.status_code == 404
```

The fixtures follow `tests/test_auth_endpoints.py`: dev auth mode, `importlib.reload(main)` so the flag env vars are picked up at import, and the six-digit `dev_code` login. Two `TestClient` instances are needed for the cross-account test because each holds its own cookie jar — one user cannot be a stranger to their own account.

- [ ] **Step 2: Run it to verify it fails**

Run: `cd backend && .venv/Scripts/python -m pytest tests/test_agent_routes.py -v`
Expected: FAIL — 404 on `/api/agent/runs` (router not mounted).

- [ ] **Step 3: Write the routes**

Create `backend/app/agent_runtime/routes.py`:

```python
"""HTTP surface for agent runs.

The event stream is the audit band, the SSE tail, and the replay input at
once (spec 1.1), so both a paginated list and a live stream read the same
table. Clients open the stream first, then fetch history, then dedupe by
event id — open-then-fetch, because the stream only carries events emitted
after it opens.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, Optional

from fastapi import (
    APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..deps import require_account
from ..models import Account, AutonomyLevel, RunStatus
from . import runtime, store

router = APIRouter(prefix="/api/agent", tags=["agent"])

POLL_INTERVAL_S = 0.5


def _require_flag() -> None:
    if os.getenv("RECONOPS_AGENT_RUNTIME") != "1":
        raise HTTPException(status_code=404, detail="Not found.")


class CreateRunRequest(BaseModel):
    goal: Dict[str, Any]
    autonomy: AutonomyLevel = AutonomyLevel.assist
    budget: Dict[str, Any] = {}


@router.post("/runs")
def create_run(
    body: CreateRunRequest,
    background_tasks: BackgroundTasks,
    account: Account = Depends(require_account),
):
    """Create a run and start it.

    Execution rides FastAPI's BackgroundTasks in Phase A, mirroring the
    existing upload path in main.py. That is not durable across a restart —
    the durable worker is Phase A follow-on work. The run row and its event
    log are already durable, which is what makes that upgrade a swap rather
    than a rewrite.

    `goal_received` is emitted by the runtime, not here, so the event log has
    exactly one writer.
    """
    _require_flag()
    run = store.create_run(
        account_id=account.id,
        goal=body.goal,
        autonomy=body.autonomy,
        budget=body.budget,
    )
    background_tasks.add_task(
        runtime.execute_run, run_id=run.id, account_id=account.id,
    )
    return {"run_id": run.id, "status": run.status.value}


@router.get("/runs/{run_id}")
def get_run(run_id: str, account: Account = Depends(require_account)):
    _require_flag()
    run = store.load_run(run_id, account.id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    return run.model_dump(mode="json")


@router.get("/runs/{run_id}/events")
def list_events(
    run_id: str, after: int = 0, account: Account = Depends(require_account),
):
    _require_flag()
    if store.load_run(run_id, account.id) is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    events = store.events_since(
        run_id=run_id, account_id=account.id, after_id=after,
    )
    return {"events": [e.model_dump(mode="json") for e in events]}


@router.get("/runs/{run_id}/events/stream")
async def stream_events(
    run_id: str,
    request: Request,
    account: Account = Depends(require_account),
    last_event_id: Optional[str] = Header(default=None, alias="Last-Event-ID"),
):
    _require_flag()
    if store.load_run(run_id, account.id) is None:
        raise HTTPException(status_code=404, detail="Run not found.")

    cursor = int(last_event_id) if last_event_id else 0

    async def generate():
        nonlocal cursor
        while True:
            if await request.is_disconnected():
                break

            events = store.events_since(
                run_id=run_id, account_id=account.id, after_id=cursor,
            )
            for event in events:
                cursor = event.id
                payload = json.dumps(event.model_dump(mode="json"), default=str)
                yield f"id: {event.id}\nevent: {event.type.value}\ndata: {payload}\n\n"

            run = store.load_run(run_id, account.id)
            if run and run.status in (
                RunStatus.done, RunStatus.failed,
                RunStatus.aborted, RunStatus.suspended,
            ):
                break

            await asyncio.sleep(POLL_INTERVAL_S)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
```

Note the `runtime` import at the top of the file is what lets `create_run` schedule execution — `from . import runtime, store`.

- [ ] **Step 4: Mount the router**

In `backend/app/main.py`, next to the existing router mounts (search for `include_router`), add:

```python
from .agent_runtime.routes import router as agent_router
app.include_router(agent_router)
```

- [ ] **Step 5: Run the tests**

Run: `cd backend && .venv/Scripts/python -m pytest tests/test_agent_routes.py -v`
Expected: PASS — all four tests.

- [ ] **Step 6: Commit**

```bash
git add backend/app/agent_runtime/routes.py backend/app/main.py backend/tests/test_agent_routes.py
git commit -m "feat(agent): run creation and SSE event stream endpoints"
```

---

## Task 12: Eval gate — planner path must match the macro-tool

Phase A's acceptance criterion: a classic-recon goal driven through the runtime produces the *same* numbers as calling the pipeline directly. If it does not, the macro-tool wrapper has introduced a regression and the whole zero-regression argument fails.

**Files:**
- Create: `backend/tests/test_agent_eval_parity.py`
- Modify: `backend/app/eval.py`

**Interfaces:**
- Consumes: `runtime.execute_run` (Task 9), `tools_macro.run_reconciliation` (Task 10), `app.agent.run_job`
- Produces: `app.eval.parity_report(account_id: str) -> dict` — `{direct, via_runtime, matches: bool}`

- [ ] **Step 1: Write the failing parity test**

Create `backend/tests/test_agent_eval_parity.py`:

```python
"""Phase A gate: the runtime path must not change the numbers.

If these diverge, the macro-tool wrapper regressed the pipeline and the
migration is no longer zero-regression (spec 1.3).
"""
from __future__ import annotations

import uuid

import pandas as pd
import pytest

from app.agent import run_job
from app.agent_runtime import artifacts, context, runtime, store, tools_macro
from app.agent_runtime.runtime import ToolCall, Turn
from app.memory import accounts as accounts_memory
from app.models import AutonomyLevel, BindingSet, ReconcileConfig
from app.tools import binding as binding_tool


class ScriptedDriver:
    def __init__(self, turns):
        self._turns = list(turns)

    def next_turn(self, *, system, messages, tools, task_budget_tokens):
        if not self._turns:
            return Turn(text="done", tool_calls=[])
        return self._turns.pop(0)


def _orders():
    return pd.DataFrame({
        "order_id": ["A-1", "A-2", "A-3", "A-4"],
        "order_total": [10.0, 20.0, 30.0, 40.0],
        "order_date": ["2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04"],
    })


def _payouts():
    return pd.DataFrame({
        "order_ref": ["A-1", "A-2", "A-3"],
        "amount_paid": [10.0, 20.0, 29.5],
        "paid_on": ["2026-06-03", "2026-06-04", "2026-06-06"],
    })


@pytest.fixture()
def account(monkeypatch):
    monkeypatch.setenv("RECONOPS_STUB_LLM", "1")
    return accounts_memory.create_account(display_name="Parity Test")


def _direct(account):
    df_a, df_b = _orders(), _payouts()
    cfg = ReconcileConfig(
        source_a=BindingSet(bindings=binding_tool.bind_columns(df_a, account_id=account.id)),
        source_b=BindingSet(bindings=binding_tool.bind_columns(df_b, account_id=account.id)),
        label_a="Orders", label_b="Payouts",
    )
    out = run_job(
        account=account, df_a=df_a, df_b=df_b, cfg=cfg, job_id=str(uuid.uuid4()),
    )
    return {
        "matched": len(out.matched),
        "unmatched_a": len(out.unmatched_a),
        "unmatched_b": len(out.unmatched_b),
        "discrepancies": len(out.discrepancies),
    }


def test_runtime_path_matches_direct_pipeline(account):
    expected = _direct(account)

    run = store.create_run(
        account_id=account.id, goal={"intent": "reconcile"},
        autonomy=AutonomyLevel.auto, budget={"tool_call_cap": 5},
    )
    token = context.set_run_context(
        context.RunContext(run_id=run.id, account_id=account.id),
    )
    try:
        a = artifacts.put_dataset(
            run_id=run.id, account_id=account.id, df=_orders(), label="orders",
        )
        b = artifacts.put_dataset(
            run_id=run.id, account_id=account.id, df=_payouts(), label="payouts",
        )
    finally:
        context.reset_run_context(token)

    driver = ScriptedDriver([
        Turn(text=None, tool_calls=[ToolCall(
            id="t1", name="run_reconciliation",
            input={
                "dataset_a_id": a, "dataset_b_id": b,
                "label_a": "Orders", "label_b": "Payouts",
            },
        )]),
        Turn(text="reconciled", tool_calls=[]),
    ])

    runtime.execute_run(run_id=run.id, account_id=account.id, driver=driver)

    events = store.events_since(run_id=run.id, account_id=account.id)
    returned = next(
        e for e in events if e.payload.get("tool") == "run_reconciliation"
        and "output" in e.payload
    )
    actual = returned.payload["output"]

    assert actual["matched"] == expected["matched"]
    assert actual["unmatched_a"] == expected["unmatched_a"]
    assert actual["unmatched_b"] == expected["unmatched_b"]
    assert actual["discrepancies"] == expected["discrepancies"]


def test_no_verdict_originates_from_the_model(account):
    """House law: the LLM never computes money."""
    run = store.create_run(
        account_id=account.id, goal={}, autonomy=AutonomyLevel.auto, budget={},
    )
    driver = ScriptedDriver([
        Turn(text="I calculate the total is $999,999.", tool_calls=[]),
    ])

    runtime.execute_run(run_id=run.id, account_id=account.id, driver=driver)

    events = store.events_since(run_id=run.id, account_id=account.id)
    tool_outputs = [
        e.payload["output"] for e in events if "output" in e.payload
    ]
    assert tool_outputs == [], "no tool ran, so no figure is authoritative"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd backend && .venv/Scripts/python -m pytest tests/test_agent_eval_parity.py -v`
Expected: FAIL — until Tasks 9 and 10 are complete this cannot pass. If they are complete and it still fails, the macro-tool changed the numbers; fix `tools_macro.py`, not the test.

- [ ] **Step 3: Expose parity as a callable report**

Add to the end of `backend/app/eval.py`:

```python
def parity_report(account_id: str, df_a, df_b) -> dict:
    """Compare the runtime path against the direct pipeline.

    Phase A's release gate. Returns both result sets plus whether they agree,
    so CI can fail the build on divergence rather than on a log line.
    """
    import uuid

    from .agent import run_job
    from .agent_runtime import artifacts, context, runtime, store
    from .agent_runtime.runtime import ToolCall, Turn
    from .memory import accounts as accounts_memory
    from .models import AutonomyLevel, BindingSet, ReconcileConfig
    from .tools import binding as binding_tool

    account = accounts_memory.load_account(account_id)
    if account is None:
        raise KeyError(f"account {account_id} not found")

    cfg = ReconcileConfig(
        source_a=BindingSet(
            bindings=binding_tool.bind_columns(df_a, account_id=account_id),
        ),
        source_b=BindingSet(
            bindings=binding_tool.bind_columns(df_b, account_id=account_id),
        ),
        label_a="A", label_b="B",
    )
    out = run_job(
        account=account, df_a=df_a, df_b=df_b, cfg=cfg, job_id=str(uuid.uuid4()),
    )
    direct = {
        "matched": len(out.matched),
        "unmatched_a": len(out.unmatched_a),
        "unmatched_b": len(out.unmatched_b),
        "discrepancies": len(out.discrepancies),
    }

    run = store.create_run(
        account_id=account_id, goal={"intent": "reconcile"},
        autonomy=AutonomyLevel.auto, budget={"tool_call_cap": 5},
    )
    token = context.set_run_context(
        context.RunContext(run_id=run.id, account_id=account_id),
    )
    try:
        a = artifacts.put_dataset(
            run_id=run.id, account_id=account_id, df=df_a, label="A",
        )
        b = artifacts.put_dataset(
            run_id=run.id, account_id=account_id, df=df_b, label="B",
        )
    finally:
        context.reset_run_context(token)

    class _Scripted:
        def __init__(self):
            self._turns = [
                Turn(text=None, tool_calls=[ToolCall(
                    id="t1", name="run_reconciliation",
                    input={
                        "dataset_a_id": a, "dataset_b_id": b,
                        "label_a": "A", "label_b": "B",
                    },
                )]),
                Turn(text="reconciled", tool_calls=[]),
            ]

        def next_turn(self, *, system, messages, tools, task_budget_tokens):
            return self._turns.pop(0) if self._turns else Turn(text="", tool_calls=[])

    runtime.execute_run(
        run_id=run.id, account_id=account_id, driver=_Scripted(),
    )

    events = store.events_since(run_id=run.id, account_id=account_id)
    returned = next(
        e for e in events
        if e.payload.get("tool") == "run_reconciliation" and "output" in e.payload
    )
    output = returned.payload["output"]
    via_runtime = {k: output[k] for k in direct}

    return {
        "direct": direct,
        "via_runtime": via_runtime,
        "matches": direct == via_runtime,
    }
```

- [ ] **Step 3b: Have the test call the shared report**

Replace the body of `test_runtime_path_matches_direct_pipeline` so the comparison lives in one place:

```python
def test_runtime_path_matches_direct_pipeline(account):
    from app.eval import parity_report

    report = parity_report(account.id, _orders(), _payouts())
    assert report["matches"], report
```

Delete the now-unused `_direct` helper and the `ScriptedDriver` class from the test file if nothing else references them.

- [ ] **Step 4: Run the full suite**

Run: `cd backend && .venv/Scripts/python -m pytest -q`
Expected: all pass, including every pre-existing test. Nothing in `app/agent.py`, `app/tools/`, or `app/memory/` changed in this phase, so any failure there is a real regression.

- [ ] **Step 5: Commit**

```bash
git add backend/app/eval.py backend/tests/test_agent_eval_parity.py
git commit -m "test(agent): parity gate between runtime and direct pipeline"
```

---

## Definition of done

Phase A is complete when, with `RECONOPS_AGENT_RUNTIME=1`:

1. `POST /api/agent/runs` with a goal creates a run and returns its id.
2. `GET /api/agent/runs/{id}/events/stream` streams events, and reconnecting with `Last-Event-ID` resumes without gaps or duplicates.
3. A run in `observe` suspends before its first tool call and records `suspended_on`.
4. A run in `auto` executes `run_reconciliation` and produces the same counts as calling `run_job` directly.
5. A failed critic check aborts the run and is visible in the event log.
6. Exceeding any hard cap aborts the run and emits `budget_exceeded`.
7. `runs.transcript` round-trips, so a suspended run can be rehydrated in a new process.
8. The existing upload → results → inbox flow is unchanged, and the full pre-existing suite passes.

## Known gaps deferred past Phase A

Stated so nobody mistakes them for oversights:

- **Resume is not wired.** Task 9 suspends and persists; the endpoint that answers a question and continues the run is Phase A follow-on work, and needs the mid-conversation system message mechanism (spec §4.6).
- **No planner yet.** The loop executes whatever the model calls. `plan_proposed` / `step_started` / `step_completed` are in the event enum but unemitted until the planner lands.
- **Tier-2 tool search is not wired.** Only tier-1 tools are registered; `defer_loading` and pack tools arrive with Phase B.
- **`render` is not implemented.** The block protocol (spec §3) is Phase D.
- **Ontology stays a module global.** `OntologyView` is Phase B; `bind_columns` still reads the import-time singleton.
- **USD spend is never recorded.** `Spend.record_llm` exists but nothing calls it until the driver reports usage; the tool-call and wall-clock caps carry the load in Phase A.
- **`jobs.run_id` is not added.** Spec §1.3 has `jobs` gaining a `run_id` FK and deprecating its `status` column. Nothing in Phase A reads that link — the macro-tool returns `job_id` directly — so the migration is deferred rather than shipped unused. Add it when the conversational surface needs to walk from a run to its job artifact.
- **Execution is not durable.** `POST /api/agent/runs` schedules via `BackgroundTasks`, so a restart mid-run leaves a row stuck in `running`. The run and its events are already durable; replacing the scheduler is a swap, not a rewrite. A reaper for stale `running` rows should land with it — `storage.reap_stale_jobs` is the existing precedent.

