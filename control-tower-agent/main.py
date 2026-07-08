"""CMK Control Tower: a multi-agent counterparty-dispute resolution pipeline.

Built on the Microsoft Agent Framework (``agent_framework``) workflow engine and
running against the real Azure SQL **ledger** database ``cmk-sqldb-ledger``
(``demo4_*`` tables, read-only).

The user picks an OPEN dispute from the work queue; selecting it kicks off this
Orchestrator workflow, which loads the dispute's full trade lifecycle from the
ledger and routes it through five specialist agents, then a human-in-the-loop
approval gate:

    select dispute -> load context (SQL ledger, read-only)
       -> Intake Agent         : classify dispute, severity, routing
       -> Prediction Agent      : pre-cutoff settlement risk
       -> Reconstruction Agent  : 6-artifact evidence pack + ACL proof status
       -> Root-Cause Agent      : primary break type + recommended resolution
       -> Remediation Agent      : HITL-gated remediation proposal + chaser
       -> Orchestrator           : HUMAN approve / deny / modify
                                 -> write ApprovalRecord + ACLReceipt (simulated)

Nothing is ever written back to the ledger tables; the approval + receipt are
kept in memory with a real SHA-256 digest.

Prerequisites:
- FOUNDRY_PROJECT_ENDPOINT / FOUNDRY_MODEL for Azure AI Foundry.
- Azure CLI login (``az login``) for AzureCliCredential (Foundry + SQL).
- ODBC Driver 18 for SQL Server (pyodbc). SQL_SERVER / SQL_DATABASE in env.
"""

import asyncio
import json
import sys
import dataclasses
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from agent_framework import (
    Agent,
    Executor,
    WorkflowBuilder,
    WorkflowContext,
    executor,
    handler,
    response_handler,
)
from azure.identity import AzureCliCredential
from dotenv import load_dotenv
from typing_extensions import Never

import agents as ag
import db
from signals import (
    derived as _derived,
    economics as _economics,
    num as _num,
    ssi_mismatch as _ssi_mismatch,
    timing as _timing,
)
from artifacts import (
    ACLReceipt,
    ApprovalRecord,
    HumanDecision,
    canonical_digest,
)

load_dotenv()

# ---------------------------------------------------------------------------
# Shared-state keys (read back by the FastAPI server to render the UI).
# ---------------------------------------------------------------------------
CONTEXT_STATE_KEY = "context"  # full dispute context dict from db
INTAKE_STATE_KEY = "intake"  # IntakeResult
PREDICTION_STATE_KEY = "prediction"  # PredictionResult
RECONSTRUCTION_STATE_KEY = "reconstruction"  # ReconstructionResult
ROOTCAUSE_STATE_KEY = "root_cause"  # RootCauseResult
REMEDIATION_STATE_KEY = "remediation"  # RemediationResult
APPROVAL_STATE_KEY = "approval_record"  # ApprovalRecord | None
APPROVAL_REQUEST_STATE_KEY = "approval_request"  # DisputeApprovalRequest (dict) surfaced at the HITL gate
RECEIPTS_STATE_KEY = "receipts"  # list[ACLReceipt]
STAGE_IO_STATE_KEY = "stage_io"  # {stage: {input, output}} for live streaming

# Maps workflow executor ids to the UI-facing stage names streamed to the client.
STAGE_BY_EXECUTOR = {
    "load_dispute": "context",
    "intake_stage": "intake",
    "prediction_stage": "prediction",
    "reconstruction_stage": "reconstruction",
    "rootcause_stage": "root_cause",
    "remediation_stage": "remediation",
    "orchestrator": "orchestrator",
}


# ---------------------------------------------------------------------------
# HITL payload surfaced to the human at the approval gate.
# ---------------------------------------------------------------------------
@dataclass
class DisputeApprovalRequest:
    dispute_id: str
    category: str
    proposed_action: str
    proposed_amount: float
    approver_role: str
    requires_dual_approval: bool
    hitl_summary: str
    draft_communication: dict[str, str]
    root_cause_narrative: str


