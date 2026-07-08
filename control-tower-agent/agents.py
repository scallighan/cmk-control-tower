"""The five specialist dispute agents (Azure AI Foundry via Microsoft Agent Framework).

Each agent mirrors one phase of the Demo 4 Counterparty Dispute workflow (see
``agents/prompts/*.md``) and returns a typed ``pydantic`` result. They are
invoked directly inside the orchestrator workflow executors (``main.py``) via
``agent.run``; the full dispute context is loaded from the SQL ledger and passed
in the prompt.

When ``MCP_SERVER_URL`` is set, each agent is additionally given the relevant
read-only ledger tools from the CMK MCP server (``mcp_server.py``) so it can
fetch or verify facts on demand (e.g. Reconstruction calls ``verify_acl_proof``).
The Microsoft Agent Framework auto-connects the MCP tool at run time.

    Intake Agent          -> IntakeResult        (classify, severity, route)
    Prediction Agent      -> PredictionResult     (pre-cutoff settlement risk)
    Reconstruction Agent  -> ReconstructionResult (6-artifact evidence pack)
    Root-Cause Agent      -> RootCauseResult      (primary break type)
    Remediation Agent     -> RemediationResult    (HITL-gated proposal)
"""

from __future__ import annotations

import os
from typing import Any, Literal

from agent_framework import Agent, MCPStreamableHTTPTool
from agent_framework.foundry import FoundryChatClient
from agent_framework.openai import OpenAIChatOptions
from azure.identity import DefaultAzureCredential
from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Structured agent outputs (typed; no free-form dicts so strict structured
# outputs accept the schema).
# ---------------------------------------------------------------------------
class IntakeEntities(BaseModel):
    dispute_id: str
    trade_id: str
    cp_buy_id: str = ""
    cp_sell_id: str = ""
    category: str
    notional_usd: float = 0.0
    opened_ts_utc: str = ""


class IntakeResult(BaseModel):
    dispute_id: str
    classification: Literal[
        "economic", "ssi", "affirmation", "fail", "cns_claim", "trs_reset", "corp_action", "fee"
    ]
    severity: Literal["HIGH", "MEDIUM", "LOW"]
    entities: IntakeEntities
    evidence_completeness_pct: float = 0.0
    recommended_agents: list[str] = []
    routing_notes: str = ""


class PredictionSignals(BaseModel):
    timing_breach_flag: bool = False
    affirm_status: str = ""
    ssi_mismatch_prob: float = 0.0
    cp_fail_propensity: float = 0.0
    liquidity_stress_score: float = 0.0


class PredictionResult(BaseModel):
    trade_id: str
    pre_cutoff_risk_score: float  # 0.0 - 2.0
    primary_risk_driver: Literal["timing", "ssi", "counterparty", "liquidity", "economic"]
    time_sensitivity: Literal["URGENT", "MONITOR", "STANDARD"]
    signal_breakdown: PredictionSignals
    recommended_actions: list[str] = []
    confidence: float = 0.0


class EvidenceArtifacts(BaseModel):
    trade_msg: bool = False
    alloc_msg: bool = False
    confirm_msg: bool = False
    affirm_msg: bool = False
    ssi_msg: bool = False
    settle_msg: bool = False


class ReconstructionResult(BaseModel):
    dispute_id: str
    evidence_completeness_pct: float = 0.0
    artifacts: EvidenceArtifacts
    gaps: list[str] = []
    ledger_verified: bool = False
    proof_integrity_score: float = 0.0
    acl_lag_minutes: int = 0
    ssi_freshness_days: int = 0
    reconstruction_notes: str = ""


class BreakDetails(BaseModel):
    broken_field: str = ""
    break_amount: float = 0.0
    counterparty_pattern: Literal["repeat_offender", "first_occurrence", "high_risk_cp", "unknown"] = "unknown"
    contributing_factors: list[str] = []


class RootCauseResult(BaseModel):
    dispute_id: str
    primary_break_type: Literal[
        "economic", "ssi", "affirmation", "fail", "cns_claim", "trs_reset", "corp_action", "fee"
    ]
    confidence: float = 0.0
    break_details: BreakDetails
    recommended_resolution: Literal[
        "ADJUST", "REBOOK", "CLAIM", "WRITE_OFF", "BUY_IN", "NO_ACTION"
    ]
    requires_hitl: bool = True
    root_cause_narrative: str = ""


class DraftCommunication(BaseModel):
    to: str = ""
    subject: str = ""
    body: str = ""


class RemediationResult(BaseModel):
    dispute_id: str
    proposed_action: Literal[
        "ADJUST", "REBOOK", "CLAIM", "WRITE_OFF", "BUY_IN", "SSI_UPDATE", "REAFFIRM", "NO_ACTION"
    ]
    proposed_amount: float = 0.0
    regulatory_cost_if_unresolved: float = 0.0
    urgency_hours: float = 0.0
    draft_communication: DraftCommunication
    hitl_summary: str = ""
    hitl_required: bool = True
    approval_deadline_utc: str = ""
    approver_role: Literal["ops_analyst", "senior_ops", "compliance", "dual_approval"] = "ops_analyst"


# ---------------------------------------------------------------------------
# Agent factories.
# ---------------------------------------------------------------------------
def _client() -> FoundryChatClient:
    return FoundryChatClient(
        project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
        model=os.environ["FOUNDRY_MODEL"],
        credential=DefaultAzureCredential(),
    )


# The MCP tool server (mcp_server.py). When unset, agents run tool-less with the
# full context supplied inline (the original, always-available behaviour).
MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "").strip()


