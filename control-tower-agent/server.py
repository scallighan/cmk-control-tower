"""FastAPI server exposing the CMK Control Tower dispute-resolution pipeline.

The UI lists OPEN disputes from the SQL ledger; selecting one kicks off the
Microsoft Agent Framework workflow (``main.py``) which runs the five specialist
agents and suspends at the human-in-the-loop approval gate.

    GET  /api/disputes                  -> OPEN dispute work queue (from ledger)
    GET  /api/disputes/{dispute_id}      -> full dispute context (pre-run detail)
    POST /api/disputes/{dispute_id}/runs -> start the agent pipeline for a dispute
    GET  /api/runs/{run_id}              -> current run state (all agent findings)
    POST /api/runs/{run_id}/decision     -> submit approve/deny/modify and resume
    GET  /api/trades/{trade_id}          -> full DB detail behind a booked trade

Workflow instances are held in memory keyed by ``run_id`` because resuming a
suspended workflow must reuse the same instance.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import db
import main as pipeline
from artifacts import HumanDecision

load_dotenv()

app = FastAPI(title="CMK Control Tower", version="4.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# In-memory run registry.
# ---------------------------------------------------------------------------
class RunSession:
    def __init__(self, run_id: str, dispute_id: str) -> None:
        self.run_id = run_id
        self.dispute_id = dispute_id
        self.created_at = datetime.now(timezone.utc)
        self.workflow = pipeline.build_workflow()
        self.result: Any = None
        self.started = False  # guards the one-shot streaming run


RUNS: dict[str, RunSession] = {}


# ---------------------------------------------------------------------------
# API models.
# ---------------------------------------------------------------------------
class PendingApproval(BaseModel):
    request_id: str
    request: dict[str, Any]


class DecisionRequest(BaseModel):
    request_id: str
    action: Literal["approve", "deny", "modify"]
    approver: str = "ops_analyst"
    final_resolution: str | None = None
    note: str = ""


class RunState(BaseModel):
    run_id: str
    dispute_id: str
    created_at: str
    status: Literal["running", "awaiting_approval", "completed"]
    dispute: dict[str, Any] | None
    trade: dict[str, Any] | None
    confirmation: dict[str, Any] | None
    derived: dict[str, Any] | None
    intake: dict[str, Any] | None
    prediction: dict[str, Any] | None
    reconstruction: dict[str, Any] | None
    root_cause: dict[str, Any] | None
    remediation: dict[str, Any] | None
    stage_io: dict[str, Any]
    pending: list[PendingApproval]
    approval: dict[str, Any] | None
    receipts: list[dict[str, Any]]
    outputs: list[str]


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------
def _state_get(session: RunSession):
    state = session.workflow._state  # noqa: SLF001 - intentional read of run state

    def getter(key: str) -> Any:
        try:
            return state.get(key)
        except Exception:
            return None

    return getter


def _dump(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, BaseModel):
        return value.model_dump()
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    return value


def _serialize_state(session: RunSession) -> RunState:
    get = _state_get(session)
    context = get(pipeline.CONTEXT_STATE_KEY) or {}

    pending: list[PendingApproval] = []
    if session.result is not None:
        for event in session.result.get_request_info_events():
            pending.append(
                PendingApproval(request_id=event.request_id, request=_dump(event.data))
            )

    outputs = list(session.result.get_outputs()) if session.result is not None else []
    if session.result is None:
        status: Literal["running", "awaiting_approval", "completed"] = "running"
    elif pending:
        status = "awaiting_approval"
        outputs = []
    else:
        status = "completed"

    return RunState(
        run_id=session.run_id,
        dispute_id=session.dispute_id,
        created_at=session.created_at.isoformat(),
        status=status,
        dispute=context.get("dispute"),
        trade=context.get("trade"),
        confirmation=context.get("confirmation"),
        derived=context.get("derived"),
        intake=_dump(get(pipeline.INTAKE_STATE_KEY)),
        prediction=_dump(get(pipeline.PREDICTION_STATE_KEY)),
        reconstruction=_dump(get(pipeline.RECONSTRUCTION_STATE_KEY)),
        root_cause=_dump(get(pipeline.ROOTCAUSE_STATE_KEY)),
        remediation=_dump(get(pipeline.REMEDIATION_STATE_KEY)),
        stage_io=get(pipeline.STAGE_IO_STATE_KEY) or {},
        pending=pending,
        approval=_dump(get(pipeline.APPROVAL_STATE_KEY)),
        receipts=[_dump(r) for r in (get(pipeline.RECEIPTS_STATE_KEY) or [])],
        outputs=[str(o) for o in outputs],
    )


# ---------------------------------------------------------------------------
# Routes.
# ---------------------------------------------------------------------------
@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/disputes")
async def list_disputes(limit: int = 200) -> list[dict[str, Any]]:
    try:
        return db.fetch_open_disputes(limit=limit)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Dispute list failed: {exc}") from exc


@app.get("/api/disputes/{dispute_id}")
async def get_dispute(dispute_id: str) -> dict[str, Any]:
    try:
        context = db.fetch_dispute_context(dispute_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Dispute lookup failed: {exc}") from exc
    if context is None:
        raise HTTPException(status_code=404, detail="Dispute not found")
    context["derived"] = pipeline._derived(context)  # noqa: SLF001
    return context


@app.post("/api/disputes/{dispute_id}/runs", response_model=RunState)
async def start_run(dispute_id: str) -> RunState:
    """Create a run session. The pipeline itself is driven by the SSE events stream."""
    run_id = uuid.uuid4().hex[:12]
    session = RunSession(run_id, dispute_id)
    RUNS[run_id] = session
    return _serialize_state(session)


def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


async def _event_stream(session: RunSession) -> AsyncIterator[str]:
    """Run the workflow with streaming, translating MAF events into SSE frames.

    ``executor_invoked`` marks a stage as processing; ``executor_completed`` reads the
    stage's persisted input/output from workflow state and emits it as done. A final
    ``state`` frame carries the full RunState (pending approval / receipts / outputs).
    """
    # Reconnect / double-open: just replay the current state.
    if session.started:
        yield _sse("state", _serialize_state(session).model_dump())
        return
    session.started = True

    get = _state_get(session)
    try:
        stream = session.workflow.run(session.dispute_id, stream=True)
        async for ev in stream:
            etype = ev.type
            exec_id = getattr(ev, "executor_id", None)
            stage = pipeline.STAGE_BY_EXECUTOR.get(exec_id or "")
            if etype == "executor_invoked" and stage:
                yield _sse("stage", {"stage": stage, "phase": "processing"})
            elif etype == "executor_completed" and stage:
                io = (get(pipeline.STAGE_IO_STATE_KEY) or {}).get(stage, {})
                yield _sse(
                    "stage",
                    {
                        "stage": stage,
                        "phase": "done",
                        "input": io.get("input"),
                        "output": io.get("output"),
                    },
                )
            elif etype == "error":
                yield _sse("run_error", {"message": str(ev.data)})
        session.result = await stream.get_final_response()
    except Exception as exc:  # noqa: BLE001
        yield _sse("run_error", {"message": str(exc)})
        return
    yield _sse("state", _serialize_state(session).model_dump())


@app.get("/api/runs/{run_id}/events")
async def run_events(run_id: str) -> StreamingResponse:
    session = RUNS.get(run_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return StreamingResponse(
        _event_stream(session),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/runs/{run_id}", response_model=RunState)
async def get_run(run_id: str) -> RunState:
    session = RUNS.get(run_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return _serialize_state(session)


@app.post("/api/runs/{run_id}/decision", response_model=RunState)
async def submit_decision(run_id: str, body: DecisionRequest) -> RunState:
    session = RUNS.get(run_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Run not found")

    pending_ids = (
        {e.request_id for e in session.result.get_request_info_events()} if session.result else set()
    )
    if body.request_id not in pending_ids:
        raise HTTPException(status_code=409, detail="No pending approval with that request_id")

    decision = HumanDecision(
        action=body.action,
        approver=body.approver,
        final_resolution=body.final_resolution,
        note=body.note,
    )
    try:
        session.result = await session.workflow.run(responses={body.request_id: decision})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Pipeline resume failed: {exc}") from exc
    return _serialize_state(session)


@app.get("/api/trades/{trade_id}")
async def get_trade_detail(trade_id: str) -> dict[str, Any]:
    try:
        detail = db.fetch_trade_detail(trade_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Lookup failed: {exc}") from exc
    if detail is None:
        raise HTTPException(status_code=404, detail="Trade not found")
    return detail


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