def build_approval_request(
    dispute_id: str,
    c: dict[str, Any],
    remediation: "ag.RemediationResult",
    rootcause: "ag.RootCauseResult | None",
) -> DisputeApprovalRequest:
    """Assemble the HITL approval payload from the remediation + root-cause findings."""
    return DisputeApprovalRequest(
        dispute_id=dispute_id,
        category=c["dispute"]["category"],
        proposed_action=remediation.proposed_action,
        proposed_amount=remediation.proposed_amount,
        approver_role=remediation.approver_role,
        requires_dual_approval=remediation.approver_role == "dual_approval",
        hitl_summary=remediation.hitl_summary,
        draft_communication=remediation.draft_communication.model_dump(),
        root_cause_narrative=rootcause.root_cause_narrative if rootcause else "",
    )


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------
def _jsonify(value: Any) -> str:
    return json.dumps(value, indent=2, default=str)


async def _run_agent(agent: Agent, prompt: str) -> str:
    response = await agent.run(prompt)
    return response.text


def _record_io(ctx: WorkflowContext, stage: str, input_payload: Any, output: Any) -> None:
    """Persist a stage's input + output to shared state so the server can stream them live.

    Custom ``intermediate`` events are reserved by MAF, so instead the FastAPI SSE
    endpoint reads this map when each stage's ``executor_completed`` event fires.
    """
    io = dict(ctx.get_state(STAGE_IO_STATE_KEY) or {})
    io[stage] = {
        "input": input_payload,
        "output": output.model_dump() if hasattr(output, "model_dump") else output,
    }
    ctx.set_state(STAGE_IO_STATE_KEY, io)


# ---------------------------------------------------------------------------
# Stage 0: load the dispute context from the ledger.
# ---------------------------------------------------------------------------
@executor(id="load_dispute")
async def load_dispute(dispute_id: str, ctx: WorkflowContext[str, str]) -> None:
    context = db.fetch_dispute_context(dispute_id, AzureCliCredential())
    if context is None:
        await ctx.yield_output(f"Dispute {dispute_id} not found in the ledger.")
        return
    context["derived"] = _derived(context)
    ctx.set_state(CONTEXT_STATE_KEY, context)
    _record_io(
        ctx,
        "context",
        {"dispute_id": dispute_id},
        {
            "dispute": context["dispute"],
            "trade": context["trade"],
            "confirmation": context["confirmation"],
            "derived": context["derived"],
        },
    )
    await ctx.send_message(dispute_id, target_id="intake_stage")


# ---------------------------------------------------------------------------
# Specialist stages.
#
# Each stage's core logic (payload assembly, prompt, agent call, deterministic
# fallback) lives in a module-level ``run_<stage>`` coroutine so it can be
# invoked BOTH from the workflow Executors (the normal end-to-end run) and from
# the rerun engine (``rerun_stage``) when a human re-runs one step with feedback.
# The runners are side-effect free: they return ``(result, payload)`` and never
# touch workflow state; the caller persists them.
# ---------------------------------------------------------------------------

# The five specialist stages in execution order (used by the rerun engine).
STAGE_SEQUENCE = ["intake", "prediction", "reconstruction", "root_cause", "remediation"]


def _with_feedback(prompt: str, feedback: str) -> str:
    """Append a human reviewer's feedback to a stage prompt for a targeted rerun."""
    fb = (feedback or "").strip()
    if not fb:
        return prompt
    return (
        prompt
        + "\n\n--- HUMAN REVIEWER FEEDBACK (re-run) ---\n"
        "A human reviewer re-ran this step with the feedback below. Treat it as "
        "authoritative guidance that overrides your earlier assumptions where they "
        "conflict, and reflect its impact in your output fields:\n" + fb
    )


def _presence(c: dict[str, Any]) -> dict[str, bool]:
    return {
        "trade_msg": c["trade"] is not None,
        "alloc_msg": _num(c.get("allocations_count")) > 0,
        "confirm_msg": c["confirmation"] is not None,
        "affirm_msg": c["affirmation"] is not None,
        "ssi_msg": c["ssi_snapshot"] is not None,
        "settle_msg": c["settlement_status"] is not None,
    }


