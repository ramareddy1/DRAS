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
