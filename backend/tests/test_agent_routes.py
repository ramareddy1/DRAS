from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


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