async def run_intake(
    agent: Agent, c: dict[str, Any], feedback: str = ""
) -> tuple["ag.IntakeResult", dict[str, Any]]:
    payload = {
        "dispute": c["dispute"],
        "trade": c["trade"],
        "confirmation": c["confirmation"],
        "evidence_pack": c["evidence_pack"],
        "counterparty_buy": c["counterparty_buy"],
        "counterparty_sell": c["counterparty_sell"],
        "communications": c["communications"],
        "derived": c["derived"],
    }
    prompt = _with_feedback(
        "Classify and register this newly opened dispute. Extract entities, assess severity, and "
        f"recommend routing. Return JSON per the schema.\n\nDispute context:\n{_jsonify(payload)}",
        feedback,
    )
    try:
        result = ag.IntakeResult.model_validate_json(await _run_agent(agent, prompt))
    except Exception:  # noqa: BLE001
        result = _intake_fallback(c)
    return result, payload


def _intake_fallback(c: dict[str, Any]) -> "ag.IntakeResult":
    d = c["dispute"]
    ep = c["evidence_pack"] or {}
    completeness = _num(ep.get("completeness_pct"))
    notional = _num(d.get("notional_usd"))
    breach = c["derived"]["timing"]["timing_breach_flag"]
    severity = "HIGH" if notional > 1_000_000 or breach else ("MEDIUM" if completeness < 1 else "LOW")
    return ag.IntakeResult(
        dispute_id=d["dispute_id"],
        classification=d["category"],
        severity=severity,
        entities=ag.IntakeEntities(
            dispute_id=d["dispute_id"], trade_id=d["trade_id"],
            cp_buy_id=d.get("cp_buy_id") or "", cp_sell_id=d.get("cp_sell_id") or "",
            category=d["category"], notional_usd=notional, opened_ts_utc=str(d.get("opened_ts_utc") or ""),
        ),
        evidence_completeness_pct=completeness,
        recommended_agents=["prediction", "reconstruction", "root_cause", "remediation"],
        routing_notes="Deterministic fallback classification from the dispute record.",
    )


async def run_prediction(
    agent: Agent, c: dict[str, Any], feedback: str = ""
) -> tuple["ag.PredictionResult", dict[str, Any]]:
    payload = {
        "trade": c["trade"], "security": c["security"], "confirmation": c["confirmation"],
        "affirmation": c["affirmation"], "settlement_instruction": c["settlement_instruction"],
        "settlement_status": c["settlement_status"], "ssi_snapshot": c["ssi_snapshot"],
        "ssi_current": c["ssi_current"], "cp_profile": c["cp_profile"], "derived": c["derived"],
    }
    prompt = _with_feedback(
        "Score the pre-cutoff settlement risk for this disputed trade and identify the primary risk "
        f"driver. Return JSON per the schema.\n\nTrade signals:\n{_jsonify(payload)}",
        feedback,
    )
    try:
        result = ag.PredictionResult.model_validate_json(await _run_agent(agent, prompt))
    except Exception:  # noqa: BLE001
        result = _prediction_fallback(c)
    return result, payload


def _prediction_fallback(c: dict[str, Any]) -> "ag.PredictionResult":
    dv = c["derived"]
    breach = dv["timing"]["timing_breach_flag"]
    stale = dv["ssi"]["snapshot_is_stale"]
    econ = dv["economics"]["has_economic_break"]
    score = min(2.0, 0.4 * breach + 0.5 * stale + 0.6 * econ + 0.3 * _num((c["trade"] or {}).get("is_stress_day")))
    driver = "timing" if breach else "ssi" if stale else "economic" if econ else "counterparty"
    return ag.PredictionResult(
        trade_id=(c["trade"] or {}).get("trade_id", c["dispute"]["trade_id"]),
        pre_cutoff_risk_score=round(score, 2),
        primary_risk_driver=driver,
        time_sensitivity="URGENT" if breach else "MONITOR" if score > 0.5 else "STANDARD",
        signal_breakdown=ag.PredictionSignals(
            timing_breach_flag=breach, affirm_status=dv["timing"]["affirm_status"],
            ssi_mismatch_prob=0.8 if stale else 0.1,
            cp_fail_propensity=min(1.0, _num((c["cp_profile"] or {}).get("fail_disputes")) / 50.0),
            liquidity_stress_score=_num((c["trade"] or {}).get("is_stress_day")),
        ),
        recommended_actions=["Monitor affirmation status", "Verify SSI currency"],
        confidence=0.6,
    )


