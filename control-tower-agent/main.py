"""CMK Control Tower: reconcile uploaded confirmations against booked trades.

An expansion of the Microsoft Agent Framework reconcile example
(github.com/implodingduck/maf-reconcile-agent) that runs against a **real Azure
SQL ledger database** (``cmk-sqldb-ledger``).

A CSV of counterparty confirmations is uploaded. A reconciliation **agent**
reads each row, looks up the booked trade (``demo4_trades``) it references, and
decides whether the confirmation matches the firm's economics of record. Clean
confirmations finalize immediately; any the agent flags as broken enter the
human-in-the-loop approval flow, where a person approves / denies / modifies the
agent's proposed corrections.

The workflow:

    upload CSV -> look up booked trades in SQL (read-only)
               -> reconciliation agent decides matched vs broken + proposes cfm_* fixes
               -> switch:
                    all matched  -> finalize (emit confirmed report)
                    any broken   -> HUMAN approve / deny / modify
                                 -> apply decision (in memory), re-verify, emit report

Nothing is ever written back to the ledger tables. Corrections are applied to an
in-memory copy of the confirmations purely to produce the reconciled report.

Prerequisites:
- FOUNDRY_PROJECT_ENDPOINT: your Azure AI Foundry Agent Service (V2) project endpoint.
- FOUNDRY_MODEL: a deployed chat model name.
- Azure CLI login (``az login``) for AzureCliCredential (used for BOTH Foundry and SQL).
- ODBC Driver 18 for SQL Server installed locally (pyodbc).
- Optionally SQL_SERVER / SQL_DATABASE in the environment or .env.
"""

import asyncio
import csv
import io
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from agent_framework import (  # Core workflow primitives used to assemble the graph
    Agent,
    AgentExecutor,
    AgentExecutorRequest,
    AgentExecutorResponse,
    Case,
    Default,
    Executor,
    Message,
    WorkflowBuilder,
    WorkflowContext,
    executor,
    handler,
    response_handler,
)
from agent_framework.foundry import FoundryChatClient
from agent_framework.openai import OpenAIChatOptions
from azure.identity import AzureCliCredential
from dotenv import load_dotenv
from pydantic import BaseModel
from typing_extensions import Never

import db

load_dotenv()

# The confirmation fields the agent may propose corrections to.
CORRECTABLE_FIELDS = {"cfm_price", "cfm_qty", "cfm_gross"}

ITEMS_STATE_KEY = "items"  # {trade_id: PairedItem}
UNKNOWN_STATE_KEY = "unknown_trade_ids"  # list[str] of CSV rows with no booked trade
ASSESSMENTS_STATE_KEY = "assessments"  # {trade_id: ItemAssessment}


# ---------------------------------------------------------------------------
# Domain model.
# ---------------------------------------------------------------------------
@dataclass
class PairedItem:
    """An uploaded confirmation paired with the booked trade it references.

    The ``cfm_*`` fields come from the CSV and are the only values ever
    corrected. The trade fields are the firm's read-only economics of record.
    """

    trade_id: str
    source: str
    # Confirmation (mutable; corrected in memory after human approval).
    cfm_price: Decimal
    cfm_qty: int
    cfm_gross: Decimal
    # Booked trade of record.
    sec_id: str
    cusip: str
    side: str
    qty: int
    price: Decimal
    gross_amt: Decimal
    ccy: str


class FieldSuggestion(BaseModel):
    """One proposed change to a single ``cfm_*`` field of a confirmation."""

    trade_id: str
    field: str
    current_value: str
    suggested_value: str
    reason: str


class ItemAssessment(BaseModel):
    """The agent's verdict for one confirmation row."""

    trade_id: str
    matched: bool
    summary: str
    suggestions: list[FieldSuggestion] = []


class ReconAssessment(BaseModel):
    """Structured output returned by the reconciliation agent."""

    items: list[ItemAssessment]