def _mcp_tool(name: str, allowed: list[str]) -> MCPStreamableHTTPTool | None:
    """Build a ledger MCP tool exposing only ``allowed`` tool names, or ``None``.

    Returns ``None`` when ``MCP_SERVER_URL`` is not configured so the pipeline
    still runs (tool-less) without the MCP server. MAF connects the tool lazily
    on the first ``agent.run`` and closes it when the agent is disposed.
    """
    if not MCP_SERVER_URL:
        return None
    return MCPStreamableHTTPTool(
        name=name,
        url=MCP_SERVER_URL,
        allowed_tools=allowed,
        load_prompts=False,
    )


def _agent(
    name: str,
    instructions: str,
    schema: type[BaseModel],
    tools: list[str] | None = None,
) -> Agent:
    mcp = _mcp_tool(f"ledger_{name}", tools) if tools else None
    return Agent(
        client=_client(),
        instructions=instructions,
        name=name,
        tools=[mcp] if mcp is not None else None,
        default_options=OpenAIChatOptions[Any](response_format=schema),
    )


_NO_TOOLS = (
    "The complete dispute context (dispute record, trade lifecycle, evidence pack, "
    "counterparty data, and signals) is provided inline in the user message as JSON. "
    "Do not ask for tools or more data; reason only over what you are given. "
)

_WITH_TOOLS = (
    "The dispute context is provided inline in the user message as JSON. You may also "
    "call the read-only ledger tools attached to you to fetch or independently verify "
    "additional detail (they hit the live SQL ledger). Prefer the inline context for "
    "speed; use tools when you need something not present or want to confirm the ledger "
    "proof. Never invent tool results. "
)


def _guidance(tools: list[str] | None) -> str:
    """The data-access note injected into each agent's instructions."""
    return _WITH_TOOLS if (tools and MCP_SERVER_URL) else _NO_TOOLS


def create_intake_agent() -> Agent:
    tools = ["get_dispute_record", "query_dispute_data"]
    return _agent(
        "intake_agent",
        (
            "You are the Intake Agent for a US cash-equity prime brokerage's counterparty dispute "
            "resolution system. You are the first responder when a dispute is opened. " + _guidance(tools) +
            "Classify the dispute into exactly one of: economic | ssi | affirmation | fail | cns_claim | "
            "trs_reset | corp_action | fee. Extract the key entities (dispute_id, trade_id, cp_buy_id, "
            "cp_sell_id, category, notional_usd, opened_ts_utc). Assess severity: HIGH if notional > $1M "
            "or there is a timing breach; MEDIUM if evidence is incomplete; LOW if all evidence is present. "
            "Recommend which specialist agents should run (subset of prediction, reconstruction, "
            "root_cause, remediation) and give short routing_notes. Do not propose economic adjustments or "
            "approve anything. Return JSON matching the schema."
        ),
        IntakeResult,
        tools=tools,
    )