async def run_reconstruction(
    agent: Agent, c: dict[str, Any], feedback: str = ""
) -> tuple["ag.ReconstructionResult", dict[str, Any]]:
    payload = {
        "dispute": c["dispute"], "evidence_pack": c["evidence_pack"], "acl_receipt": c["acl_receipt"],
        "presence": _presence(c), "ssi": c["derived"]["ssi"],
    }
    prompt = _with_feedback(
        "Assemble the 6-artifact evidence pack and verify the ACL proof chain. Mark each artifact "
        f"present/absent from the presence map. Return JSON per the schema.\n\n{_jsonify(payload)}",
        feedback,
    )
    try:
        result = ag.ReconstructionResult.model_validate_json(await _run_agent(agent, prompt))
    except Exception:  # noqa: BLE001
        result = _reconstruction_fallback(c)
    return result, payload


def _reconstruction_fallback(c: dict[str, Any]) -> "ag.ReconstructionResult":
    p = _presence(c)
    ep = c["evidence_pack"] or {}
    acl = c["acl_receipt"] or {}
    gaps = [k for k, present in p.items() if not present]
    return ag.ReconstructionResult(
        dispute_id=c["dispute"]["dispute_id"],
        evidence_completeness_pct=_num(ep.get("completeness_pct")),
        artifacts=ag.EvidenceArtifacts(**p),
        gaps=[f"Missing {g}" for g in gaps],
        ledger_verified=bool(acl.get("verify_level_1")) and bool(acl.get("verify_level_2")),
        proof_integrity_score=1.0 if acl.get("verify_level_2") else 0.5,
        acl_lag_minutes=int(_num(acl.get("lag_minutes"))),
        ssi_freshness_days=0,
        reconstruction_notes="Deterministic fallback from evidence pack + ACL receipt.",
    )


async def run_rootcause(
    agent: Agent,
    c: dict[str, Any],
    prediction: "ag.PredictionResult | None",
    reconstruction: "ag.ReconstructionResult | None",
    feedback: str = "",
) -> tuple["ag.RootCauseResult", dict[str, Any]]:
    payload = {
        "dispute": c["dispute"], "trade": c["trade"], "confirmation": c["confirmation"],
        "settlement_status": c["settlement_status"], "cp_profile": c["cp_profile"],
        "derived": c["derived"],
        "prediction": prediction.model_dump() if prediction else None,
        "reconstruction": reconstruction.model_dump() if reconstruction else None,
    }
    prompt = _with_feedback(
        "Diagnose the primary break type and recommend a resolution, grounded in the evidence and "
        f"signals. Return JSON per the schema.\n\n{_jsonify(payload)}",
        feedback,
    )
    try:
        result = ag.RootCauseResult.model_validate_json(await _run_agent(agent, prompt))
    except Exception:  # noqa: BLE001
        result = _rootcause_fallback(c)
    return result, payload


def _rootcause_fallback(c: dict[str, Any]) -> "ag.RootCauseResult":
    d = c["dispute"]
    econ = c["derived"]["economics"]
    category = d["category"]
    resolution_map = {
        "economic": "ADJUST", "ssi": "NO_ACTION", "affirmation": "NO_ACTION",
        "fail": "BUY_IN", "cns_claim": "CLAIM", "trs_reset": "REBOOK",
        "corp_action": "REBOOK", "fee": "ADJUST",
    }
    repeat = _num((c["cp_profile"] or {}).get("open_disputes")) > 10
    return ag.RootCauseResult(
        dispute_id=d["dispute_id"],
        primary_break_type=category,
        confidence=0.7,
        break_details=ag.BreakDetails(
            broken_field=econ.get("broken_field") or "",
            break_amount=abs(_num(econ.get("gross_break_amount"))),
            counterparty_pattern="repeat_offender" if repeat else "first_occurrence",
            contributing_factors=["Deterministic fallback classification"],
        ),
        recommended_resolution=resolution_map.get(category, "NO_ACTION"),
        requires_hitl=category == "economic",
        root_cause_narrative=f"Fallback: classified as {category} from the dispute record.",
    )