class HumanDecision(BaseModel):
    """The human's verdict on the proposed corrections, supplied via run(responses=...)."""

    action: Literal["approve", "deny", "modify"]
    modified_suggestions: list[FieldSuggestion] = []


@dataclass
class AssessmentOutcome:
    """Routed payload: the agent's assessments plus the derived break summary."""

    matched_ids: list[str] = field(default_factory=list)
    broken_ids: list[str] = field(default_factory=list)
    suggestions: list[FieldSuggestion] = field(default_factory=list)

    @property
    def has_breaks(self) -> bool:
        return bool(self.broken_ids)


@dataclass
class ApprovalRequest:
    """Payload surfaced to the human for approve / deny / modify decisions."""

    suggestions: list[FieldSuggestion]
    summaries: list[dict[str, str]]


# ---------------------------------------------------------------------------
# Deterministic verification (used to confirm the final, human-approved state).
# ---------------------------------------------------------------------------
def _q2(value: Decimal) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"))


def _q4(value: Decimal) -> Decimal:
    return Decimal(value).quantize(Decimal("0.0001"))


def item_issues(item: PairedItem) -> list[str]:
    """Deterministic reconciliation issues for a confirmation vs its booked trade."""
    issues: list[str] = []
    if item.cfm_qty != item.qty:
        issues.append(f"cfm_qty {item.cfm_qty} != booked qty {item.qty}")
    if _q4(item.cfm_price) != _q4(item.price):
        issues.append(f"cfm_price {_q4(item.cfm_price)} != booked price {_q4(item.price)}")
    if _q2(item.cfm_gross) != _q2(item.gross_amt):
        issues.append(f"cfm_gross {_q2(item.cfm_gross)} != booked gross {_q2(item.gross_amt)}")
    expected = _q2(Decimal(item.cfm_qty) * _q4(item.cfm_price))
    if _q2(item.cfm_gross) != expected:
        issues.append(f"cfm_gross {_q2(item.cfm_gross)} != cfm_qty*cfm_price ({expected})")
    return issues


def reconciles(item: PairedItem) -> bool:
    return not item_issues(item)


def _items_to_csv(items: list[PairedItem]) -> str:
    fieldnames = [
        "trade_id", "source", "cusip", "side", "qty", "price", "gross_amt",
        "cfm_qty", "cfm_price", "cfm_gross", "ccy",
    ]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    for it in items:
        writer.writerow({k: getattr(it, k) for k in fieldnames})
    return buffer.getvalue().strip()


def _confirmation_public(item: PairedItem) -> dict[str, Any]:
    return {
        "trade_id": item.trade_id,
        "source": item.source,
        "cfm_price": str(item.cfm_price),
        "cfm_qty": item.cfm_qty,
        "cfm_gross": str(item.cfm_gross),
    }


def _trade_public(item: PairedItem) -> dict[str, Any]:
    return {
        "trade_id": item.trade_id,
        "sec_id": item.sec_id,
        "cusip": item.cusip,
        "side": item.side,
        "qty": item.qty,
        "price": str(item.price),
        "gross_amt": str(item.gross_amt),
        "ccy": item.ccy,
    }


def _parse_confirmations(csv_text: str) -> list[dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(csv_text))
    rows: list[dict[str, Any]] = []
    for row in reader:
        rows.append(
            {
                "trade_id": (row.get("trade_id") or "").strip(),
                "source": (row.get("source") or "").strip(),
                "cfm_price": (row.get("cfm_price") or "").strip(),
                "cfm_qty": (row.get("cfm_qty") or "").strip(),
                "cfm_gross": (row.get("cfm_gross") or "").strip(),
            }
        )
    return [r for r in rows if r["trade_id"]]