def create_prediction_agent() -> Agent:
    tools = ["get_analytic_signals", "get_trade_lifecycle"]
    return _agent(
        "prediction_agent",
        (
            "You are the Prediction Agent. You assess pre-cutoff settlement risk for the disputed trade. "
            + _guidance(tools) +
            "Compute a composite pre_cutoff_risk_score from 0.0 to 2.0 (1.0 ~= affirmed-late + confirm "
            "mismatch + stress day). Identify the primary_risk_driver (timing | ssi | counterparty | "
            "liquidity | economic). Set time_sensitivity: URGENT (<2h to cutoff or already breached), "
            "MONITOR (2-24h), STANDARD (>24h). Fill signal_breakdown from the data: timing_breach_flag "
            "(affirmation MISSING or AFFIRMED_LATE), ssi_mismatch_prob (higher when the SSI snapshot "
            "version is behind the current SSI version), cp_fail_propensity (from the counterparty's "
            "dispute/fail history), liquidity_stress_score (higher on stress days / low ADV). Recommend "
            "specific pre-emptive actions. You predict and recommend only. Return JSON matching the schema."
        ),
        PredictionResult,
        tools=tools,
    )


def create_reconstruction_agent() -> Agent:
    tools = ["get_evidence_pack", "verify_acl_proof", "get_dispute_record", "get_trade_lifecycle"]
    return _agent(
        "reconstruction_agent",
        (
            "You are the Reconstruction Agent. You assemble the 6-artifact evidence pack and verify the "
            "ledger/ACL proof chain. " + _guidance(tools) +
            "The 6 required artifacts are: trade_msg (execution record), alloc_msg (allocation), "
            "confirm_msg (confirmation), affirm_msg (affirmation), ssi_msg (SSI), settle_msg (settlement "
            "status). Mark each present/absent based on whether that record exists in the context. Report "
            "evidence_completeness_pct (use the evidence pack's completeness if present), list the gaps for "
            "missing artifacts, and surface ledger_verified / proof_integrity_score / acl_lag_minutes from "
            "the ACL receipt. When tools are available, call verify_acl_proof to independently confirm the "
            "ledger proof chain before reporting ledger_verified. Compute ssi_freshness_days as the gap "
            "between the SSI snapshot and the current SSI (flag if the snapshot version is behind). When "
            "an artifact is missing, use get_trade_lifecycle to pinpoint where the trade -> allocation -> "
            "confirmation -> affirmation chain breaks. Retrieve and verify only; do not modify records. "
            "Return JSON matching the schema."
        ),
        ReconstructionResult,
        tools=tools,
    )


def create_rootcause_agent() -> Agent:
    tools = ["get_trade_lifecycle", "get_analytic_signals"]
    return _agent(
        "rootcause_agent",
        (
            "You are the Root-Cause Agent. You classify the primary break type driving the dispute. "
            + _guidance(tools) +
            "Classify primary_break_type into exactly ONE of: economic (price/qty mismatch between trade "
            "and confirmation), ssi (settlement-instruction mismatch), affirmation (late/missing affirm), "
            "fail (CNS settlement failure), cns_claim, trs_reset, corp_action, fee. Populate break_details "
            "(broken_field, break_amount, counterparty_pattern, contributing_factors) from the confirmation "
            "vs booked trade and the counterparty history. Recommend a resolution (ADJUST | REBOOK | CLAIM "
            "| WRITE_OFF | BUY_IN | NO_ACTION). Economic adjustments ALWAYS require HITL, so set "
            "requires_hitl=true for economic breaks. Provide a clear root_cause_narrative for the human "
            "reviewer. Do not execute anything. Return JSON matching the schema."
        ),
        RootCauseResult,
        tools=tools,
    )


def create_remediation_agent() -> Agent:
    tools = ["get_dispute_record", "get_analytic_signals"]
    return _agent(
        "remediation_agent",
        (
            "You are the Remediation Agent. You draft a remediation proposal for human approval; you NEVER "
            "execute adjustments. " + _guidance(tools) +
            "Based on the root-cause classification, choose proposed_action (ADJUST | REBOOK | CLAIM | "
            "WRITE_OFF | BUY_IN | SSI_UPDATE | REAFFIRM | NO_ACTION) and the appropriate remediation path: "
            "economic -> price/qty adjustment + chaser to counterparty ops AND an internal ops alert; ssi -> "
            "SSI_UPDATE request; affirmation -> REAFFIRM request; fail -> BUY_IN notice; cns_claim -> CNS "
            "claim counter-proposal. Set proposed_amount to the economic break amount when relevant, estimate "
            "regulatory_cost_if_unresolved and urgency_hours, set approval_deadline_utc from the cutoff / "
            "urgency window (ISO-8601 UTC), and draft a concise counterparty communication (to, subject, "
            "body). hitl_required is ALWAYS true. NEVER propose WRITE_OFF for a dispute over $50,000 without "
            "explicit escalation — for those, escalate (approver_role=senior_ops or compliance) and say so in "
            "hitl_summary rather than writing off silently. If the counterparty's cp_repeat_score exceeds "
            "0.7, add an internal escalation note in hitl_summary. Use approver_role=dual_approval for "
            "economic adjustments over $100,000, compliance if the ledger is unverified, else ops_analyst. "
            "Write a one-paragraph hitl_summary for the approver. Return JSON matching the schema."
        ),
        RemediationResult,
        tools=tools,
    )


