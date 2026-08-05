# Agent Run Resume Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user answer a suspended run's gate — approve or reject the pending tool call — and have the run continue in a fresh process.

**Architecture:** A new `runtime.resume_run` beside `execute_run`. The loop body extracts into a shared `_drive`, and per-turn tool execution into a shared `_execute_calls`, so gate/critic/budget logic is written once. Resume rehydrates `messages` from `runs.transcript` and `spend` from `runs.spend`, turns the user's decision into `tool_result` blocks answering every pending `tool_use`, optionally appends a mid-conversation `{"role": "system"}` note, then re-enters the loop.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy 2.0, Postgres, `anthropic` SDK, pytest.

**Spec:** [`docs/specs/2026-08-03-agent-resume-design.md`](../specs/2026-08-03-agent-resume-design.md) — read "The API constraint that shapes everything" before starting.

## Global Constraints

Every task's requirements implicitly include this section.

- **Tests run from `backend/`** with `RECONOPS_DATABASE_URL` pointed at the dev Postgres. On this machine that is port **5433**, not conftest's 5432 default:
  ```bash
  export RECONOPS_DATABASE_URL="postgresql://reconops:reconops@localhost:5433/reconops_test"
  cd backend && .venv/Scripts/python -m pytest -q
  ```
  Docker must be running (`docker ps` shows `dras-postgres-1`).
- **Model ID is exactly `claude-opus-4-8`.** Mid-conversation `{"role": "system"}` messages are supported on that model only; an unsupported model returns 400 `role 'system' is not supported on this model`.
- **A mid-conversation system message must follow a `user` turn and must be last** (or followed by an assistant turn). It can never be `messages[0]`.
- **Every `tool_use` block must be answered by a `tool_result` with the same id**, and all results for one turn go in a **single** user message. Splitting them trains the model out of parallel tool calls.
- **The LLM never computes money.** Every number in a result originates from a deterministic tool.
- **No migration.** `rejected_calls` and `suspended_at` live in `runs.payload` (JSONB); every event type used already exists in `RunEventType`.
- **`backend/app/agent.py` is not modified.**
- **Feature flag:** the new route is gated on `RECONOPS_AGENT_RUNTIME=1`.
- **LLM stubbing in tests uses `ScriptedDriver`**, not network. Never call a live model in a test.

---

## File Structure

**Modified**

| File | Change |
|---|---|
| `backend/app/agent_runtime/budget.py` | `Spend.accumulated_s`, `Spend.from_dict`, `elapsed_s` accumulates |
| `backend/app/agent_runtime/registry.py` | `callable_for(tool_name)` |
| `backend/app/agent_runtime/runtime.py` | `_dispatch` via registry; extract `_drive` + `_execute_calls`; `resume_run`; repeat guard; set `suspended_at` |
| `backend/app/agent_runtime/store.py` | `claim_suspended`, `record_rejected_calls` |
| `backend/app/agent_runtime/routes.py` | `POST /api/agent/runs/{run_id}/answer` |
| `backend/app/models.py` | `Run.rejected_calls`, `Run.suspended_at` |

**New tests:** `backend/tests/test_agent_resume.py`, plus additions to `test_agent_budget.py`, `test_agent_store.py`, `test_agent_routes.py`.

---

## Task 1: Spend survives a process boundary

Fixes a live bug. `Spend.started_at` is `time.monotonic()` — process-local with an arbitrary epoch — and `to_dict()` serializes only the derived `elapsed_s`. There is no `from_dict`, so a resumed run builds a fresh `Spend()` with `tool_calls` and `usd` back at zero. The hard caps would become **per-segment instead of per-run**: suspend and resume in a loop and no cap ever trips.

**Files:**
- Modify: `backend/app/agent_runtime/budget.py`
- Test: `backend/tests/test_agent_budget.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `Spend.accumulated_s: float` (field, default `0.0`)
  - `Spend.from_dict(d: Dict[str, Any]) -> Spend`
  - `Spend.elapsed_s() -> float` now returns `accumulated_s + (monotonic() - started_at)`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_agent_budget.py`:

```python
def test_spend_round_trips_counts_across_a_process_boundary():
    """A resumed run must not reset its caps.

    `started_at` is process-local monotonic, so a resumed process cannot
    reuse it. What must survive is the accumulated cost: tool calls, usd,
    and elapsed compute time.
    """
    spend = Spend(tool_calls=7, usd=0.12)
    restored = Spend.from_dict(spend.to_dict())

    assert restored.tool_calls == 7
    assert restored.usd == 0.12


def test_from_dict_carries_prior_elapsed_forward():
    spend = Spend(tool_calls=1, accumulated_s=45.0)
    restored = Spend.from_dict(spend.to_dict())

    # Prior compute time is preserved; the new local clock starts at ~0.
    assert restored.elapsed_s() >= 45.0
    assert restored.elapsed_s() < 46.0


def test_suspended_time_does_not_count_against_the_wall_clock():
    """Spec decision 3: the cap measures agent compute, not human latency.

    A run suspended for an hour and then approved must resume with its
    wall-clock budget intact — otherwise every gated run trips
    `budget_exceeded` the instant it is approved.
    """
    budget = Budget(wall_clock_s=120)
    spend = Spend(tool_calls=1, accumulated_s=30.0)
    persisted = spend.to_dict()

    # ... an hour of human deliberation passes; a new process resumes ...
    restored = Spend.from_dict(persisted)

    assert exceeded(budget, restored) is None


def test_from_dict_defaults_are_safe_on_a_missing_key():
    restored = Spend.from_dict({})
    assert restored.tool_calls == 0
    assert restored.usd == 0.0
    assert restored.elapsed_s() < 1.0
```

Ensure the file's import line covers what these use:

```python
from app.agent_runtime.budget import Budget, Spend, exceeded
```

- [ ] **Step 2: Run them to verify they fail**

```bash
cd backend && .venv/Scripts/python -m pytest tests/test_agent_budget.py -q
```
Expected: FAIL — `AttributeError: type object 'Spend' has no attribute 'from_dict'`.

- [ ] **Step 3: Implement**

In `backend/app/agent_runtime/budget.py`, replace the `Spend` dataclass with:

```python
@dataclass
class Spend:
    tool_calls: int = 0
    usd: float = 0.0
    started_at: float = field(default_factory=time.monotonic)
    accumulated_s: float = 0.0

    def record_tool_call(self) -> None:
        self.tool_calls += 1

    def record_llm(self, usd: float) -> None:
        self.usd += usd

    def elapsed_s(self) -> float:
        """Compute time across every segment of this run.

        `started_at` measures only the current process. A run that suspended
        on a gate and resumed elsewhere carries its prior segments in
        `accumulated_s`, so the wall-clock cap keeps measuring agent work
        rather than restarting at zero each time (spec decision 3).
        """
        return self.accumulated_s + (time.monotonic() - self.started_at)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tool_calls": self.tool_calls,
            "usd": round(self.usd, 6),
            "elapsed_s": round(self.elapsed_s(), 3),
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Spend":
        """Rehydrate spend for a resumed run.

        `started_at` is deliberately NOT restored: it is process-local
        monotonic, so a value from the suspending process is meaningless
        here. The elapsed total moves into `accumulated_s` and the local
        clock restarts, which is what excludes suspended time from the cap.
        """
        return Spend(
            tool_calls=int(d.get("tool_calls", 0)),
            usd=float(d.get("usd", 0.0)),
            accumulated_s=float(d.get("elapsed_s", 0.0)),
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd backend && .venv/Scripts/python -m pytest tests/test_agent_budget.py -q
```
Expected: PASS.

- [ ] **Step 5: Run the full suite for fallout**

```bash
cd backend && .venv/Scripts/python -m pytest -q
```
Expected: PASS. `_would_exceed` in `runtime.py` constructs a probe `Spend(...)` without `accumulated_s`; that probe now under-reports elapsed time on a resumed run. Fix it in the same commit by copying the field:

```python
    probe = Spend(
        tool_calls=spend.tool_calls + 1,
        usd=spend.usd,
        started_at=spend.started_at,
        accumulated_s=spend.accumulated_s,
    )
```

- [ ] **Step 6: Commit**

```bash
git add backend/app/agent_runtime/budget.py backend/tests/test_agent_budget.py
git commit -m "fix(agent): make spend survive a process boundary"
```

---

## Task 2: Dispatch resolves through the registry

`_dispatch` resolves tools with `getattr(tools_core, name)`. `run_reconciliation` lives in `tools_macro`, so it is **not** reachable — approving a gated `run_reconciliation` would raise `KeyError: unknown tool run_reconciliation`. This is latent today only because the gate always fires and the call never reaches dispatch; resume is precisely what unblocks that path.

This also closes the hazard the existing comment flags as a follow-up: attribute lookup on a module makes any module-level callable reachable by name.

**Files:**
- Modify: `backend/app/agent_runtime/registry.py`
- Modify: `backend/app/agent_runtime/runtime.py` (`_dispatch`)
- Test: `backend/tests/test_agent_registry.py`

**Interfaces:**
- Consumes: nothing
- Produces: `registry.callable_for(tool_name: str) -> Optional[Callable]`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_agent_registry.py`:

```python
def test_macro_tool_is_dispatchable():
    """The gate is the only reason this has not broken yet.

    `run_reconciliation` lives in tools_macro, not tools_core, so the old
    attribute-lookup dispatch could not find it. It always gated, so the
    call never reached dispatch — until resume executes an approved one.
    """
    from app.agent_runtime import runtime, tools_macro  # noqa: F401

    assert registry.callable_for("run_reconciliation") is not None


def test_callable_for_returns_none_for_an_unregistered_name():
    assert registry.callable_for("not_a_real_tool") is None
```

- [ ] **Step 2: Run it to verify it fails**

```bash
cd backend && .venv/Scripts/python -m pytest tests/test_agent_registry.py -q
```
Expected: FAIL — `AttributeError: module 'app.agent_runtime.registry' has no attribute 'callable_for'`.

- [ ] **Step 3: Add the accessor**

Append to `backend/app/agent_runtime/registry.py`, after `effect_of`:

```python
def callable_for(tool_name: str) -> Optional[Any]:
    """The registered callable for a tool name, or None.

    Dispatch goes through the registry rather than a module attribute
    lookup, so a name is executable exactly when it is registered — and
    registration is also what assigns its effect. The two cannot drift.
    """
    return _TOOLS.get(tool_name)
