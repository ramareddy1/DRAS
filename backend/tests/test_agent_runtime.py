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


class LoopingDriver:
    """Never stops asking for tools — drives the iteration cap."""

    def __init__(self, turn):
        self._turn = turn
        self.turns_served = 0

    def next_turn(self, *, system, messages, tools, task_budget_tokens):
        self.turns_served += 1
        return self._turn


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
    # Exactly the goal turn and the model's reply — nothing else is mirrored.
    assert len(reloaded.transcript) == 2
    assert reloaded.transcript[0] == {"role": "user", "content": "test"}
    assert reloaded.transcript[1] == {
        "role": "assistant",
        "content": [{"type": "text", "text": "hello"}],
    }
    assert reloaded.spend["tool_calls"] == 0


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
    events = store.events_since(run_id=run.id, account_id=account_id)
    types = [e.type for e in events]
    assert RunEventType.question_asked in types
    assert RunEventType.tool_returned not in types
    # The pointer has to name the question the user is being asked, not just
    # be set: resume reads the run back through this id.
    question = next(e for e in events if e.type == RunEventType.question_asked)
    assert result.suspended_on == question.id
    assert question.payload["tool"] == "profile_schema"


@mock_aws
def test_a_gated_call_blocks_every_call_in_the_same_turn(account_id):
    """Suspension is all-or-nothing per turn (no orphaned tool_use blocks)."""
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="reconops-test-bucket")
    run = _make_run(account_id, autonomy=AutonomyLevel.assist)
    ds = _dataset(run, account_id)
    driver = FakeDriver([
        Turn(text=None, tool_calls=[
            # A read that would run unattended in assist mode...
            ToolCall(id="t1", name="profile_schema", input={"dataset_id": ds}),
            # ...followed by an unregistered tool, which gates (fail closed).
            ToolCall(id="t2", name="post_to_slack", input={"text": "hi"}),
        ]),
    ])

    result = runtime.execute_run(
        run_id=run.id, account_id=account_id, driver=driver,
    )

    assert result.status is RunStatus.suspended
    events = store.events_since(run_id=run.id, account_id=account_id)
    types = [e.type for e in events]
    assert RunEventType.question_asked in types
    # Nothing in the turn ran — not even the read that precedes the gate.
    assert RunEventType.tool_called not in types
    assert RunEventType.tool_returned not in types
    question = next(e for e in events if e.type == RunEventType.question_asked)
    assert question.payload["tool"] == "post_to_slack"
    assert result.suspended_on == question.id
    # The saved transcript ends on the assistant turn; both tool_use blocks
    # are unanswered together, which is what makes the run resumable.
    assert result.transcript[-1]["role"] == "assistant"
    assert [b["id"] for b in result.transcript[-1]["content"]] == ["t1", "t2"]


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
    events = store.events_since(run_id=run.id, account_id=account_id)
    types = [e.type for e in events]
    assert RunEventType.budget_exceeded in types
    # A hard cap of 1 means exactly one tool call ran. Counting the status
    # alone would pass even if the cap overshot by a whole batch.
    called = [e for e in events if e.type == RunEventType.tool_called]
    assert len(called) == 1
    assert result.spend["tool_calls"] == 1


@mock_aws
def test_tool_call_cap_stops_mid_turn_not_after_the_batch(account_id):
    """One turn asking for three calls under a cap of 1 runs exactly one."""
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="reconops-test-bucket")
    run = _make_run(account_id, budget={"tool_call_cap": 1})
    ds = _dataset(run, account_id)
    driver = FakeDriver([
        Turn(text=None, tool_calls=[
            ToolCall(id=f"t{i}", name="profile_schema",
                     input={"dataset_id": ds})
            for i in range(3)
        ]),
    ])

    result = runtime.execute_run(
        run_id=run.id, account_id=account_id, driver=driver,
    )

    assert result.status is RunStatus.aborted
    events = store.events_since(run_id=run.id, account_id=account_id)
    called = [e for e in events if e.type == RunEventType.tool_called]
    returned = [e for e in events if e.type == RunEventType.tool_returned]
    assert len(called) == 1
    assert len(returned) == 1
    assert RunEventType.budget_exceeded in [e.type for e in events]


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