# ---------------------------------------------------------------------------
# Conversational review assistant.
#
# Unlike the five specialist agents, this agent returns free-form text (no
# response_format) and holds a multi-turn conversation with the human reviewer
# about a single dispute that is paused at the approval gate. The full run
# snapshot (every agent's findings + the pending proposal) is supplied inline;
# the read-only ledger tools let it verify or fetch extra detail on demand.
# ---------------------------------------------------------------------------
CONVERSATION_TOOLS = [
    "get_dispute_record",
    "get_analytic_signals",
    "get_trade_lifecycle",
    "get_evidence_pack",
    "verify_acl_proof",
    "query_dispute_data",
]


def create_conversation_agent(rerun_tool: Any = None) -> Agent:
    """A plain-text analyst assistant for discussing a dispute pending approval.

    ``rerun_tool`` is an optional per-run ``FunctionTool`` (built by the server and
    bound to the specific run's workflow state) that lets the assistant re-run a
    pipeline step with the reviewer's feedback on their behalf.
    """
    mcp = _mcp_tool("ledger_dispute_assistant", CONVERSATION_TOOLS)
    tools: list[Any] = []
    if mcp is not None:
        tools.append(mcp)
    if rerun_tool is not None:
        tools.append(rerun_tool)
    instructions = (
        "You are the CMK Control Tower review assistant. You help a human operations "
        "reviewer understand a single counterparty dispute that is paused at the "
        "human-in-the-loop approval gate, so they can make an informed approve / deny / "
        "modify decision.\n\n"
        "You are given a JSON snapshot of the whole agent run: the dispute record and "
        "trade, and each specialist agent's structured findings — Intake (classification, "
        "severity, routing), Prediction (pre-cutoff settlement risk + signals), "
        "Reconstruction (6-artifact evidence pack + ledger/ACL proof), Root-Cause "
        "(primary break type + recommended resolution) and Remediation (the proposed "
        "action, amount, draft communication and the pending approval request).\n\n"
        + (_WITH_TOOLS if MCP_SERVER_URL else _NO_TOOLS) +
        "\n\nAnswer the reviewer's questions specifically and concisely. Explain what each "
        "workflow step did and WHY it reached its conclusion, citing concrete numbers from "
        "the findings (risk scores, break amounts, completeness %, timing breaches, "
        "counterparty history). Surface risks, gaps and anything that warrants caution.\n\n"
        "When the reviewer disagrees with a step or wants to test a different assumption, "
        "re-run that step for them with the `rerun_step` tool: pass the step name "
        "(intake, prediction, reconstruction, root_cause, or remediation) and a clear "
        "`feedback` string that captures the reviewer's intent in your own words. Calling "
        "the tool kicks off the rerun live in the main pipeline view — the affected step "
        "and every downstream step visibly re-process and are highlighted as revised. If "
        "the reviewer's intent is clear, just call it (don't ask permission); if it's "
        "ambiguous which step or what change they mean, ask one brief clarifying question "
        "first. After you call it, tell the reviewer to watch the pipeline update and that "
        "you'll discuss the revised result once it lands. Only re-run when the reviewer "
        "clearly wants a change — never to approve or execute anything. You never "
        "approve, deny, or execute the remediation yourself; the human decides at the gate "
        "and the orchestrator writes the ledger.\n\n"
        "Keep answers focused; use short paragraphs or bullet points."
    )
    return Agent(
        client=_client(),
        instructions=instructions,
        name="dispute_assistant",
        tools=tools or None,
    )
