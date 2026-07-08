"""CMK Control Tower — MCP tool server for the dispute-resolution agents.

Exposes the tool calls the specialist agents were designed to use (see
``agents/tools/tool_schemas.json``) as a real
`Model Context Protocol <https://modelcontextprotocol.io>`_ server, backed by the
live Azure SQL **ledger** database ``cmk-sqldb-ledger`` (``demo4_*`` tables) via
``db.py``. Everything that touches the ledger is strictly read-only; the
approval / ledger-write tools are *simulated* (the real human-in-the-loop gate
and ledger receipts are produced deterministically by the orchestrator workflow
in ``main.py``, not by an LLM tool call).

Tools
-----
Read-only (ledger-backed), attached to the specialist agents:
  * ``get_dispute_record``   — full dispute record + counterparties + evidence pack
  * ``get_analytic_signals`` — derived economics / SSI / timing / risk signals
  * ``get_trade_lifecycle``  — trade -> allocation -> confirmation -> affirmation + break analysis
  * ``get_evidence_pack``    — 6-artifact presence, completeness, ACL proof status
  * ``verify_acl_proof``     — EXEC dbo.usp_VerifyDisputeL1 (SHA-256 tamper check)
  * ``query_dispute_data``   — lightweight NL-ish analytics over the ledger

Simulated (not attached to specialist agents; the workflow owns these):
  * ``submit_for_hitl_approval``
  * ``write_ledger_decision``

Run it as a Streamable-HTTP server the agents connect to::

    python mcp_server.py            # serves http://127.0.0.1:8001/mcp

Configuration (env):
  * ``MCP_HOST``  (default ``127.0.0.1``)
  * ``MCP_PORT``  (default ``8001``)
  * plus the usual ``SQL_SERVER`` / ``SQL_DATABASE`` + ``az login`` used by ``db.py``.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv
from fastmcp import FastMCP

import db
import signals

load_dotenv()

mcp = FastMCP(
    name="cmk-control-tower",
    instructions=(
        "Read-only tools over the CMK counterparty-dispute ledger (cmk-sqldb-ledger). "
        "Use get_dispute_record / get_analytic_signals / get_trade_lifecycle / "
        "get_evidence_pack to fetch a dispute's facts, and verify_acl_proof to check the "
        "SHA-256 ledger proof chain. The submit_for_hitl_approval and write_ledger_decision "
        "tools are simulated — never a substitute for the orchestrator's human approval gate."
    ),
)


def _credential() -> DefaultAzureCredential:
    return DefaultAzureCredential()


def _context(dispute_id: str) -> dict[str, Any] | None:
    return db.fetch_dispute_context(dispute_id, _credential())


def _not_found(dispute_id: str) -> dict[str, Any]:
    return {"error": "not_found", "message": f"Dispute {dispute_id} not found in the ledger."}


def _rows(cursor: Any) -> list[dict[str, Any]]:
    """All remaining rows as JSON-friendly dicts."""
    columns = [d[0] for d in cursor.description]
    return [{c: db._jsonable(v) for c, v in zip(columns, row)} for row in cursor.fetchall()]


# ---------------------------------------------------------------------------
# Read-only, ledger-backed tools.
# ---------------------------------------------------------------------------
@mcp.tool(
    description=(
        "Retrieve the full dispute record from the SQL ledger: the dispute row, both "
        "counterparties, the filer, evidence-pack summary, communications rollup, a light "
        "counterparty risk profile, and any prior agent findings. Read-only."
    )
)
def get_dispute_record(dispute_id: str) -> dict[str, Any]:
    """Full dispute record for one ``dispute_id`` (e.g. ``DSP-0000001``)."""
    ctx = _context(dispute_id)
    if ctx is None:
        return _not_found(dispute_id)
    return {
        "dispute": ctx["dispute"],
        "counterparty_buy": ctx["counterparty_buy"],
        "counterparty_sell": ctx["counterparty_sell"],
        "filer_cp": ctx["filer_cp"],
        "evidence_pack": ctx["evidence_pack"],
        "communications": ctx["communications"],
        "cp_profile": ctx["cp_profile"],
        "prior_findings": ctx["prior_findings"],
    }


@mcp.tool(
    description=(
        "Retrieve derived analytic signals for a dispute: confirmation-vs-booked economics "
        "(price/qty/gross breaks), SSI freshness (snapshot vs current version), affirmation "
        "timing breach, settlement status, and counterparty dispute/fail profile. Read-only."
    )
)
def get_analytic_signals(dispute_id: str, trade_id: str = "") -> dict[str, Any]:
    """Pre-computed risk / break signals for a dispute (``trade_id`` optional)."""
    ctx = _context(dispute_id)
    if ctx is None:
        return _not_found(dispute_id)
    return {
        "dispute_id": dispute_id,
        "trade_id": (ctx.get("trade") or {}).get("trade_id"),
        "signals": signals.derived(ctx),
    }


@mcp.tool(
    description=(
        "Retrieve the full trade lifecycle chain for a trade_id: booked trade, allocation "
        "count, latest confirmation and affirmation, and the deterministic break analysis "
        "(broken_field, price/qty/gross break amounts). Read-only."
    )
)
def get_trade_lifecycle(trade_id: str) -> dict[str, Any]:
    """Trade -> allocation -> confirmation -> affirmation chain + break analysis."""
    with db.get_connection(_credential()) as conn:
        cursor = conn.cursor()

        def one(sql: str, *params: Any) -> dict[str, Any] | None:
            cursor.execute(sql, *params)
            return db._row_to_public_dict(cursor)

        trade = one(
            """
            SELECT trade_id, trade_date, settle_date, cp_buy_id, cp_sell_id, sec_id, cusip,
                   side, qty, price, gross_amt, ccy, exec_venue, exec_ts_utc, instrument_kind,
                   trader_id, book, is_stress_day
            FROM demo4_trades WHERE trade_id = ?
            """,
            trade_id,
        )
        if trade is None:
            return {"error": "not_found", "message": f"Trade {trade_id} not found in the ledger."}

        cursor.execute("SELECT COUNT(*) FROM demo4_allocations WHERE trade_id = ?", trade_id)
        allocations_count = int(cursor.fetchone()[0])

        confirmation = one(
            """
            SELECT TOP 1 confirm_id, trade_id, source, cfm_price, cfm_qty, cfm_gross,
                   status, broken_field, confirm_ts_utc
            FROM demo4_confirmations WHERE trade_id = ? ORDER BY confirm_ts_utc DESC
            """,
            trade_id,
        )
        affirmation = one(
            """
            SELECT TOP 1 affirm_id, confirm_id, trade_id, workflow, affirmer_role,
                   affirm_ts_utc, cutoff_ts_utc, status
            FROM demo4_affirmations WHERE trade_id = ? ORDER BY affirm_ts_utc DESC
            """,
            trade_id,
        )

    lifecycle = {"trade": trade, "confirmation": confirmation, "affirmation": affirmation}
    return {
        "trade": trade,
        "allocations_count": allocations_count,
        "confirmation": confirmation,
        "affirmation": affirmation,
        "break_analysis": signals.economics(lifecycle),
        "timing": signals.timing(lifecycle),
    }


@mcp.tool(
    description=(
        "Retrieve the evidence pack for a dispute: the 6-artifact presence map (trade, alloc, "
        "confirm, affirm, ssi, settle), completeness percentage, digest hash, and the ACL proof "
        "chain status (verify levels 1/2, lag minutes) from demo4_acl_receipts. Read-only."
    )
)
def get_evidence_pack(dispute_id: str) -> dict[str, Any]:
    """6-artifact evidence pack, completeness and ACL proof status for a dispute."""
    ctx = _context(dispute_id)
    if ctx is None:
        return _not_found(dispute_id)
    presence = signals.artifact_presence(ctx)
    ep = ctx["evidence_pack"] or {}
    acl = ctx["acl_receipt"] or {}
    gaps = [k for k, present in presence.items() if not present]
    return {
        "dispute_id": dispute_id,
        "artifacts_present": presence,
        "gaps": gaps,
        "completeness_pct": signals.num(ep.get("completeness_pct")),
        "digest_hash": ep.get("digest_hash"),
        "acl_proof": {
            "acl_txn_id": acl.get("acl_txn_id"),
            "verify_level_1": bool(acl.get("verify_level_1")),
            "verify_level_2": bool(acl.get("verify_level_2")),
            "lag_minutes": signals.num(acl.get("lag_minutes")),
            "ledger_verified": bool(acl.get("verify_level_1")) and bool(acl.get("verify_level_2")),
        },
    }


@mcp.tool(
    description=(
        "Verify the ACL proof chain for a dispute by executing dbo.usp_VerifyDisputeL1, which "
        "recomputes a SHA-256 hash of the current dispute fields and compares it to the hash "
        "anchored in demo4_acl_receipts. Returns PASS if unchanged since anchoring, or FAIL "
        "with the modification detail if tampered. Read-only (calls a verification proc)."
    )
)
def verify_acl_proof(dispute_id: str) -> dict[str, Any]:
    """Run the L1 ledger-proof verification stored procedure for a dispute."""
    try:
        with db.get_connection(_credential()) as conn:
            cursor = conn.cursor()
            cursor.execute("EXEC dbo.usp_VerifyDisputeL1 ?", dispute_id)
            if cursor.description is None:
                return {"dispute_id": dispute_id, "verify_status": "NO_RESULT"}
            cols = [c[0] for c in cursor.description]
            row = cursor.fetchone()
            if row is None:
                return {
                    "dispute_id": dispute_id,
                    "verify_status": "NOT_FOUND",
                    "verify_message": "No result returned from verification proc.",
                }
            return {k: db._jsonable(v) for k, v in zip(cols, row)}
    except Exception as exc:  # noqa: BLE001
        return {"dispute_id": dispute_id, "verify_status": "ERROR", "verify_message": str(exc)}


@mcp.tool(
    description=(
        "Answer a lightweight question about dispute data against the SQL ledger. Supports: a "
        "specific dispute_id (returns its summary), open-dispute counts by category, and the "
        "top disputes by notional. Pass a dispute_id in `context` to scope the answer. "
        "Read-only; not a full natural-language-to-SQL engine."
    )
)
def query_dispute_data(question: str, context: str = "") -> dict[str, Any]:
    """Small canned analytics over the ledger for quick agent lookups."""
    q = f"{question} {context}".upper()

    # Scope to a specific dispute if an id is present.
    token = None
    for word in q.replace(",", " ").split():
        if word.startswith("DSP-") or word.startswith("DIS"):
            token = word
            break
    if token:
        ctx = _context(token)
        if ctx is None:
            return _not_found(token)
        d = ctx["dispute"]
        return {
            "answer_kind": "dispute_summary",
            "dispute_id": d.get("dispute_id"),
            "category": d.get("category"),
            "status": d.get("status"),
            "notional_usd": d.get("notional_usd"),
            "completeness_pct": signals.num((ctx["evidence_pack"] or {}).get("completeness_pct")),
            "has_economic_break": signals.economics(ctx)["has_economic_break"],
        }

    with db.get_connection(_credential()) as conn:
        cursor = conn.cursor()
        if "TOP" in q or "NOTIONAL" in q or "LARGEST" in q:
            cursor.execute(
                """
                SELECT TOP 5 dispute_id, category, status, notional_usd
                FROM demo4_disputes ORDER BY notional_usd DESC
                """
            )
            return {
                "answer_kind": "top_disputes_by_notional",
                "rows": _rows(cursor),
            }
        cursor.execute(
            """
            SELECT category, COUNT(*) AS open_count
            FROM demo4_disputes WHERE status = 'OPEN'
            GROUP BY category ORDER BY open_count DESC
            """
        )
        return {
            "answer_kind": "open_disputes_by_category",
            "rows": _rows(cursor),
        }


# ---------------------------------------------------------------------------
# Simulated tools (the workflow owns the real HITL gate + ledger receipts).
# These exist so the MCP server mirrors the full tool catalog, but they never
# write to the ledger. They are NOT attached to the specialist agents.
# ---------------------------------------------------------------------------
@mcp.tool(
    description=(
        "SIMULATED. Submit a remediation proposal for human-in-the-loop approval. Returns a "
        "generated approval_record_id and PENDING status. The real HITL gate is enforced by the "
        "orchestrator workflow, not by this tool."
    )
)
def submit_for_hitl_approval(
    dispute_id: str, proposal: dict[str, Any], approver_role: str = "ops_analyst"
) -> dict[str, Any]:
    """Simulate creating a pending HITL approval record."""
    return {
        "simulated": True,
        "approval_record_id": f"APR-{uuid.uuid4().hex[:12].upper()}",
        "dispute_id": dispute_id,
        "approver_role": approver_role,
        "status": "PENDING",
        "submitted_ts_utc": datetime.now(timezone.utc).isoformat(),
        "proposal_digest": hashlib.sha256(
            json.dumps(proposal, sort_keys=True, default=str).encode()
        ).hexdigest(),
    }


@mcp.tool(
    description=(
        "SIMULATED. Write an approved decision to the ledger. Returns a SHA-256 digest receipt "
        "but performs NO database write — the orchestrator produces the real (simulated) ledger "
        "receipts after the human approval gate."
    )
)
def write_ledger_decision(
    dispute_id: str, decision: str, approver_id: str, rationale: str, approval_record_id: str
) -> dict[str, Any]:
    """Simulate an append-only ledger decision receipt (no DB write)."""
    record = {
        "dispute_id": dispute_id,
        "decision": decision,
        "approver_id": approver_id,
        "rationale": rationale,
        "approval_record_id": approval_record_id,
        "decision_ts_utc": datetime.now(timezone.utc).isoformat(),
    }
    return {
        "simulated": True,
        "acl_txn_id": f"ACL-{uuid.uuid4().hex[:12].upper()}",
        "digest_hash": hashlib.sha256(
            json.dumps(record, sort_keys=True, default=str).encode()
        ).hexdigest(),
        **record,
    }


if __name__ == "__main__":
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("MCP_PORT", "8001"))
    mcp.run(transport="http", host=host, port=port)