@mock_aws
def test_both_abort_paths_persist_the_transcript_and_spend(account_id):
    """An aborted run must not disagree with its own event log."""
    from app.agent_runtime import critic

    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="reconops-test-bucket")

    # Path 1: the budget cap.
    budget_run = _make_run(account_id, budget={"tool_call_cap": 1})
    ds = _dataset(budget_run, account_id)
    call = ToolCall(id="t", name="profile_schema", input={"dataset_id": ds})
    runtime.execute_run(
        run_id=budget_run.id, account_id=account_id,
        driver=FakeDriver([
            Turn(text=None, tool_calls=[call]),
            Turn(text=None, tool_calls=[call]),
        ]),
    )
    reloaded = store.load_run(budget_run.id, account_id)
    assert reloaded.status is RunStatus.aborted
    assert reloaded.transcript, "budget abort dropped the transcript"
    assert reloaded.transcript[0]["role"] == "user"
    assert reloaded.spend["tool_calls"] == 1

    # Path 2: a failed post-condition. Registered only now so the run above
    # is unaffected; _isolate_critic_state undoes it after the test.
    critic.register_check("profile_schema", lambda out: "synthetic failure")
    critic_run = _make_run(account_id)
    ds2 = _dataset(critic_run, account_id)
    runtime.execute_run(
        run_id=critic_run.id, account_id=account_id,
        driver=FakeDriver([
            Turn(text="looking", tool_calls=[
                ToolCall(id="c1", name="profile_schema",
                         input={"dataset_id": ds2}),
            ]),
        ]),
    )
    reloaded = store.load_run(critic_run.id, account_id)
    assert reloaded.status is RunStatus.aborted
    assert reloaded.transcript, "critic abort dropped the transcript"
    assert reloaded.transcript[0]["role"] == "user"
    assert reloaded.spend["tool_calls"] == 1


def test_iteration_exhaustion_aborts_rather_than_reporting_done(
    account_id, monkeypatch,
):
    """A run cut off mid-work is not a finished run."""
    monkeypatch.setattr(runtime, "MAX_ITERATIONS", 3)
    run = _make_run(account_id)
    driver = LoopingDriver(Turn(text=None, tool_calls=[
        ToolCall(id="t1", name="profile_schema",
                 input={"dataset_id": "does-not-exist"}),
    ]))

    result = runtime.execute_run(
        run_id=run.id, account_id=account_id, driver=driver,
    )

    assert result.status is RunStatus.aborted
    assert driver.turns_served == 3
    types = [e.type for e in store.events_since(
        run_id=run.id, account_id=account_id,
    )]
    assert RunEventType.run_finished not in types
    assert RunEventType.budget_exceeded in types
    assert "iteration cap" in result.error
    assert result.transcript, "iteration abort dropped the transcript"


def test_execute_run_refuses_a_finished_run(account_id):
    """Re-invoking a terminal run would restart it and duplicate its log."""
    run = _make_run(account_id)
    finished = runtime.execute_run(
        run_id=run.id, account_id=account_id,
        driver=FakeDriver([Turn(text="ok", tool_calls=[])]),
    )
    assert finished.status is RunStatus.done

    with pytest.raises(ValueError) as err:
        runtime.execute_run(
            run_id=run.id, account_id=account_id,
            driver=FakeDriver([Turn(text="again", tool_calls=[])]),
        )
    assert run.id in str(err.value)
    assert "done" in str(err.value)

    events = store.events_since(run_id=run.id, account_id=account_id)
    goals = [e for e in events if e.type == RunEventType.goal_received]
    assert len(goals) == 1
    assert store.load_run(run.id, account_id).status is RunStatus.done


def test_execute_run_refuses_a_suspended_run(account_id):
    """Resume is a separate capability, never a silent restart."""
    run = _make_run(account_id)
    suspended = runtime.execute_run(
        run_id=run.id, account_id=account_id,
        driver=FakeDriver([Turn(text=None, tool_calls=[
            ToolCall(id="t1", name="post_to_slack", input={"text": "hi"}),
        ])]),
    )
    assert suspended.status is RunStatus.suspended

    with pytest.raises(ValueError) as err:
        runtime.execute_run(
            run_id=run.id, account_id=account_id,
            driver=FakeDriver([Turn(text="retry", tool_calls=[])]),
        )
    assert "suspended" in str(err.value)
    assert store.load_run(run.id, account_id).status is RunStatus.suspended


def test_system_prompt_puts_core_instructions_first(account_id):
    """Cache layering: the stable block must precede account-specific text."""
    run = _make_run(account_id)
    driver = FakeDriver([Turn(text="ok", tool_calls=[])])

    runtime.execute_run(run_id=run.id, account_id=account_id, driver=driver)

    system = driver.seen_systems[0]
    assert isinstance(system, list)
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert "never compute money" in system[0]["text"]