# ---------------------------------------------------------------------------
# Workflow executors.
# ---------------------------------------------------------------------------
@executor(id="ingest_confirmations")
async def ingest_confirmations(csv_text: str, ctx: WorkflowContext[AgentExecutorRequest]) -> None:
    # Parse the uploaded confirmations, look up each referenced booked trade, and
    # hand the paired data to the reconciliation agent.
    rows = _parse_confirmations(csv_text)
    credential = AzureCliCredential()
    trades = db.fetch_trades_by_ids([r["trade_id"] for r in rows], credential)

    items: dict[str, PairedItem] = {}
    unknown: list[str] = []
    for r in rows:
        trade = trades.get(r["trade_id"])
        if trade is None:
            unknown.append(r["trade_id"])
            continue
        try:
            cfm_price = Decimal(r["cfm_price"])
            cfm_qty = int(r["cfm_qty"])
            cfm_gross = Decimal(r["cfm_gross"])
        except (InvalidOperation, ValueError):
            unknown.append(r["trade_id"])
            continue
        items[trade.trade_id] = PairedItem(
            trade_id=trade.trade_id,
            source=r["source"] or "CSV",
            cfm_price=cfm_price,
            cfm_qty=cfm_qty,
            cfm_gross=cfm_gross,
            sec_id=trade.sec_id,
            cusip=trade.cusip,
            side=trade.side,
            qty=trade.qty,
            price=trade.price,
            gross_amt=trade.gross_amt,
            ccy=trade.ccy,
        )

    ctx.set_state(ITEMS_STATE_KEY, items)
    ctx.set_state(UNKNOWN_STATE_KEY, unknown)

    payload = [
        {"confirmation": _confirmation_public(it), "booked_trade": _trade_public(it)}
        for it in items.values()
    ]
    prompt = (
        "You are reconciling uploaded counterparty confirmations against the firm's booked trades "
        "(the economics of record, read from the ledger database). For EACH confirmation below, decide "
        "whether it matches its booked trade. A confirmation matches only when cfm_qty == booked qty, "
        "cfm_price == booked price, cfm_gross == booked gross_amt, AND cfm_gross == cfm_qty * cfm_price. "
        "For any confirmation that does not match, propose the minimal corrections to the CONFIRMATION "
        "fields (cfm_price, cfm_qty, cfm_gross) that would make it reconcile, based on the booked trade. "
        "Return JSON matching the schema: an 'items' array with one entry per confirmation, each having "
        "'trade_id', 'matched' (bool), 'summary' (short human-readable), and 'suggestions' (array of "
        "{trade_id, field, current_value, suggested_value, reason}; empty when matched). Never change the "
        "booked trade; only correct confirmation fields.\n\n"
        f"Confirmations to reconcile:\n{json.dumps(payload, indent=2)}"
    )
    await ctx.send_message(
        AgentExecutorRequest(messages=[Message("user", contents=[prompt])], should_respond=True)
    )


@executor(id="route_assessment")
async def route_assessment(response: AgentExecutorResponse, ctx: WorkflowContext[AssessmentOutcome]) -> None:
    # Parse the agent's per-confirmation verdicts and derive the break summary.
    parsed = ReconAssessment.model_validate_json(response.agent_response.text)
    items: dict[str, PairedItem] = ctx.get_state(ITEMS_STATE_KEY)

    by_id: dict[str, ItemAssessment] = {a.trade_id: a for a in parsed.items}
    # Guard against omissions: fall back to a deterministic verdict for any
    # item the agent did not assess.
    for tid, item in items.items():
        if tid not in by_id:
            issues = item_issues(item)
            by_id[tid] = ItemAssessment(
                trade_id=tid,
                matched=not issues,
                summary="Reconciles." if not issues else "; ".join(issues),
                suggestions=[],
            )

    ctx.set_state(ASSESSMENTS_STATE_KEY, by_id)

    outcome = AssessmentOutcome()
    for tid in items:
        assessment = by_id[tid]
        if assessment.matched:
            outcome.matched_ids.append(tid)
        else:
            outcome.broken_ids.append(tid)
            outcome.suggestions.extend(assessment.suggestions)
    await ctx.send_message(outcome)