async def run_remediation(
    agent: Agent,
    c: dict[str, Any],
    rootcause: "ag.RootCauseResult | None",
    reconstruction: "ag.ReconstructionResult | None",
    feedback: str = "",
) -> tuple["ag.RemediationResult", dict[str, Any]]:
    payload = {
        "dispute": c["dispute"], "trade": c["trade"], "communications": c["communications"],
        "cp_profile": c["cp_profile"], "derived": c["derived"],
        "root_cause": rootcause.model_dump() if rootcause else None,
        "reconstruction": reconstruction.model_dump() if reconstruction else None,
    }
    prompt = _with_feedback(
        "Draft a remediation proposal for human approval based on the root cause. Never execute. "
        f"Return JSON per the schema.\n\n{_jsonify(payload)}",
        feedback,
    )
    try:
        result = ag.RemediationResult.model_validate_json(await _run_agent(agent, prompt))
    except Exception:  # noqa: BLE001
        result = _remediation_fallback(c, rootcause)
    return result, payload


def _remediation_fallback(c: dict[str, Any], rootcause: Any) -> "ag.RemediationResult":
    d = c["dispute"]
    amount = abs(_num(c["derived"]["economics"].get("gross_break_amount")))
    action = rootcause.recommended_resolution if rootcause else "NO_ACTION"
    action = action if action in {
        "ADJUST", "REBOOK", "CLAIM", "WRITE_OFF", "BUY_IN", "NO_ACTION"} else "NO_ACTION"

    summary = f"Fallback proposal for {d['category']} dispute {d['dispute_id']}."

    # Role selection: dual approval for large economic adjustments; escalate
    # (compliance) when the ledger proof is unverified.
    acl = c.get("acl_receipt") or {}
    ledger_verified = bool(acl.get("verify_level_1")) and bool(acl.get("verify_level_2"))
    role = "dual_approval" if amount > 100_000 else "ops_analyst"
    if not ledger_verified:
        role = "compliance"
        summary += " Ledger proof unverified — routed to compliance."

    # Never write off > $50k silently: force an explicit escalation.
    if action == "WRITE_OFF" and amount > 50_000:
        role = "compliance" if role == "ops_analyst" else role
        summary += " WRITE_OFF over $50,000 — requires explicit escalation before execution."

    # Internal escalation for repeat-offender counterparties (proxy for
    # cp_repeat_score > 0.7 from the counterparty's dispute history).
    prof = c["derived"].get("cp_profile") or {}
    total = _num(prof.get("total_disputes"))
    repeat_ratio = _num(prof.get("fail_disputes")) / total if total else 0.0
    if repeat_ratio > 0.7:
        summary += " Repeat-offender counterparty — internal ops escalation recommended."

    urgency_hours = 4.0
    deadline = (datetime.now(timezone.utc) + timedelta(hours=urgency_hours)).isoformat()

    return ag.RemediationResult(
        dispute_id=d["dispute_id"],
        proposed_action=action,
        proposed_amount=amount,
        regulatory_cost_if_unresolved=round(amount * 0.0001, 2),
        urgency_hours=urgency_hours,
        draft_communication=ag.DraftCommunication(
            to="counterparty_ops",
            subject=f"Dispute {d['dispute_id']} — action required",
            body=f"Our records differ from your confirmation on {d['trade_id']}. Please review and affirm.",
        ),
        hitl_summary=summary,
        hitl_required=True,
        approval_deadline_utc=deadline,
        approver_role=role,
    )


class IntakeStage(Executor):
    def __init__(self, agent: Agent) -> None:
        super().__init__(id="intake_stage")
        self._agent = agent

    @handler
    async def run(self, dispute_id: str, ctx: WorkflowContext[str]) -> None:
        c = ctx.get_state(CONTEXT_STATE_KEY)
        result, payload = await run_intake(self._agent, c)
        ctx.set_state(INTAKE_STATE_KEY, result)
        _record_io(ctx, "intake", payload, result)
        await ctx.send_message(dispute_id, target_id="prediction_stage")


class PredictionStage(Executor):
    def __init__(self, agent: Agent) -> None:
        super().__init__(id="prediction_stage")
        self._agent = agent

    @handler
    async def run(self, dispute_id: str, ctx: WorkflowContext[str]) -> None:
        c = ctx.get_state(CONTEXT_STATE_KEY)
        result, payload = await run_prediction(self._agent, c)
        ctx.set_state(PREDICTION_STATE_KEY, result)
        _record_io(ctx, "prediction", payload, result)
        await ctx.send_message(dispute_id, target_id="reconstruction_stage")


