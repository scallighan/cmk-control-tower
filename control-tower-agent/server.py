"""FastAPI server exposing the CMK Control Tower reconciliation workflow.

Wraps the Microsoft Agent Framework workflow from ``main.py`` behind an HTTP API
so a React UI can drive the human-in-the-loop approval gate:

    POST /api/runs                      -> upload a confirmations CSV; runs the
                                           reconciliation agent until it suspends
                                           for human approval (or completes)
    GET  /api/runs                      -> list runs
    GET  /api/runs/{run_id}             -> current state of a run
    POST /api/runs/{run_id}/decision    -> submit approve/deny/modify and resume
    GET  /api/trades/{trade_id}         -> full DB detail behind a booked trade

Workflow instances are held in memory keyed by ``run_id`` because resuming a
suspended workflow (``workflow.run(responses=...)``) must reuse the same
instance. This is single-process, in-memory state suitable for a demo.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import db
import main as recon

load_dotenv()

app = FastAPI(title="CMK Control Tower", version="2.0.0")

# Allow the Vite dev server (and any local origin) to call the API.
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
    """Holds a live workflow instance and its most recent result."""

    def __init__(self, run_id: str, filename: str | None) -> None:
        self.run_id = run_id
        self.filename = filename
        self.created_at = datetime.now(timezone.utc)
        self.workflow = recon.build_workflow()
        self.result: Any = None


RUNS: dict[str, RunSession] = {}


# ---------------------------------------------------------------------------
# API request/response models.
# ---------------------------------------------------------------------------
class FieldSuggestionModel(BaseModel):
    trade_id: str
    field: str
    current_value: str
    suggested_value: str
    reason: str


class AssessedItem(BaseModel):
    trade_id: str
    source: str
    matched: bool
    summary: str
    confirmation: dict[str, Any]
    trade: dict[str, Any]
    suggestions: list[FieldSuggestionModel]


class PendingApproval(BaseModel):
    request_id: str
    suggestions: list[FieldSuggestionModel]
    summaries: list[dict[str, str]]


class RunState(BaseModel):
    run_id: str
    filename: str | None
    created_at: str
    status: Literal["awaiting_approval", "completed"]
    items: list[AssessedItem]
    unknown_trade_ids: list[str]
    pending: list[PendingApproval]
    outputs: list[str]


class DecisionRequest(BaseModel):
    request_id: str
    action: Literal["approve", "deny", "modify"]
    modified_suggestions: list[FieldSuggestionModel] = []


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------
def _read_state(session: RunSession, key: str) -> Any:
    """Read a shared-state value set by the workflow executors (or None)."""
    state = session.workflow._state  # noqa: SLF001 - intentional read of run state
    try:
        return state.get(key)
    except Exception:
        return None


def _build_items(session: RunSession) -> list[AssessedItem]:
    """Combine paired confirmations with the agent's per-item assessments."""
    items: dict[str, recon.PairedItem] = _read_state(session, recon.ITEMS_STATE_KEY) or {}
    assessments: dict[str, recon.ItemAssessment] = (
        _read_state(session, recon.ASSESSMENTS_STATE_KEY) or {}
    )
    result: list[AssessedItem] = []
    for tid, item in items.items():
        assessment = assessments.get(tid)
        # Re-derive the verdict from the (possibly corrected) in-memory
        # confirmation so the UI reflects post-approval state too.
        matched = recon.reconciles(item)
        summary = assessment.summary if assessment else ("Reconciles." if matched else "")
        suggestions = (
            [FieldSuggestionModel(**s.model_dump()) for s in assessment.suggestions]
            if assessment
            else []
        )
        result.append(
            AssessedItem(
                trade_id=tid,
                source=item.source,
                matched=matched,
                summary=summary,
                confirmation=recon._confirmation_public(item),  # noqa: SLF001
                trade=recon._trade_public(item),  # noqa: SLF001
                suggestions=suggestions,
            )
        )
    return result


def _serialize_state(session: RunSession) -> RunState:
    """Translate the workflow's current result into an API-friendly state."""
    result = session.result
    pending: list[PendingApproval] = []
    if result is not None:
        for event in result.get_request_info_events():
            approval: recon.ApprovalRequest = event.data
            pending.append(
                PendingApproval(
                    request_id=event.request_id,
                    suggestions=[FieldSuggestionModel(**s.model_dump()) for s in approval.suggestions],
                    summaries=[dict(s) for s in approval.summaries],
                )
            )

    outputs = list(result.get_outputs()) if result is not None else []
    status: Literal["awaiting_approval", "completed"] = "awaiting_approval" if pending else "completed"
    # While awaiting approval the workflow may surface intermediate agent output;
    # only expose real outputs once the run has completed.
    if pending:
        outputs = []
    return RunState(
        run_id=session.run_id,
        filename=session.filename,
        created_at=session.created_at.isoformat(),
        status=status,
        items=_build_items(session),
        unknown_trade_ids=_read_state(session, recon.UNKNOWN_STATE_KEY) or [],
        pending=pending,
        outputs=[str(o) for o in outputs],
    )


# ---------------------------------------------------------------------------
# Routes.
# ---------------------------------------------------------------------------
@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/runs", response_model=RunState)
async def start_run(
    file: UploadFile | None = File(default=None),
    csv_text: str | None = Form(default=None),
) -> RunState:
    """Start a reconciliation run from an uploaded confirmations CSV."""
    if file is not None:
        raw = await file.read()
        text = raw.decode("utf-8-sig")
        filename = file.filename
    elif csv_text is not None:
        text = csv_text
        filename = None
    else:
        raise HTTPException(status_code=400, detail="Provide a CSV file upload or csv_text form field.")

    if not text.strip():
        raise HTTPException(status_code=400, detail="The uploaded CSV is empty.")

    run_id = uuid.uuid4().hex[:12]
    session = RunSession(run_id, filename)
    RUNS[run_id] = session
    try:
        session.result = await session.workflow.run(text)
    except Exception as exc:  # surface DB/LLM/auth failures to the client
        RUNS.pop(run_id, None)
        raise HTTPException(status_code=502, detail=f"Workflow run failed: {exc}") from exc
    return _serialize_state(session)


@app.get("/api/runs", response_model=list[RunState])
async def list_runs() -> list[RunState]:
    return [_serialize_state(s) for s in RUNS.values()]


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

    pending_ids = {e.request_id for e in session.result.get_request_info_events()} if session.result else set()
    if body.request_id not in pending_ids:
        raise HTTPException(status_code=409, detail="No pending approval with that request_id")

    decision = recon.HumanDecision(
        action=body.action,
        modified_suggestions=[recon.FieldSuggestion(**s.model_dump()) for s in body.modified_suggestions],
    )
    try:
        session.result = await session.workflow.run(responses={body.request_id: decision})
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Workflow resume failed: {exc}") from exc
    return _serialize_state(session)


@app.get("/api/trades/{trade_id}")
async def get_trade_detail(trade_id: str) -> dict[str, Any]:
    """Full DB detail behind a booked trade (trade, security, counterparties)."""
    try:
        detail = db.fetch_trade_detail(trade_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Lookup failed: {exc}") from exc
    if detail is None:
        raise HTTPException(status_code=404, detail="Trade not found")
    return detail


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