```

Add `Optional` to the typing import at the top of the file:

```python
from typing import Any, Callable, Dict, List, Optional
```

- [ ] **Step 4: Point `_dispatch` at it**

In `backend/app/agent_runtime/runtime.py`, replace `_dispatch` entirely:

```python
def _dispatch(name: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Execute a registered tool by name.

    Resolution is the registry's table, not an attribute lookup on a
    module: a name is executable exactly when it is registered, and
    registration is also what assigns the effect the gate reads. That
    keeps "gateable" and "runnable" from drifting apart, and it is what
    makes tools outside `tools_core` — `run_reconciliation` — dispatchable.
    """
    fn = registry.callable_for(name)
    if fn is None:
        raise KeyError(f"unknown tool {name}")
    return fn(**payload)
```

- [ ] **Step 5: Run the tests**

```bash
cd backend && .venv/Scripts/python -m pytest tests/test_agent_registry.py tests/test_agent_runtime.py -q
```
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/agent_runtime/registry.py backend/app/agent_runtime/runtime.py backend/tests/test_agent_registry.py
git commit -m "fix(agent): dispatch tools through the registry, not module attrs"
```

---

## Task 3: Run fields and the atomic claim

`claim_suspended` is the double-POST guard: two concurrent answers would otherwise both schedule a resume and execute the pending tool twice.

**Files:**
- Modify: `backend/app/models.py`
- Modify: `backend/app/agent_runtime/store.py`
- Modify: `backend/app/agent_runtime/runtime.py` (suspend block sets `suspended_at`)
- Test: `backend/tests/test_agent_store.py`

**Interfaces:**
- Consumes: `app.models.Run`, `RunStatus`
- Produces:
  - `Run.rejected_calls: List[Dict[str, Any]]` (default `[]`)
  - `Run.suspended_at: Optional[str]`
  - `store.claim_suspended(run_id: str, account_id: str) -> Optional[Run]`
  - `store.record_rejected_calls(*, run_id: str, account_id: str, calls: List[Dict[str, Any]]) -> None`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_agent_store.py`:

```python
def test_claim_suspended_flips_status_and_clears_the_question(account_id):
    run = store.create_run(
        account_id=account_id, goal={}, autonomy=AutonomyLevel.assist, budget={},
    )
    q = store.append_event(
        run=run, type=RunEventType.question_asked, payload={"tool": "x"},
    )
    store.set_status(
        run_id=run.id, account_id=account_id,
        status=RunStatus.suspended, suspended_on=q.id,
    )

    claimed = store.claim_suspended(run.id, account_id)
    assert claimed is not None
    assert claimed.status is RunStatus.running
    assert claimed.suspended_on is None


def test_claim_suspended_is_single_use(account_id):
    """The double-POST guard: two answers must not both execute the tool."""
    run = store.create_run(
        account_id=account_id, goal={}, autonomy=AutonomyLevel.assist, budget={},
    )
    store.set_status(
        run_id=run.id, account_id=account_id, status=RunStatus.suspended,
    )

    assert store.claim_suspended(run.id, account_id) is not None
    assert store.claim_suspended(run.id, account_id) is None


def test_claim_suspended_refuses_a_run_that_never_suspended(account_id):
    run = store.create_run(
        account_id=account_id, goal={}, autonomy=AutonomyLevel.assist, budget={},
    )
    assert store.claim_suspended(run.id, account_id) is None


def test_claim_suspended_is_account_scoped(account_id):
    run = store.create_run(
        account_id=account_id, goal={}, autonomy=AutonomyLevel.assist, budget={},
    )
    store.set_status(
        run_id=run.id, account_id=account_id, status=RunStatus.suspended,
    )
    other = str(uuid.uuid4())
    with session_scope() as s:
        s.add(AccountORM(id=other, payload={}))

    assert store.claim_suspended(run.id, other) is None
    # ...and the run is untouched, so the owner can still claim it.
    assert store.claim_suspended(run.id, account_id) is not None


def test_rejected_calls_round_trip(account_id):
    run = store.create_run(
        account_id=account_id, goal={}, autonomy=AutonomyLevel.assist, budget={},
    )
    store.record_rejected_calls(
        run_id=run.id, account_id=account_id,
        calls=[{"tool": "run_reconciliation", "input": {"a": 1}}],
    )

    loaded = store.load_run(run.id, account_id)
    assert loaded.rejected_calls == [
        {"tool": "run_reconciliation", "input": {"a": 1}},
    ]
```

- [ ] **Step 2: Run them to verify they fail**

```bash
cd backend && .venv/Scripts/python -m pytest tests/test_agent_store.py -q
```
Expected: FAIL — `AttributeError: module 'app.agent_runtime.store' has no attribute 'claim_suspended'`.

- [ ] **Step 3: Add the model fields**

In `backend/app/models.py`, add two fields to the `Run` model, after `suspended_on`:

```python
    suspended_on: Optional[int] = None
    suspended_at: Optional[str] = None
    rejected_calls: List[Dict[str, Any]] = Field(default_factory=list)
```

No migration: these live inside `runs.payload`, which is JSONB.

- [ ] **Step 4: Add the store functions**

Append to `backend/app/agent_runtime/store.py`:

```python
def claim_suspended(run_id: str, account_id: str) -> Optional[Run]:
    """Atomically take ownership of a suspended run, or return None.

    Answering a gate is a two-step operation — flip the status, then
    execute the pending tool — and two concurrent answers would otherwise
    both pass the status check and run the tool twice. The row lock
    serializes them, and the status test inside it means only the first
    caller sees `suspended`; everyone after gets None.

    Returns None rather than raising for a missing or cross-account run,
    so the caller cannot use it as an existence oracle.
    """
    with session_scope() as s:
        row = s.scalar(
            select(RunORM).where(
                RunORM.id == run_id, RunORM.account_id == account_id,
            ).with_for_update()
        )
        if row is None:
            return None
        run = Run.model_validate(row.payload)
        if run.status is not RunStatus.suspended:
            return None
        run.status = RunStatus.running
        run.suspended_on = None
        row.payload = run.model_dump(mode="json")
        row.status = run.status.value
        return run


def record_rejected_calls(
    *, run_id: str, account_id: str, calls: List[Dict[str, Any]],
) -> None:
    """Persist the calls a user has refused, for the repeat guard."""

    def apply(run: Run) -> None:
        run.rejected_calls = calls

    _mutate(run_id, account_id, apply)
```

- [ ] **Step 5: Stamp `suspended_at` when a run suspends**

In `backend/app/agent_runtime/runtime.py`, inside `execute_run`'s gate block ("Pass 1"), the suspend currently calls `store.set_status(...)`. Add the timestamp immediately before it:

```python
                    store.set_suspended_at(
                        run_id=run.id, account_id=account_id,
                    )
                    store.set_status(
                        run_id=run.id, account_id=account_id,
                        status=RunStatus.suspended, suspended_on=q.id,
                    )
```

And add the helper to `store.py`:

```python
def set_suspended_at(*, run_id: str, account_id: str) -> None:
    """Stamp when a run began waiting on a human.

    Nothing reads this yet. It exists so a reaper for abandoned suspended
    runs has a field to sort on — that reaper is deliberately out of scope
    here, but the timestamp is only recordable at the moment of suspension.
    """

    def apply(run: Run) -> None:
        run.suspended_at = _now()

    _mutate(run_id, account_id, apply)
```

- [ ] **Step 6: Run the tests**

```bash
cd backend && .venv/Scripts/python -m pytest tests/test_agent_store.py tests/test_agent_runtime.py -q
```
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add backend/app/models.py backend/app/agent_runtime/store.py backend/app/agent_runtime/runtime.py backend/tests/test_agent_store.py
git commit -m "feat(agent): atomic claim for suspended runs"
```

---

## Task 4: Extract the shared loop

Pure refactor, no behavior change. `execute_run` and `resume_run` must share gate, critic, and budget handling; writing it twice is how the two paths drift.

The gate for this task is the **existing suite passing unchanged** — if any runtime test needs editing, the refactor changed behavior and is wrong.

**Files:**
- Modify: `backend/app/agent_runtime/runtime.py`

**Interfaces:**
- Consumes: `Budget`, `Spend`, `ToolCall`, `Turn`, `store`, `critic`, `registry`
- Produces:
  - `_Executed` dataclass — `results: List[Dict[str, Any]]`, `abort: Optional[Tuple[RunStatus, str]]`
  - `_execute_calls(*, run: Run, account_id: str, calls: List[ToolCall], budget: Budget, spend: Spend) -> _Executed`
  - `_drive(*, run: Run, account_id: str, messages: List[Dict[str, Any]], budget: Budget, spend: Spend, driver: Driver) -> Run`

- [ ] **Step 1: Add the `_Executed` carrier**

In `backend/app/agent_runtime/runtime.py`, after the `Turn` dataclass:

```python
@dataclass
class _Executed:
    """Outcome of running one turn's tool calls.

    `abort` is set when a cap or a critic stopped the turn part-way. The
    caller persists and terminates; carrying it in the return value rather
    than raising keeps the two entry points handling it identically.
    """
    results: List[Dict[str, Any]] = field(default_factory=list)
    abort: Optional[Tuple[RunStatus, str]] = None
```

Add `Tuple` to the typing import:

```python
from typing import Any, Dict, List, Optional, Protocol, Tuple
```

- [ ] **Step 2: Extract `_execute_calls` from Pass 2**

Move the body of `execute_run`'s "Pass 2" loop verbatim into a new function. Do not redesign it — the events, the ordering, and the `_would_exceed` probe all stay exactly as they are:

```python
def _execute_calls(
    *, run: Run, account_id: str, calls: List[ToolCall],
    budget: Budget, spend: Spend,
) -> _Executed:
    """Run one turn's tool calls, checking caps before each.

    A cap that can only trip once a whole turn has run is not a hard cap,
    so the probe happens per call rather than per batch.
    """
    out = _Executed()
    for call in calls:
        reason = _would_exceed(budget, spend)
        if reason:
            store.append_event(
                run=run, type=RunEventType.budget_exceeded,
                payload={"reason": reason},
            )
            out.abort = (RunStatus.aborted, reason)
            return out

        store.append_event(
            run=run, type=RunEventType.tool_called,
            payload={"tool": call.name, "input": call.input},
        )
        # Phase A spend tracking is tool calls and wall clock only.
        # `Spend.record_llm` is never called — `Turn` carries no token usage
        # back from the driver — so `spend.usd` stays 0.0 and `budget.usd_cap`
        # is inert. The tool-call and wall-clock caps carry enforcement.
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
            out.results.append({
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
            out.abort = (RunStatus.aborted, "; ".join(verdict.failures))
            return out

        store.append_event(
            run=run, type=RunEventType.tool_returned,
            payload={"tool": call.name, "output": output},
        )
        out.results.append({
            "type": "tool_result", "tool_use_id": call.id,
            "content": json.dumps(output, default=str),
        })
    return out
```

- [ ] **Step 3: Extract `_drive` from `execute_run`**

`_drive` is everything from the `for _ in range(MAX_ITERATIONS)` loop through the terminal `run_finished`. It assumes the caller has already set the run context and set status to `running`.

```python
def _drive(
    *, run: Run, account_id: str, messages: List[Dict[str, Any]],
    budget: Budget, spend: Spend, driver: Driver,
) -> Run:
    """The plan-execute-verify loop, shared by fresh starts and resumes.

    The caller owns the run context and the initial status; this owns the
    turns. Both entry points hand it a `messages` list that already ends
    somewhere the model can continue from.
    """
    def _finish(status: RunStatus, error: Optional[str]) -> Run:
        store.save_transcript(
            run_id=run.id, account_id=account_id,
            transcript=messages, spend=spend.to_dict(),
        )
        store.set_status(
            run_id=run.id, account_id=account_id, status=status, error=error,
        )
        return store.load_run(run.id, account_id)

    tools = registry.tier1_tools()

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

        # Pass 1 — gate the whole turn before executing any of it.
        # Suspension is all-or-nothing per turn: if a later call needs
        # approval, an earlier read must not have already run. Otherwise the
        # saved transcript ends with an assistant message whose tool_use
        # blocks are only partly answered, and the run cannot be resumed
        # coherently.
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
                store.set_suspended_at(run_id=run.id, account_id=account_id)
                store.set_status(
                    run_id=run.id, account_id=account_id,
                    status=RunStatus.suspended, suspended_on=q.id,
                )
                return store.load_run(run.id, account_id)

        executed = _execute_calls(
            run=run, account_id=account_id, calls=turn.tool_calls,
            budget=budget, spend=spend,
        )
        if executed.abort is not None:
            status, error = executed.abort
            return _finish(status, error)

        messages.append({"role": "user", "content": executed.results})

        # Backstop: a turn that lands exactly on a cap (or runs the clock
        # out) trips here, before another model round-trip is paid for.
        reason = exceeded(budget, spend)
        if reason:
            store.append_event(
                run=run, type=RunEventType.budget_exceeded,
                payload={"reason": reason},
            )
            return _finish(RunStatus.aborted, reason)
    else:
        # No `break`: the loop ran out of iterations while the model was
        # still asking for tools. The run is truncated mid-work, not
        # finished — reporting `done` here would make a cut-off run
        # indistinguishable from a completed one.
        reason = f"iteration cap reached: {MAX_ITERATIONS} model turns"
        store.append_event(
            run=run, type=RunEventType.budget_exceeded,
            payload={"reason": reason},
        )
        return _finish(RunStatus.aborted, reason)

    # Reached only via the `break` above: the model stopped calling tools.
    store.append_event(
        run=run, type=RunEventType.run_finished,
        payload={"spend": spend.to_dict()},
    )
    return _finish(RunStatus.done, None)
```

- [ ] **Step 4: Rewrite `execute_run` to call `_drive`**

Keep the status guard, the context handling, and the `except`/`finally` exactly as they are. Only the loop body is replaced:

```python
def execute_run(
    *, run_id: str, account_id: str, driver: Optional[Driver] = None,
) -> Run:
    run = store.load_run(run_id, account_id)
    if run is None:
        raise KeyError(f"run {run_id} not found for account {account_id}")

    # Only a run that has not finished may be started. This function rebuilds
    # the message history from the goal and ignores `run.transcript`, so
    # re-invoking a finished run would restart it from scratch and duplicate
    # its event log. Resuming a suspended run is `resume_run`, and must not
    # be silently conflated with a fresh start. `running` is allowed because
    # that is what a crashed worker leaves behind, and a retry has to be able
    # to pick it up.
    if run.status not in (RunStatus.pending, RunStatus.running):
        raise ValueError(
            f"run {run.id} is not startable: status is {run.status.value}"
        )

    driver = driver or AnthropicDriver()
    budget = Budget.from_dict(run.budget)
    spend = Spend()
    messages: List[Dict[str, Any]] = [
        {"role": "user", "content": _goal_text(run)},
    ]

    # Everything from here on runs inside the try, so `finally` always clears
    # the context. A RunContext left set on a pooled worker thread is exactly
    # the cross-account hazard context.py exists to prevent, so no statement
    # that can raise may sit between the set and the try.
    token = set_run_context(RunContext(run_id=run.id, account_id=account_id))
    try:
        store.set_status(
            run_id=run.id, account_id=account_id, status=RunStatus.running,
        )
        store.append_event(
            run=run, type=RunEventType.goal_received, payload=run.goal,
        )
        return _drive(
            run=run, account_id=account_id, messages=messages,
            budget=budget, spend=spend, driver=driver,
        )
    except Exception as exc:
        # Persist whatever the run got through before recording the failure,
        # so the run and its event log still agree. A failure to save must
        # never mask the original exception, hence the inner guard.
        try:
            store.save_transcript(
                run_id=run.id, account_id=account_id,
                transcript=messages, spend=spend.to_dict(),
            )
        except Exception:
            pass
        store.set_status(
            run_id=run.id, account_id=account_id, status=RunStatus.failed,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    finally:
        reset_run_context(token)
```

- [ ] **Step 5: Verify the refactor changed nothing**

```bash
cd backend && .venv/Scripts/python -m pytest -q
```
Expected: **all 224 pass with zero test edits.** If a test needs changing, the refactor altered behavior — revert and redo.

- [ ] **Step 6: Commit**

```bash
git add backend/app/agent_runtime/runtime.py
git commit -m "refactor(agent): extract the shared drive loop"
```

---

## Task 5: `resume_run`

**Files:**
- Modify: `backend/app/agent_runtime/runtime.py`
- Test: `backend/tests/test_agent_resume.py`

**Interfaces:**
- Consumes: `_drive`, `_execute_calls`, `Spend.from_dict` (Task 1), `store.claim_suspended` / `record_rejected_calls` (Task 3)
- Produces: `resume_run(*, run_id: str, account_id: str, decision: str, note: Optional[str] = None, driver: Optional[Driver] = None) -> Run`

`decision` is `"approve"` or `"reject"`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_agent_resume.py`:

```python
"""Answering a gate and continuing the run.

A suspended transcript ends with an assistant turn whose `tool_use` blocks
have no `tool_result`. The API rejects any continuation in that shape, so
the user's decision has to *become* the tool_result — approve produces the
tool's real output, reject produces an `is_error` result the model can
re-plan around (spec: "The API constraint that shapes everything").
"""
from __future__ import annotations

import uuid

import boto3
import pandas as pd
import pytest
from moto import mock_aws

from app.agent_runtime import artifacts, context, runtime, store
from app.agent_runtime.runtime import ToolCall, Turn
from app.db.base import session_scope
from app.db.models import AccountORM
from app.models import AutonomyLevel, RunEventType, RunStatus

BUCKET = "reconops-test-bucket"


@pytest.fixture(autouse=True)
def _s3_env(monkeypatch):
    monkeypatch.setenv("RECONOPS_S3_BUCKET", BUCKET)
    monkeypatch.setenv("RECONOPS_S3_REGION", "us-east-1")
    monkeypatch.delenv("RECONOPS_S3_ENDPOINT_URL", raising=False)
    monkeypatch.setenv("RECONOPS_S3_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("RECONOPS_S3_SECRET_ACCESS_KEY", "testing")


class ScriptedDriver:
    def __init__(self, turns):
        self._turns = list(turns)

    def next_turn(self, *, system, messages, tools, task_budget_tokens):
        self.last_messages = list(messages)
        if not self._turns:
            return Turn(text="done", tool_calls=[])
        return self._turns.pop(0)


@pytest.fixture()
def account_id() -> str:
    acct = str(uuid.uuid4())
    with session_scope() as s:
        s.add(AccountORM(id=acct, payload={}))
    return acct


def _suspend_on_profile(account_id, autonomy=AutonomyLevel.observe):
    """Drive a run to a gate and leave it suspended.

    `observe` gates even a read, which is the cheapest way to reach a
    suspend without needing a write tool's side effects.
    """
    run = store.create_run(
        account_id=account_id, goal={"intent": "look"},
        autonomy=autonomy, budget={"tool_call_cap": 10},
    )
    token = context.set_run_context(
        context.RunContext(run_id=run.id, account_id=account_id),
    )
    try:
        ds = artifacts.put_dataset(
            run_id=run.id, account_id=account_id,
            df=pd.DataFrame({"x": [1, 2, 3]}), label="d",
        )
    finally:
        context.reset_run_context(token)

    driver = ScriptedDriver([
        Turn(text=None, tool_calls=[
            ToolCall(id="t1", name="profile_schema", input={"dataset_id": ds}),
        ]),
        Turn(text="all done", tool_calls=[]),
    ])
    finished = runtime.execute_run(
        run_id=run.id, account_id=account_id, driver=driver,
    )
    assert finished.status is RunStatus.suspended
    return run.id, ds


@mock_aws
def test_approve_executes_the_pending_tool_and_continues(account_id):
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
    run_id, _ = _suspend_on_profile(account_id)

    claimed = store.claim_suspended(run_id, account_id)
    assert claimed is not None

    finished = runtime.resume_run(
        run_id=run_id, account_id=account_id, decision="approve",
        driver=ScriptedDriver([Turn(text="all done", tool_calls=[])]),
    )

    assert finished.status is RunStatus.done
    events = store.events_since(run_id=run_id, account_id=account_id)
    returned = [
        e for e in events
        if e.type is RunEventType.tool_returned
        and e.payload.get("tool") == "profile_schema"
    ]
    assert len(returned) == 1
    assert returned[0].payload["output"]["row_count"] == 3


@mock_aws
def test_reject_feeds_an_error_result_back_and_the_model_replans(account_id):
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
    run_id, _ = _suspend_on_profile(account_id)
    store.claim_suspended(run_id, account_id)

    driver = ScriptedDriver([Turn(text="understood", tool_calls=[])])
    finished = runtime.resume_run(
        run_id=run_id, account_id=account_id, decision="reject",
        note="Not on production data.", driver=driver,
    )

    assert finished.status is RunStatus.done

    # The tool must NOT have run.
    events = store.events_since(run_id=run_id, account_id=account_id)
    assert not [e for e in events if e.type is RunEventType.tool_returned]

    # The model saw a denial for every pending call, in one user turn.
    user_turns = [m for m in driver.last_messages if m["role"] == "user"]
    denial = user_turns[-1]["content"]
    assert all(b["type"] == "tool_result" for b in denial)
    assert all(b["is_error"] for b in denial)


@mock_aws
def test_the_note_lands_in_a_legal_system_position(account_id):
    """A mid-conversation system message must follow a user turn and be last."""
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
    run_id, _ = _suspend_on_profile(account_id)
    store.claim_suspended(run_id, account_id)

    driver = ScriptedDriver([Turn(text="ok", tool_calls=[])])
    runtime.resume_run(
        run_id=run_id, account_id=account_id, decision="approve",
        note="Only June, please.", driver=driver,
    )

    seen = driver.last_messages
    assert seen[-1] == {"role": "system", "content": "Only June, please."}
    assert seen[-2]["role"] == "user"
    assert seen[0]["role"] != "system"


@mock_aws
def test_spend_accumulates_across_the_suspend_boundary(account_id):
    """The caps are per-run, not per-segment."""
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
    run_id, _ = _suspend_on_profile(account_id)
    store.claim_suspended(run_id, account_id)

    finished = runtime.resume_run(
        run_id=run_id, account_id=account_id, decision="approve",
        driver=ScriptedDriver([Turn(text="done", tool_calls=[])]),
    )
    assert finished.spend["tool_calls"] == 1


@mock_aws
def test_resume_refuses_a_run_that_was_not_claimed(account_id):
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
    run_id, _ = _suspend_on_profile(account_id)

    # No claim_suspended() — the run is still `suspended`.
    with pytest.raises(ValueError):
        runtime.resume_run(
            run_id=run_id, account_id=account_id, decision="approve",
            driver=ScriptedDriver([]),
        )


def test_resume_refuses_a_finished_run(account_id):
    run = store.create_run(
        account_id=account_id, goal={}, autonomy=AutonomyLevel.auto, budget={},
    )
    store.set_status(
        run_id=run.id, account_id=account_id, status=RunStatus.done,
    )
    with pytest.raises(ValueError):
        runtime.resume_run(
            run_id=run.id, account_id=account_id, decision="approve",
            driver=ScriptedDriver([]),
        )
```

- [ ] **Step 2: Run them to verify they fail**

```bash
cd backend && .venv/Scripts/python -m pytest tests/test_agent_resume.py -q
```
Expected: FAIL — `AttributeError: module 'app.agent_runtime.runtime' has no attribute 'resume_run'`.

- [ ] **Step 3: Implement `resume_run`**

Append to `backend/app/agent_runtime/runtime.py`:

```python
APPROVE = "approve"
REJECT = "reject"


def _pending_calls(messages: List[Dict[str, Any]]) -> List[ToolCall]:
    """The unanswered tool_use blocks a suspended transcript ends with.

    Raises if the transcript is not in that shape. Every downstream step
    depends on the invariant, and a malformed continuation is a 400 from
    the API with a far less obvious message than this one.
    """
    if not messages:
        raise ValueError("cannot resume: transcript is empty")
    last = messages[-1]
    if last.get("role") != "assistant":
        raise ValueError(
            f"cannot resume: transcript ends with a {last.get('role')} turn"
        )
    calls = [
        ToolCall(
            id=b["id"], name=b["name"], input=dict(b.get("input") or {}),
        )
        for b in last.get("content", [])
        if isinstance(b, dict) and b.get("type") == "tool_use"
    ]
    if not calls:
        raise ValueError("cannot resume: no pending tool call to answer")
    return calls


def resume_run(
    *, run_id: str, account_id: str, decision: str,
    note: Optional[str] = None, driver: Optional[Driver] = None,
) -> Run:
    """Answer a gate and continue the run.

    The caller must already hold the run via `store.claim_suspended`, which
    is what makes a double-answer safe; this refuses anything not in
    `running`. The decision becomes the `tool_result` answering every
    pending `tool_use` — the API has no other legal continuation from a
    suspended transcript.
    """
    if decision not in (APPROVE, REJECT):
        raise ValueError(f"decision must be {APPROVE!r} or {REJECT!r}")

    run = store.load_run(run_id, account_id)
    if run is None:
        raise KeyError(f"run {run_id} not found for account {account_id}")
    if run.status is not RunStatus.running:
        raise ValueError(
            f"run {run.id} is not resumable: status is {run.status.value}"
        )

    driver = driver or AnthropicDriver()
    budget = Budget.from_dict(run.budget)
    spend = Spend.from_dict(run.spend)
    messages: List[Dict[str, Any]] = list(run.transcript)
    pending = _pending_calls(messages)

    token = set_run_context(RunContext(run_id=run.id, account_id=account_id))
    try:
        store.append_event(
            run=run, type=RunEventType.question_answered,
            payload={
                "decision": decision,
                "note": note,
                "tools": [c.name for c in pending],
            },
        )

        if decision == APPROVE:
            executed = _execute_calls(
                run=run, account_id=account_id, calls=pending,
                budget=budget, spend=spend,
            )
            if executed.abort is not None:
                status, error = executed.abort
                store.save_transcript(
                    run_id=run.id, account_id=account_id,
                    transcript=messages, spend=spend.to_dict(),
                )
                store.set_status(
                    run_id=run.id, account_id=account_id,
                    status=status, error=error,
                )
                return store.load_run(run.id, account_id)
            results = executed.results
        else:
            denial = note or "The user declined this action."
            results = [
                {
                    "type": "tool_result", "tool_use_id": call.id,
                    "content": f"declined: {denial}", "is_error": True,
                }
                for call in pending
            ]
            rejected = list(run.rejected_calls) + [
                {"tool": call.name, "input": call.input} for call in pending
            ]
            run.rejected_calls = rejected
            store.record_rejected_calls(
                run_id=run.id, account_id=account_id, calls=rejected,
            )
            store.append_event(
                run=run, type=RunEventType.proposal_rejected,
                payload={"tools": [c.name for c in pending]},
            )

        # One user message for the whole turn: splitting tool_results across
        # messages trains the model out of parallel tool calls.
        messages.append({"role": "user", "content": results})

        if note:
            # Mid-conversation system message (spec 4.6). Legal only here:
            # it follows a user turn and is last. Opus 4.8 only — an
            # unsupported model returns 400.
            messages.append({"role": "system", "content": note})

        return _drive(
            run=run, account_id=account_id, messages=messages,
            budget=budget, spend=spend, driver=driver,
        )
    except Exception as exc:
        try:
            store.save_transcript(
                run_id=run.id, account_id=account_id,
                transcript=messages, spend=spend.to_dict(),
            )
        except Exception:
            pass
        store.set_status(
            run_id=run.id, account_id=account_id, status=RunStatus.failed,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    finally:
        reset_run_context(token)
```

- [ ] **Step 4: Run the tests**

```bash
cd backend && .venv/Scripts/python -m pytest tests/test_agent_resume.py -q
```
Expected: PASS — all seven.

- [ ] **Step 5: Commit**

```bash
git add backend/app/agent_runtime/runtime.py backend/tests/test_agent_resume.py
git commit -m "feat(agent): resume a suspended run by answering its gate"
```

---

## Task 6: Guard against re-proposing a rejected call

A rejected turn is exactly when the model is most likely to re-propose the same call, which would gate again and ping-pong. `MAX_ITERATIONS` and `tool_call_cap` bound the loop but are not a *good* stop — the run burns its budget on something the user already refused.

**Files:**
- Modify: `backend/app/agent_runtime/runtime.py` (`_drive`)
- Test: `backend/tests/test_agent_resume.py`

**Interfaces:**
- Consumes: `Run.rejected_calls` (Task 3), `_drive` (Task 4)
- Produces: `_was_rejected(run: Run, call: ToolCall) -> bool`

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_agent_resume.py`:

```python
@mock_aws
def test_re_proposing_a_rejected_call_aborts_rather_than_gating_again(
    account_id,
):
    """Reject has to mean something.

    Without this the model re-proposes, the gate fires, the user rejects
    again — and the run spends its whole budget on a loop the user already
    refused. `MAX_ITERATIONS` would eventually stop it, but only after
    paying for every turn.
    """
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
    run_id, ds = _suspend_on_profile(account_id)
    store.claim_suspended(run_id, account_id)

    # The model immediately asks for the very call that was just refused.
    driver = ScriptedDriver([
        Turn(text=None, tool_calls=[
            ToolCall(id="t2", name="profile_schema", input={"dataset_id": ds}),
        ]),
    ])
    finished = runtime.resume_run(
        run_id=run_id, account_id=account_id, decision="reject",
        driver=driver,
    )

    assert finished.status is RunStatus.aborted
    assert "rejected" in (finished.error or "")

    events = store.events_since(run_id=run_id, account_id=account_id)
    # It aborted instead of asking a second time.
    assert len([
        e for e in events if e.type is RunEventType.question_asked
    ]) == 1


@mock_aws
def test_a_different_call_still_reaches_the_gate_after_a_rejection(account_id):
    """The guard is per-call, not a blanket freeze on the run.

    The run stays in `observe`, where every call gates — so a *different*
    call suspending again is the proof that the guard matched on arguments
    rather than freezing the run outright.
    """
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)
    run_id, _ = _suspend_on_profile(account_id)
    store.claim_suspended(run_id, account_id)

    token = context.set_run_context(
        context.RunContext(run_id=run_id, account_id=account_id),
    )
    try:
        other = artifacts.put_dataset(
            run_id=run_id, account_id=account_id,
            df=pd.DataFrame({"y": [1, 2]}), label="other",
        )
    finally:
        context.reset_run_context(token)

    driver = ScriptedDriver([
        Turn(text=None, tool_calls=[
            ToolCall(
                id="t2", name="profile_schema",
                input={"dataset_id": other},
            ),
        ]),
    ])
    finished = runtime.resume_run(
        run_id=run_id, account_id=account_id, decision="reject",
        driver=driver,
    )

    # Same tool, different arguments → not the refused call, so it gates
    # normally instead of aborting on the guard.
    assert finished.status is RunStatus.suspended
```

- [ ] **Step 2: Run them to verify they fail**

```bash
cd backend && .venv/Scripts/python -m pytest tests/test_agent_resume.py -q -k rejected
```
Expected: FAIL — the run is `suspended` (gated a second time), not `aborted`.

- [ ] **Step 3: Implement the guard**

Add the predicate to `backend/app/agent_runtime/runtime.py`, near `_pending_calls`:

```python
def _was_rejected(run: Run, call: ToolCall) -> bool:
    """Has the user already refused this exact call in this run?

    Matches on name and arguments together: refusing `run_reconciliation`
    on one pair of datasets should not block it on a different pair.
    """
    return any(
        entry.get("tool") == call.name and entry.get("input") == call.input
        for entry in run.rejected_calls
    )
```

Then in `_drive`, immediately **before** the "Pass 1" gate loop:

```python
        # A refused call must not come back around. Re-gating it would ask
        # the user the same question again and spend the run's budget on a
        # loop they already declined.
        for call in turn.tool_calls:
            if _was_rejected(run, call):
                reason = f"call already rejected by the user: {call.name}"
                store.append_event(
                    run=run, type=RunEventType.run_finished,
                    payload={"reason": "repeat_of_rejected_call",
                             "tool": call.name},
                )
                return _finish(RunStatus.aborted, reason)
```

- [ ] **Step 4: Run the tests**

```bash
cd backend && .venv/Scripts/python -m pytest tests/test_agent_resume.py -q
```
Expected: PASS — all nine.

- [ ] **Step 5: Commit**

```bash
git add backend/app/agent_runtime/runtime.py backend/tests/test_agent_resume.py
git commit -m "feat(agent): abort when the model re-proposes a rejected call"
```

---

## Task 7: HTTP surface

**Files:**
- Modify: `backend/app/agent_runtime/routes.py`
- Test: `backend/tests/test_agent_routes.py`

**Interfaces:**
- Consumes: `store.claim_suspended` (Task 3), `runtime.resume_run` (Task 5)
- Produces: `POST /api/agent/runs/{run_id}/answer` — body `{decision, note?}` → `{run_id, status}`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_agent_routes.py`:

```python
def _suspended_run(client, headers):
    """Create a run in `observe`, which gates before any tool executes."""
    created = client.post(
        "/api/agent/runs",
        json={"goal": {"intent": "test"}, "autonomy": "observe"},
        headers=headers,
    )
    assert created.status_code == 200
    return created.json()["run_id"]


def test_answering_a_run_that_is_not_suspended_is_409(client, owner_headers):
    run_id = _suspended_run(client, owner_headers)
    # The stubbed driver returns no tool calls, so this run finished rather
    # than suspending — answering it is a conflict, not a 404.
    r = client.post(
        f"/api/agent/runs/{run_id}/answer",
        json={"decision": "approve"}, headers=owner_headers,
    )
    assert r.status_code == 409


def test_answer_rejects_an_unknown_decision(client, owner_headers):
    run_id = _suspended_run(client, owner_headers)
    r = client.post(
        f"/api/agent/runs/{run_id}/answer",
        json={"decision": "maybe"}, headers=owner_headers,
    )
    assert r.status_code == 422


def test_answer_is_not_reachable_from_another_account(
    client, owner_headers, stranger,
):
    run_id = _suspended_run(client, owner_headers)
    other_client, other_headers = stranger
    r = other_client.post(
        f"/api/agent/runs/{run_id}/answer",
        json={"decision": "approve"}, headers=other_headers,
    )
    assert r.status_code == 404


def test_answer_is_404_when_the_flag_is_off(tmp_path, monkeypatch):
    monkeypatch.delenv("RECONOPS_AGENT_RUNTIME", raising=False)
    monkeypatch.setenv("RECONOPS_AUTH_DEV", "1")
    monkeypatch.setenv("RECONOPS_DATA_DIR", str(tmp_path))
    from app import main
    importlib.reload(main)
    with TestClient(main.app) as c:
        headers = _account_headers(c, "answeroff@x.co")
        r = c.post(
            "/api/agent/runs/whatever/answer",
            json={"decision": "approve"}, headers=headers,
        )
    assert r.status_code == 404
```

- [ ] **Step 2: Run them to verify they fail**

```bash
cd backend && .venv/Scripts/python -m pytest tests/test_agent_routes.py -q
```
Expected: FAIL — 404 on `/answer` (route not mounted).

- [ ] **Step 3: Add the route**

Append to `backend/app/agent_runtime/routes.py`:

```python
class AnswerRequest(BaseModel):
    decision: Literal["approve", "reject"]
    note: Optional[str] = None


@router.post("/runs/{run_id}/answer")
def answer_run(
    run_id: str,
    body: AnswerRequest,
    background_tasks: BackgroundTasks,
    account: Account = Depends(require_account),
):
    """Answer a suspended run's gate and resume it.

    The claim happens here, synchronously, and it is what makes a
    double-POST safe: a claim succeeds at most once, so the second request
    gets a 409 rather than a second background task executing the same
    pending tool. Doing it in the background task instead would make the
    409 unreportable — this handler would already have returned 200.
    """
    _require_flag()
    if store.load_run(run_id, account.id) is None:
        raise HTTPException(status_code=404, detail="Run not found.")

    claimed = store.claim_suspended(run_id, account.id)
    if claimed is None:
        raise HTTPException(
            status_code=409, detail="Run is not awaiting an answer.",
        )

    background_tasks.add_task(
        runtime.resume_run,
        run_id=run_id, account_id=account.id,
        decision=body.decision, note=body.note,
    )
    return {"run_id": run_id, "status": RunStatus.running.value}
```

Add `Literal` to the typing import at the top of `routes.py`:

```python
from typing import Any, Dict, Literal, Optional
```

- [ ] **Step 4: Run the route tests**

```bash
cd backend && .venv/Scripts/python -m pytest tests/test_agent_routes.py -q
```
Expected: PASS.

- [ ] **Step 5: Run the full suite**

```bash
cd backend && .venv/Scripts/python -m pytest -q
```
Expected: all pass — the pre-existing 224 plus the new cases.

- [ ] **Step 6: Update the Phase A plan's known-gaps list**

In `docs/plans/2026-08-01-phase-a-agent-runtime.md`, the first entry under "Known gaps deferred past Phase A" begins **"Resume is not wired."** Replace that entry with:

```markdown
- **Resume is wired.** Answering a gate and continuing a run landed in
  [`2026-08-03-agent-resume.md`](2026-08-03-agent-resume.md). What remains
  deferred is the abandonment reaper for runs that sit suspended forever —
  `runs.suspended_at` exists for it.
```

- [ ] **Step 7: Commit**

```bash
git add backend/app/agent_runtime/routes.py backend/tests/test_agent_routes.py docs/plans/2026-08-01-phase-a-agent-runtime.md
git commit -m "feat(agent): endpoint to answer a suspended run"
```

---

## Definition of done

1. `POST /api/agent/runs/{id}/answer` with `{"decision": "approve"}` executes the pending tool and the run continues to `done`.
2. `{"decision": "reject"}` feeds an `is_error` result back; the model re-plans and the run still completes.
3. A rejected call re-proposed verbatim aborts with `repeat_of_rejected_call` instead of gating twice.
4. Two concurrent answers produce one 200 and one 409, and the tool executes exactly once.
5. `tool_calls` and `usd` accumulate across the suspend boundary — the caps are per-run.
6. A run suspended for longer than `wall_clock_s` still resumes.
7. `run_reconciliation` is dispatchable, so an approved macro-tool gate actually runs.
8. The full pre-existing suite passes unchanged.

## Deferred past this plan

- **Abandonment reaper.** `runs.suspended_at` is written but never read. Reaping runs that sit suspended indefinitely is a scheduled job, modelled on `storage.reap_stale_jobs`.
- **`ask_user` as a registered tool.** Every suspend today is an autonomy gate. When a question tool lands it reuses this same `tool_result` path.
- **Per-call approval.** The gate is all-or-nothing per turn; splitting it needs partial-turn execution ordering.
- **Autonomy-dial changes mid-run.** Spec §4.6 mentions the same mechanism carrying a dial flip.
- **Durable execution.** Resume rides `BackgroundTasks`, so a restart mid-resume leaves a row in `running`. Same swap as run creation.
- **USD accounting.** `Spend.record_llm` is still never called, so `usd_cap` remains inert.
