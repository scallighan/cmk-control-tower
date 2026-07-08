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
from typing import Annotated, Any, AsyncIterator, Literal

from agent_framework import tool
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import db
import main as pipeline
import agents as ag
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
        # Conversational review assistant (lazily created on first chat turn).
        self.chat_agent: Any = None
        self.chat_session: Any = None
        self.chat_log: list[dict[str, str]] = []  # [{role, content}] for the UI
        self.chat_context_dirty = True  # re-inject the run snapshot on the next chat turn
        # Set by the rerun tool during a chat turn: {"stage", "feedback"}. The UI
        # picks this up and drives a live, streamed rerun of the pipeline.
        self.pending_rerun: dict[str, str] | None = None
        self.chat_lock = asyncio.Lock()  # serialize chat/rerun mutations of state


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


class ChatRequest(BaseModel):
    message: str


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class RerunSignal(BaseModel):
    stage: str
    feedback: str


class ChatResponse(BaseModel):
    reply: str
    history: list[ChatMessage]
    # Present only when the assistant asked (via its tool) to re-run a step, so the
    # UI can drive a live, streamed rerun of the pipeline from the main view.
    rerun: RerunSignal | None = None


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
        # The approval payload is rebuilt on the fly whenever a stage is re-run
        # (see /rerun), so prefer the version persisted in workflow state; fall
        # back to the request captured by MAF when the run first suspended.
        state_req = get(pipeline.APPROVAL_REQUEST_STATE_KEY)
        for event in session.result.get_request_info_events():
            pending.append(
                PendingApproval(request_id=event.request_id, request=state_req or _dump(event.data))
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


# ---------------------------------------------------------------------------
# Conversational review assistant + step rerun (available while a run waits at
# the human-in-the-loop approval gate).
# ---------------------------------------------------------------------------
def _chat_history(session: RunSession) -> list[ChatMessage]:
    return [ChatMessage(role=m["role"], content=m["content"]) for m in session.chat_log]  # type: ignore[arg-type]


def _build_rerun_tool(session: RunSession):
    """A per-run FunctionTool that lets the review assistant *request* a step rerun.

    It does not run anything itself — it records the request on the session so the
    chat endpoint can hand it back to the UI, which then drives a live, streamed
    rerun in the main pipeline view (visible re-processing + highlighted changes).
    The human never leaves the approval gate.
    """

    @tool(
        name="rerun_step",
        description=(
            "Re-run one specialist step of THIS dispute's pipeline with the reviewer's "
            "feedback, followed by every downstream step, to test a different assumption. "
            "Calling this KICKS OFF the rerun live in the main pipeline view — the affected "
            "agents visibly re-process and the changed steps are highlighted. Use it only "
            "when the reviewer wants to change or re-test a step's conclusion. It never "
            "approves, denies, or executes anything."
        ),
    )
    async def rerun_step(
        stage: Annotated[
            str,
            "Which step to re-run: one of intake, prediction, reconstruction, root_cause, remediation.",
        ],
        feedback: Annotated[
            str,
            "Clear guidance capturing the reviewer's intent, in your own words (e.g. 'treat the "
            "SSI snapshot as stale' or 'this is a fee break, not economic').",
        ] = "",
    ) -> str:
        norm = (stage or "").strip().lower()
        if norm not in pipeline.STAGE_SEQUENCE:
            return (
                f"Invalid stage '{stage}'. Choose one of: {', '.join(pipeline.STAGE_SEQUENCE)}."
            )
        downstream = pipeline.STAGE_SEQUENCE[pipeline.STAGE_SEQUENCE.index(norm):]
        session.pending_rerun = {"stage": norm, "feedback": feedback or ""}
        return json.dumps(
            {
                "status": "rerun_started",
                "stage": norm,
                "also_reruns": downstream[1:],
                "note": (
                    "The rerun is now running live in the main pipeline view; the affected "
                    "steps will re-process and be highlighted as revised."
                ),
            },
            default=str,
        )

    return rerun_step


@app.get("/api/runs/{run_id}/chat", response_model=list[ChatMessage])
async def get_chat(run_id: str) -> list[ChatMessage]:
    session = RUNS.get(run_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return _chat_history(session)


@app.post("/api/runs/{run_id}/chat", response_model=ChatResponse)
async def post_chat(run_id: str, body: ChatRequest) -> ChatResponse:
    session = RUNS.get(run_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Run not found")
    message = body.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="Empty message")
    if session.result is None:
        raise HTTPException(
            status_code=409,
            detail="The agents are still running — wait for the run to reach the approval gate.",
        )

    async with session.chat_lock:
        if session.chat_agent is None:
            session.chat_agent = ag.create_conversation_agent(rerun_tool=_build_rerun_tool(session))
            session.chat_session = session.chat_agent.create_session()

        # Re-inject the run snapshot on the first turn and after any rerun so the
        # assistant always reasons over the current findings; otherwise the MAF
        # session already carries the earlier context, so send just the question.
        if session.chat_context_dirty or not session.chat_log:
            snapshot = pipeline.build_conversation_snapshot(session.workflow._state)  # noqa: SLF001
            user_input = (
                "Current snapshot of the dispute run you are assisting with — the dispute, trade, "
                "every agent's findings, and the pending approval proposal — as JSON:\n\n"
                + json.dumps(snapshot, default=str)
                + "\n\nUse this as the ground truth for my questions.\n\nQuestion: "
                + message
            )
            session.chat_context_dirty = False
        else:
            user_input = message

        session.pending_rerun = None
        try:
            response = await session.chat_agent.run(user_input, session=session.chat_session)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"Assistant failed: {exc}") from exc

        reply = (getattr(response, "text", "") or "").strip() or "(no response)"
        session.chat_log.append({"role": "user", "content": message})
        session.chat_log.append({"role": "assistant", "content": reply})
        # If the assistant asked to re-run a step, hand the request back so the UI
        # can drive the streamed rerun in the main pipeline view.
        signal = RerunSignal(**session.pending_rerun) if session.pending_rerun else None
        return ChatResponse(reply=reply, history=_chat_history(session), rerun=signal)


async def _rerun_event_stream(session: RunSession, stage: str, feedback: str) -> AsyncIterator[str]:
    """Stream a live rerun of ``stage`` + downstream stages as SSE frames.

    Emits ``stage`` frames (processing → done, flagged ``revised``) for each stage
    that re-runs, then rebuilds the approval proposal and emits a terminal
    ``state`` frame with the refreshed RunState. Mirrors the initial run stream so
    the frontend can reuse its stage-card update path.
    """
    stage = (stage or "").strip().lower()
    if stage not in pipeline.STAGE_SEQUENCE:
        yield _sse("run_error", {"message": f"Unknown stage '{stage}'."})
        return

    async with session.chat_lock:
        state = session.workflow._state  # noqa: SLF001
        seq = pipeline.STAGE_SEQUENCE[pipeline.STAGE_SEQUENCE.index(stage):]
        try:
            for s in seq:
                yield _sse("stage", {"stage": s, "phase": "processing", "revised": True})
                await pipeline._rerun_one(state, s, feedback if s == stage else "")  # noqa: SLF001
                io = (state.get(pipeline.STAGE_IO_STATE_KEY) or {}).get(s, {})
                yield _sse(
                    "stage",
                    {
                        "stage": s,
                        "phase": "done",
                        "input": io.get("input"),
                        "output": io.get("output"),
                        "revised": True,
                    },
                )
            pipeline.rebuild_approval_request(state)
        except Exception as exc:  # noqa: BLE001
            yield _sse("run_error", {"message": f"Rerun failed: {exc}"})
            return
        # The assistant should reason over the refreshed findings on its next turn.
        session.chat_context_dirty = True
    yield _sse("state", _serialize_state(session).model_dump())


@app.get("/api/runs/{run_id}/rerun/stream")
async def rerun_stream(run_id: str, stage: str, feedback: str = "") -> StreamingResponse:
    session = RUNS.get(run_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if session.result is None or not session.result.get_request_info_events():
        raise HTTPException(
            status_code=409,
            detail="Re-running a step is only available while the dispute is awaiting approval.",
        )
    return StreamingResponse(
        _rerun_event_stream(session, stage, feedback),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/trades/{trade_id}")
async def get_trade_detail(trade_id: str) -> dict[str, Any]:
    try:
        detail = db.fetch_trade_detail(trade_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Lookup failed: {exc}") from exc
    if detail is None:
        raise HTTPException(status_code=404, detail="Trade not found")
    return detail


# ---------------------------------------------------------------------------
# Static frontend (built React app). When the Vite build output exists (as it
# does in the container image), the same server serves the UI and the API from
# a single origin, so no CORS/proxy is needed in production.
# ---------------------------------------------------------------------------
import os

from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

_WEB_DIST = os.path.join(os.path.dirname(__file__), "web", "dist")

if os.path.isdir(_WEB_DIST):
    app.mount(
        "/assets",
        StaticFiles(directory=os.path.join(_WEB_DIST, "assets")),
        name="assets",
    )

    @app.get("/")
    async def _spa_index() -> FileResponse:
        return FileResponse(os.path.join(_WEB_DIST, "index.html"))

    @app.get("/{full_path:path}")
    async def _spa_fallback(full_path: str) -> FileResponse:
        # Never shadow the API; let unmatched /api paths 404 as usual.
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found")
        candidate = os.path.join(_WEB_DIST, full_path)
        if full_path and os.path.isfile(candidate):
            return FileResponse(candidate)
        return FileResponse(os.path.join(_WEB_DIST, "index.html"))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
