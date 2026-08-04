from __future__ import annotations

import importlib

import boto3
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

from app.agent_runtime import artifacts, context, runtime, store
from app.agent_runtime.runtime import ToolCall, Turn
from app.models import AutonomyLevel, RunStatus

BUCKET = "reconops-test-bucket"


@pytest.fixture(autouse=True)
def _s3_env(monkeypatch):
    monkeypatch.setenv("RECONOPS_S3_BUCKET", BUCKET)
    monkeypatch.setenv("RECONOPS_S3_REGION", "us-east-1")
    monkeypatch.delenv("RECONOPS_S3_ENDPOINT_URL", raising=False)
    monkeypatch.setenv("RECONOPS_S3_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("RECONOPS_S3_SECRET_ACCESS_KEY", "testing")


def _create_bucket() -> None:
    boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=BUCKET)


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


def _sse_ids(body: str) -> list[int]:
    return [
        int(line.split(":", 1)[1].strip())
        for line in body.splitlines()
        if line.startswith("id:")
    ]


def test_stream_emits_sse_frames_and_terminates_on_a_finished_run(
    client, owner_headers,
):
    """The stream is the headline surface; it must close, not hang.

    The generator breaks once the run reaches a terminal status, so a run
    that already finished under the stubbed LLM drains in one pass. Without
    that break this request would poll forever and the test would hang.
    """
    created = client.post(
        "/api/agent/runs", json={"goal": {"intent": "test"}}, headers=owner_headers,
    )
    run_id = created.json()["run_id"]

    r = client.get(
        f"/api/agent/runs/{run_id}/events/stream", headers=owner_headers,
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")

    body = r.text
    assert "event: goal_received" in body
    assert "data: " in body
    assert _sse_ids(body), "each frame carries an id for Last-Event-ID resume"


def test_stream_resumes_after_last_event_id(client, owner_headers):
    """Reconnect must not replay what the client already has."""
    created = client.post(
        "/api/agent/runs", json={"goal": {"intent": "test"}}, headers=owner_headers,
    )
    run_id = created.json()["run_id"]

    full = client.get(
        f"/api/agent/runs/{run_id}/events/stream", headers=owner_headers,
    )
    ids = _sse_ids(full.text)
    assert len(ids) >= 2, "need at least two events to prove the cursor works"

    resumed = client.get(
        f"/api/agent/runs/{run_id}/events/stream",
        headers={**owner_headers, "Last-Event-ID": str(ids[0])},
    )
    assert _sse_ids(resumed.text) == ids[1:]


def test_stream_is_not_readable_from_another_account(
    client, owner_headers, stranger,
):
    created = client.post(
        "/api/agent/runs", json={"goal": {"intent": "test"}}, headers=owner_headers,
    )
    run_id = created.json()["run_id"]

    other_client, other_headers = stranger
    r = other_client.get(
        f"/api/agent/runs/{run_id}/events/stream", headers=other_headers,
    )
    assert r.status_code == 404


def test_run_is_not_readable_from_another_account(client, owner_headers, stranger):
    created = client.post(
        "/api/agent/runs", json={"goal": {"intent": "test"}}, headers=owner_headers,
    )
    run_id = created.json()["run_id"]

    other_client, other_headers = stranger
    r = other_client.get(f"/api/agent/runs/{run_id}", headers=other_headers)
    assert r.status_code == 404


class ScriptedDriver:
    """A driver that plays back fixed turns — see test_agent_resume.py."""

    def __init__(self, turns):
        self._turns = list(turns)

    def next_turn(self, *, system, messages, tools, task_budget_tokens):
        if not self._turns:
            return Turn(text="done", tool_calls=[])
        return self._turns.pop(0)


def _finished_run(client, headers):
    """Create a run in `observe` — which finishes here, not suspends.

    `observe` gates before any tool executes, so on a real driver this
    would suspend. But this module's `client` fixture sets
    `RECONOPS_STUB_LLM=1`, and under that flag `AnthropicDriver.next_turn`
    unconditionally returns zero tool calls — there is never a call to
    gate, so the run runs straight through to `done`. That is still useful
    here: a finished run is exactly a run `claim_suspended` refuses, which
    is all the 409/422/cross-account tests below need — none of them
    assert anything about genuine suspension. A genuinely suspended run is
    built separately, by `_suspend_via_store` below.
    """
    created = client.post(
        "/api/agent/runs",
        json={"goal": {"intent": "test"}, "autonomy": "observe"},
        headers=headers,
    )
    assert created.status_code == 200
    return created.json()["run_id"]


def _suspend_via_store(account_id):
    """Drive a run to a genuine suspend, directly through store/runtime.

    A run created through the HTTP API can never suspend under this
    module's stub driver (see `_finished_run`'s docstring), so this
    builds one the way `test_agent_resume.py` builds its fixtures instead:
    create the run and its dataset directly, then drive one gated tool
    call through `runtime.execute_run` with a `ScriptedDriver` that
    actually proposes a call. `observe` gates even a read, which is the
    cheapest way to reach a suspend.
    """
    run = store.create_run(
        account_id=account_id, goal={"intent": "look"},
        autonomy=AutonomyLevel.observe, budget={"tool_call_cap": 10},
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
    return run.id


def test_answering_a_run_that_is_not_suspended_is_409(client, owner_headers):
    run_id = _finished_run(client, owner_headers)
    # This run finished rather than suspending — answering it is a
    # conflict, not a 404.
    r = client.post(
        f"/api/agent/runs/{run_id}/answer",
        json={"decision": "approve"}, headers=owner_headers,
    )
    assert r.status_code == 409


def test_answer_rejects_an_unknown_decision(client, owner_headers):
    run_id = _finished_run(client, owner_headers)
    r = client.post(
        f"/api/agent/runs/{run_id}/answer",
        json={"decision": "maybe"}, headers=owner_headers,
    )
    assert r.status_code == 422


def test_answer_is_not_reachable_from_another_account(
    client, owner_headers, stranger,
):
    run_id = _finished_run(client, owner_headers)
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


@mock_aws
def test_answering_a_suspended_run_returns_200_and_resumes_it(
    client, owner_headers,
):
    """The 200 path — structurally impossible via HTTP-created runs.

    Built directly through the store layer (`_suspend_via_store`), then
    answered through the real route. `TestClient` runs `BackgroundTasks`
    before returning the response (see
    `test_events_endpoint_supports_after_cursor` above), so asserting the
    run's terminal status proves `resume_run` actually ran to completion in
    the background task — not merely that the route returned 200. Approve
    dispatches the pending `profile_schema` call for real, then the stub
    driver's next turn has no tool calls, so the loop breaks and the run
    reaches `done`.
    """
    account_id = owner_headers["X-Account-Id"]
    _create_bucket()
    run_id = _suspend_via_store(account_id)

    r = client.post(
        f"/api/agent/runs/{run_id}/answer",
        json={"decision": "approve"}, headers=owner_headers,
    )
    assert r.status_code == 200
    assert r.json() == {"run_id": run_id, "status": "running"}

    finished = client.get(f"/api/agent/runs/{run_id}", headers=owner_headers)
    assert finished.status_code == 200
    assert finished.json()["status"] == "done"

    events = client.get(
        f"/api/agent/runs/{run_id}/events", headers=owner_headers,
    ).json()["events"]
    returned = [
        e for e in events
        if e["type"] == "tool_returned"
        and e["payload"].get("tool") == "profile_schema"
    ]
    assert len(returned) == 1


@mock_aws
def test_double_answering_a_suspended_run_is_one_200_and_one_409(
    client, owner_headers,
):
    """The route-level half of the concurrency guarantee.

    `claim_suspended`'s lock is proven under genuine thread contention at
    the store layer (test_agent_store.py); this proves the route wires
    that guarantee through end to end: a sequential double-POST produces
    exactly one 200 and one 409, and the pending tool executes exactly
    once — not twice.
    """
    account_id = owner_headers["X-Account-Id"]
    _create_bucket()
    run_id = _suspend_via_store(account_id)

    first = client.post(
        f"/api/agent/runs/{run_id}/answer",
        json={"decision": "approve"}, headers=owner_headers,
    )
    assert first.status_code == 200
    assert first.json() == {"run_id": run_id, "status": "running"}

    second = client.post(
        f"/api/agent/runs/{run_id}/answer",
        json={"decision": "approve"}, headers=owner_headers,
    )
    assert second.status_code == 409

    events = client.get(
        f"/api/agent/runs/{run_id}/events", headers=owner_headers,
    ).json()["events"]
    returned = [
        e for e in events
        if e["type"] == "tool_returned"
        and e["payload"].get("tool") == "profile_schema"
    ]
    assert len(returned) == 1