class ReconstructionStage(Executor):
    def __init__(self, agent: Agent) -> None:
        super().__init__(id="reconstruction_stage")
        self._agent = agent

    @handler
    async def run(self, dispute_id: str, ctx: WorkflowContext[str]) -> None:
        c = ctx.get_state(CONTEXT_STATE_KEY)
        result, payload = await run_reconstruction(self._agent, c)
        ctx.set_state(RECONSTRUCTION_STATE_KEY, result)
        _record_io(ctx, "reconstruction", payload, result)
        await ctx.send_message(dispute_id, target_id="rootcause_stage")


class RootCauseStage(Executor):
    def __init__(self, agent: Agent) -> None:
        super().__init__(id="rootcause_stage")
        self._agent = agent

    @handler
    async def run(self, dispute_id: str, ctx: WorkflowContext[str]) -> None:
        c = ctx.get_state(CONTEXT_STATE_KEY)
        result, payload = await run_rootcause(
            self._agent, c, ctx.get_state(PREDICTION_STATE_KEY), ctx.get_state(RECONSTRUCTION_STATE_KEY)
        )
        ctx.set_state(ROOTCAUSE_STATE_KEY, result)
        _record_io(ctx, "root_cause", payload, result)
        await ctx.send_message(dispute_id, target_id="remediation_stage")


class RemediationStage(Executor):
    def __init__(self, agent: Agent) -> None:
        super().__init__(id="remediation_stage")
        self._agent = agent

    @handler
    async def run(self, dispute_id: str, ctx: WorkflowContext[str]) -> None:
        c = ctx.get_state(CONTEXT_STATE_KEY)
        result, payload = await run_remediation(
            self._agent, c, ctx.get_state(ROOTCAUSE_STATE_KEY), ctx.get_state(RECONSTRUCTION_STATE_KEY)
        )
        ctx.set_state(REMEDIATION_STATE_KEY, result)
        _record_io(ctx, "remediation", payload, result)
        await ctx.send_message(dispute_id, target_id="orchestrator")


# ---------------------------------------------------------------------------
# Orchestrator: human approval + simulated ledger write.
# ---------------------------------------------------------------------------
class OrchestratorExecutor(Executor):
    def __init__(self) -> None:
        super().__init__(id="orchestrator")

    @handler
    async def request_approval(self, dispute_id: str, ctx: WorkflowContext) -> None:
        c = ctx.get_state(CONTEXT_STATE_KEY)
        remediation = ctx.get_state(REMEDIATION_STATE_KEY)
        rootcause = ctx.get_state(ROOTCAUSE_STATE_KEY)
        req = build_approval_request(dispute_id, c, remediation, rootcause)
        ctx.set_state(APPROVAL_REQUEST_STATE_KEY, dataclasses.asdict(req))
        await ctx.request_info(req, response_type=HumanDecision)

    @response_handler
    async def apply_decision(
        self,
        original_request: DisputeApprovalRequest,
        decision: HumanDecision,
        ctx: WorkflowContext[Never, str],
    ) -> None:
        c = ctx.get_state(CONTEXT_STATE_KEY)
        remediation = ctx.get_state(REMEDIATION_STATE_KEY)
        dispute_id = original_request.dispute_id

        if decision.action == "deny":
            resolution = "NO_ACTION"
        elif decision.action == "modify":
            resolution = decision.final_resolution or remediation.proposed_action
        else:
            resolution = remediation.proposed_action

        approval = ApprovalRecord(
            dispute_id=dispute_id,
            action=decision.action,
            approver=decision.approver,
            resolution=resolution,
            note=decision.note or original_request.hitl_summary,
        )
        ctx.set_state(APPROVAL_STATE_KEY, approval)

        # Simulated ACL receipts: the (real) evidence-pack digest from the ledger,
        # plus a fresh digest over the approval record.
        receipts: list[ACLReceipt] = []
        ep = c.get("evidence_pack") or {}
        if ep.get("digest_hash"):
            receipts.append(ACLReceipt(
                dispute_id=dispute_id, artifact="EvidencePack",
                digest_hash=str(ep["digest_hash"]).strip(),
            ))
        receipts.append(ACLReceipt(
            dispute_id=dispute_id, artifact="ApprovalRecord",
            digest_hash=canonical_digest(approval.model_dump()),
        ))
        ctx.set_state(RECEIPTS_STATE_KEY, receipts)

        summary = (
            f"Dispute {dispute_id}: human decision '{decision.action}' by {decision.approver}. "
            f"Resolution: {resolution}. Wrote {len(receipts)} simulated ledger receipt(s)."
        )
        await ctx.yield_output(summary)