@executor(id="finalize_matched")
async def finalize_matched(outcome: AssessmentOutcome, ctx: WorkflowContext[Never, str]) -> None:
    # Clean path: the agent found no breaks.
    if outcome.has_breaks:
        raise RuntimeError("This executor should only handle fully matched outcomes.")
    items: dict[str, PairedItem] = ctx.get_state(ITEMS_STATE_KEY)
    unknown: list[str] = ctx.get_state(UNKNOWN_STATE_KEY) or []
    csv_out = _items_to_csv(list(items.values()))
    summary = f"All {len(outcome.matched_ids)} uploaded confirmation(s) match their booked trades."
    if unknown:
        summary += f" Skipped {len(unknown)} row(s) with no booked trade: {', '.join(unknown)}."
    await ctx.yield_output(f"{summary}\n\n{csv_out}")


class HumanApprovalExecutor(Executor):
    """Human-in-the-loop gate. Suspends for approval, then applies the decision."""

    def __init__(self) -> None:
        super().__init__(id="human_approval")

    @handler
    async def request_approval(self, outcome: AssessmentOutcome, ctx: WorkflowContext) -> None:
        if not outcome.has_breaks:
            raise RuntimeError("This executor should only handle outcomes with breaks.")
        assessments: dict[str, ItemAssessment] = ctx.get_state(ASSESSMENTS_STATE_KEY)
        summaries = [
            {"trade_id": tid, "summary": assessments[tid].summary}
            for tid in outcome.broken_ids
            if tid in assessments
        ]
        await ctx.request_info(
            ApprovalRequest(suggestions=outcome.suggestions, summaries=summaries),
            response_type=HumanDecision,
        )

    @response_handler
    async def apply_decision(
        self,
        original_request: ApprovalRequest,
        decision: HumanDecision,
        ctx: WorkflowContext[Never, str],
    ) -> None:
        items: dict[str, PairedItem] = ctx.get_state(ITEMS_STATE_KEY)
        unknown: list[str] = ctx.get_state(UNKNOWN_STATE_KEY) or []

        if decision.action == "deny":
            applied: list[FieldSuggestion] = []
        elif decision.action == "modify":
            applied = decision.modified_suggestions
        else:  # approve
            applied = original_request.suggestions

        for suggestion in applied:
            item = items.get(suggestion.trade_id)
            if item is None or suggestion.field not in CORRECTABLE_FIELDS:
                continue
            current = getattr(item, suggestion.field)
            try:
                if isinstance(current, int):
                    new_value: Any = int(suggestion.suggested_value)
                elif isinstance(current, Decimal):
                    new_value = Decimal(suggestion.suggested_value)
                else:
                    new_value = suggestion.suggested_value
            except (ValueError, InvalidOperation):
                new_value = suggestion.suggested_value
            setattr(item, suggestion.field, new_value)

        still_broken = [tid for tid, item in items.items() if not reconciles(item)]
        csv_out = _items_to_csv(list(items.values()))
        summary = (
            f"Human decision: {decision.action}. Applied {len(applied)} correction(s). "
            f"{len(items) - len(still_broken)}/{len(items)} confirmation(s) now reconcile."
        )
        if still_broken:
            summary += f" Still unresolved: {', '.join(still_broken)}."
        if unknown:
            summary += f" Skipped {len(unknown)} row(s) with no booked trade: {', '.join(unknown)}."
        await ctx.yield_output(f"{summary}\n\n{csv_out}")


