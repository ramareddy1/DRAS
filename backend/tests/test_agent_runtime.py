from __future__ import annotations

import uuid

import boto3
import pandas as pd
import pytest
from moto import mock_aws

from app.agent_runtime import artifacts, runtime, store
from app.agent_runtime.runtime import Turn, ToolCall
from app.db.base import session_scope
from app.db.models import AccountORM
from app.models import AutonomyLevel, RunEventType, RunStatus


@pytest.fixture(autouse=True)
def _s3_env(monkeypatch):
    """Object storage config for tests that call artifacts.put_dataset.

    Mirrors the convention in test_agent_artifacts.py / test_agent_tools_core.py:
    moto's @mock_aws intercepts the actual calls, this fixture just makes sure
    storage_s3._client() has something to read regardless of the real
    environment (CI/dev sets no RECONOPS_S3_* at all).
    """
    monkeypatch.setenv("RECONOPS_S3_BUCKET", "reconops-test-bucket")
    monkeypatch.setenv("RECONOPS_S3_REGION", "us-east-1")
    monkeypatch.delenv("RECONOPS_S3_ENDPOINT_URL", raising=False)
    monkeypatch.setenv("RECONOPS_S3_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("RECONOPS_S3_SECRET_ACCESS_KEY", "testing")


@pytest.fixture(autouse=True)
def _isolate_critic_state():
    """Snapshot and restore module-global critic state.

    test_critic_failure_aborts_the_run registers an always-failing check on
    "profile_schema" via critic.register_check(). critic._CHECKS is a module
    global shared with test_agent_critic.py and every other test in this
    file, so without cleanup that check leaks into every later test in the
    same session. Restore by in-place mutation (not rebinding) so any other
    module holding a reference to the original dict still sees the reset —
    same shape as _isolate_registry_state in test_agent_registry.py.
    """
    from app.agent_runtime import critic

    saved = {k: list(v) for k, v in critic._CHECKS.items()}

    yield

    critic._CHECKS.clear()
    critic._CHECKS.update(saved)


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


@mock_aws
def test_read_tool_executes_and_emits_call_and_return(account_id):
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="reconops-test-bucket")
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


@mock_aws
def test_observe_mode_suspends_before_running_a_read_tool(account_id):
    """The dial is enforced by the loop, not by prompting."""
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="reconops-test-bucket")
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


@mock_aws
def test_critic_failure_aborts_the_run(account_id):
    """A failed post-condition is never swallowed (spec 2.4)."""
    from app.agent_runtime import critic

    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="reconops-test-bucket")
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


@mock_aws
def test_tool_call_cap_stops_the_run(account_id):
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="reconops-test-bucket")
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