# ---------------------------------------------------------------------------
# Workflow assembly.
# ---------------------------------------------------------------------------
def build_workflow():
    intake = IntakeStage(ag.create_intake_agent())
    prediction = PredictionStage(ag.create_prediction_agent())
    reconstruction = ReconstructionStage(ag.create_reconstruction_agent())
    rootcause = RootCauseStage(ag.create_rootcause_agent())
    remediation = RemediationStage(ag.create_remediation_agent())
    orchestrator = OrchestratorExecutor()

    return (
        WorkflowBuilder(start_executor=load_dispute)
        .add_edge(load_dispute, intake)
        .add_edge(intake, prediction)
        .add_edge(prediction, reconstruction)
        .add_edge(reconstruction, rootcause)
        .add_edge(rootcause, remediation)
        .add_edge(remediation, orchestrator)
        .build()
    )


# ---------------------------------------------------------------------------
# Rerun engine + conversational snapshot (used by the FastAPI chat / rerun
# endpoints in ``server.py``). Both operate directly on a suspended workflow's
# shared ``State`` object so a human can re-run one step with feedback or
# discuss the run while it waits at the approval gate.
# ---------------------------------------------------------------------------
STAGE_STATE_KEYS = {
    "intake": INTAKE_STATE_KEY,
    "prediction": PREDICTION_STATE_KEY,
    "reconstruction": RECONSTRUCTION_STATE_KEY,
    "root_cause": ROOTCAUSE_STATE_KEY,
    "remediation": REMEDIATION_STATE_KEY,
}


def _record_io_state(state: Any, stage: str, input_payload: Any, output: Any) -> None:
    """Persist a stage's input/output to the STAGE_IO map on a raw State object."""
    io = dict(state.get(STAGE_IO_STATE_KEY) or {})
    io[stage] = {
        "input": input_payload,
        "output": output.model_dump() if hasattr(output, "model_dump") else output,
    }
    state.set(STAGE_IO_STATE_KEY, io)


async def _rerun_one(state: Any, s: str, feedback: str = "") -> tuple[Any, Any]:
    """Run a single stage against the current state, persist result + I/O, commit.

    Reads upstream findings from ``state`` so downstream stages see refreshed
    inputs. Returns ``(result, input_payload)``.
    """
    c = state.get(CONTEXT_STATE_KEY)
    if c is None:
        raise ValueError("Run context is not loaded; cannot rerun a stage.")
    if s == "intake":
        result, payload = await run_intake(ag.create_intake_agent(), c, feedback)
    elif s == "prediction":
        result, payload = await run_prediction(ag.create_prediction_agent(), c, feedback)
    elif s == "reconstruction":
        result, payload = await run_reconstruction(ag.create_reconstruction_agent(), c, feedback)
    elif s == "root_cause":
        result, payload = await run_rootcause(
            ag.create_rootcause_agent(), c,
            state.get(PREDICTION_STATE_KEY), state.get(RECONSTRUCTION_STATE_KEY), feedback,
        )
    elif s == "remediation":
        result, payload = await run_remediation(
            ag.create_remediation_agent(), c,
            state.get(ROOTCAUSE_STATE_KEY), state.get(RECONSTRUCTION_STATE_KEY), feedback,
        )
    else:
        raise ValueError(f"Unknown stage '{s}'.")
    state.set(STAGE_STATE_KEYS[s], result)
    _record_io_state(state, s, payload, result)
    state.commit()  # so the next (downstream) stage reads the refreshed upstream
    return result, payload


def rebuild_approval_request(state: Any) -> None:
    """Rebuild the pending HITL approval payload from the current findings."""
    c = state.get(CONTEXT_STATE_KEY)
    remediation = state.get(REMEDIATION_STATE_KEY)
    if c is None or remediation is None:
        return
    req = build_approval_request(
        c["dispute"]["dispute_id"], c, remediation, state.get(ROOTCAUSE_STATE_KEY)
    )
    state.set(APPROVAL_REQUEST_STATE_KEY, dataclasses.asdict(req))
    state.commit()