# ---------------------------------------------------------------------------
# Agent.
# ---------------------------------------------------------------------------
def create_reconcile_agent() -> Agent:
    """Create the agent that reconciles uploaded confirmations against booked trades."""
    return Agent(
        client=FoundryChatClient(
            project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
            model=os.environ["FOUNDRY_MODEL"],
            credential=AzureCliCredential(),
        ),
        instructions=(
            "You are a capital-markets trade reconciliation assistant. You are given uploaded counterparty "
            "confirmations paired with the firm's booked trades (economics of record). For each confirmation "
            "decide whether it matches its booked trade: cfm_qty == qty, cfm_price == price, cfm_gross == "
            "gross_amt, and cfm_gross == cfm_qty * cfm_price. When a confirmation does not match, propose the "
            "minimal corrections to the confirmation fields (cfm_price, cfm_qty, cfm_gross) that make it "
            "reconcile with the booked trade. Always return JSON matching the required schema with one item "
            "per confirmation. Only correct confirmation fields; never change the booked trade."
        ),
        name="reconcile_agent",
        default_options=OpenAIChatOptions[Any](response_format=ReconAssessment),
    )


# ---------------------------------------------------------------------------
# Workflow assembly (shared by the CLI and the FastAPI server).
# ---------------------------------------------------------------------------
def build_workflow():
    """Assemble the reconciliation workflow graph.

    ingest -> reconcile agent -> route -> switch (matched vs broken)
      matched -> finalize_matched (yields the confirmed report)
      broken  -> human approval -> yields reconciled report
    """
    reconcile_agent = AgentExecutor(create_reconcile_agent())
    human_approval = HumanApprovalExecutor()
    return (
        WorkflowBuilder(start_executor=ingest_confirmations)
        .add_edge(ingest_confirmations, reconcile_agent)
        .add_edge(reconcile_agent, route_assessment)
        .add_switch_case_edge_group(
            route_assessment,
            [
                Case(
                    condition=lambda o: isinstance(o, AssessmentOutcome) and not o.has_breaks,
                    target=finalize_matched,
                ),
                Default(target=human_approval),
            ],
        )
        .build()
    )


# ---------------------------------------------------------------------------
# Console helper: turn a pending ApprovalRequest into a HumanDecision.
# ---------------------------------------------------------------------------
def prompt_human(request: ApprovalRequest) -> HumanDecision:
    print("\n=== Human review required: agent flagged confirmations ===")
    for s in request.summaries:
        print(f"  - [{s['trade_id']}] {s['summary']}")
    if not request.suggestions:
        print("The agent could not propose any corrections. You can only deny.")
        return HumanDecision(action="deny")

    print("\nProposed confirmation corrections:")
    for i, s in enumerate(request.suggestions, start=1):
        print(f"  {i}. [{s.trade_id}] {s.field}: '{s.current_value}' -> '{s.suggested_value}'  ({s.reason})")

    choice = input("\nApprove, deny, or modify these changes? [approve/deny/modify]: ").strip().lower()
    if choice == "deny":
        return HumanDecision(action="deny")
    if choice == "modify":
        modified: list[FieldSuggestion] = []
        print("Enter a new suggested value for each change (blank = keep, '-' = drop this change):")
        for s in request.suggestions:
            entry = input(f"  [{s.trade_id}] {s.field} (current suggestion '{s.suggested_value}'): ").strip()
            if entry == "-":
                continue
            modified.append(s if entry == "" else s.model_copy(update={"suggested_value": entry}))
        return HumanDecision(action="modify", modified_suggestions=modified)
    return HumanDecision(action="approve")


async def main() -> None:
    """Build and run the reconciliation workflow from a confirmations CSV."""
    workflow = build_workflow()

    csv_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.realpath(__file__)), "samples", "confirmations.csv"
    )
    if not os.path.exists(csv_path):
        print(f"CSV not found: {csv_path}")
        return
    with open(csv_path, encoding="utf-8") as f:  # noqa: ASYNC230
        csv_text = f.read()

    print(f"Reconciling confirmations from {csv_path} ...")
    result = await workflow.run(csv_text)

    while True:
        requests = result.get_request_info_events()
        if not requests:
            break
        responses: dict[str, HumanDecision] = {}
        for event in requests:
            responses[event.request_id] = prompt_human(event.data)
        result = await workflow.run(responses=responses)

    for output in result.get_outputs():
        print(f"\nWorkflow output:\n{output}")


if __name__ == "__main__":
    asyncio.run(main())