async def rerun_stage(state: Any, stage: str, feedback: str = "") -> list[str]:
    """Re-run one specialist stage (with feedback) plus every downstream stage.

    Feedback is applied only to the targeted stage; downstream stages re-run with
    the refreshed upstream findings so the HITL proposal stays internally
    consistent. The refreshed results, stage I/O and rebuilt approval request are
    committed back to ``state``. Returns the ordered list of stages that ran.
    """
    if stage not in STAGE_SEQUENCE:
        raise ValueError(f"Unknown stage '{stage}'. Expected one of {STAGE_SEQUENCE}.")
    if state.get(CONTEXT_STATE_KEY) is None:
        raise ValueError("Run context is not loaded; cannot rerun a stage.")

    ran: list[str] = []
    for s in STAGE_SEQUENCE[STAGE_SEQUENCE.index(stage):]:
        await _rerun_one(state, s, feedback if s == stage else "")
        ran.append(s)

    rebuild_approval_request(state)
    return ran


def build_conversation_snapshot(state: Any) -> dict[str, Any]:
    """A JSON-serializable snapshot of the whole run for the conversation agent."""
    c = state.get(CONTEXT_STATE_KEY) or {}

    def dump(v: Any) -> Any:
        return v.model_dump() if hasattr(v, "model_dump") else v

    return {
        "dispute": c.get("dispute"),
        "trade": c.get("trade"),
        "confirmation": c.get("confirmation"),
        "derived": c.get("derived"),
        "findings": {
            "intake": dump(state.get(INTAKE_STATE_KEY)),
            "prediction": dump(state.get(PREDICTION_STATE_KEY)),
            "reconstruction": dump(state.get(RECONSTRUCTION_STATE_KEY)),
            "root_cause": dump(state.get(ROOTCAUSE_STATE_KEY)),
            "remediation": dump(state.get(REMEDIATION_STATE_KEY)),
        },
        "pending_approval": state.get(APPROVAL_REQUEST_STATE_KEY),
        "approval_record": dump(state.get(APPROVAL_STATE_KEY)),
    }


# ---------------------------------------------------------------------------
# CLI (for local testing without the web UI).
# ---------------------------------------------------------------------------
def _prompt_human(req: DisputeApprovalRequest) -> HumanDecision:
    print(f"\n=== HITL approval — dispute {req.dispute_id} ({req.category}) ===")
    print(f"  Proposed action: {req.proposed_action}  amount={req.proposed_amount}")
    print(f"  Approver role:   {req.approver_role}"
          f"{'  [DUAL APPROVAL]' if req.requires_dual_approval else ''}")
    print(f"  Summary:         {req.hitl_summary}")
    print(f"  Root cause:      {req.root_cause_narrative}")
    choice = input("\nApprove, deny, or modify? [approve/deny/modify]: ").strip().lower()
    if choice == "deny":
        return HumanDecision(action="deny")
    if choice == "modify":
        res = input("  New resolution (ADJUST/REBOOK/CLAIM/WRITE_OFF/BUY_IN/NO_ACTION): ").strip().upper()
        return HumanDecision(action="modify", final_resolution=res or None)
    return HumanDecision(action="approve")


async def main() -> None:
    workflow = build_workflow()
    if len(sys.argv) > 1:
        dispute_id = sys.argv[1]
    else:
        disputes = db.fetch_open_disputes(limit=1, credential=AzureCliCredential())
        if not disputes:
            print("No open disputes found.")
            return
        dispute_id = disputes[0]["dispute_id"]

    print(f"Processing dispute {dispute_id} ...")
    result = await workflow.run(dispute_id)
    while True:
        requests = result.get_request_info_events()
        if not requests:
            break
        responses = {e.request_id: _prompt_human(e.data) for e in requests}
        result = await workflow.run(responses=responses)

    for output in result.get_outputs():
        print(f"\n{output}")

    get = workflow._state.get  # noqa: SLF001
    for key, label in [
        (INTAKE_STATE_KEY, "Intake"), (PREDICTION_STATE_KEY, "Prediction"),
        (RECONSTRUCTION_STATE_KEY, "Reconstruction"), (ROOTCAUSE_STATE_KEY, "Root-Cause"),
        (REMEDIATION_STATE_KEY, "Remediation"), (APPROVAL_STATE_KEY, "Approval"),
    ]:
        val = get(key)
        if val is not None:
            print(f"\n--- {label} ---\n{_jsonify(val.model_dump())}")


if __name__ == "__main__":
    asyncio.run(main())
