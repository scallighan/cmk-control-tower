"""Deterministic, pure signal derivations over a dispute-context dict.

These helpers turn the raw ledger context assembled by
``db.fetch_dispute_context`` into the compact "analytic signals" the agents (and
their deterministic fallbacks) reason over: confirmation-vs-booked economics, SSI
freshness, affirmation timing, and a rolled-up ``derived`` bundle.

They live in their own module so both the orchestrator workflow (``main.py``) and
the MCP tool server (``mcp_server.py``) can share exactly the same logic without
importing the heavyweight workflow graph.
"""

from typing import Any


def num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def economics(ctx_data: dict[str, Any]) -> dict[str, Any]:
    """Deterministic confirmation-vs-booked-trade comparison used by agents/fallbacks."""
    trade = ctx_data.get("trade") or {}
    cfm = ctx_data.get("confirmation") or {}
    price_break = num(cfm.get("cfm_price")) - num(trade.get("price"))
    qty_break = num(cfm.get("cfm_qty")) - num(trade.get("qty"))
    gross_break = num(cfm.get("cfm_gross")) - num(trade.get("gross_amt"))
    return {
        "broken_field": cfm.get("broken_field"),
        "confirm_status": cfm.get("status"),
        "price_break_amount": round(price_break, 6),
        "qty_break_amount": round(qty_break, 4),
        "gross_break_amount": round(gross_break, 2),
        "has_economic_break": abs(price_break) > 1e-9 or abs(qty_break) > 1e-9 or abs(gross_break) > 1e-2,
    }


def ssi_mismatch(ctx_data: dict[str, Any]) -> dict[str, Any]:
    snap = ctx_data.get("ssi_snapshot") or {}
    curr = ctx_data.get("ssi_current") or {}
    stale = bool(snap and curr and num(snap.get("version")) < num(curr.get("version")))
    return {
        "snapshot_version": snap.get("version"),
        "current_version": curr.get("version"),
        "snapshot_is_stale": stale,
        "instruction_mismatch_kind": (ctx_data.get("settlement_instruction") or {}).get("mismatch_kind"),
    }


def timing(ctx_data: dict[str, Any]) -> dict[str, Any]:
    aff = ctx_data.get("affirmation") or {}
    status = (aff.get("status") or "").upper()
    return {
        "affirm_status": status,
        "timing_breach_flag": status in {"MISSING", "AFFIRMED_LATE", "UNAFFIRMED"},
        "cutoff_ts_utc": aff.get("cutoff_ts_utc"),
        "affirm_ts_utc": aff.get("affirm_ts_utc"),
    }


def derived(ctx_data: dict[str, Any]) -> dict[str, Any]:
    return {
        "economics": economics(ctx_data),
        "ssi": ssi_mismatch(ctx_data),
        "timing": timing(ctx_data),
        "settlement_status": ctx_data.get("settlement_status"),
        "cp_profile": ctx_data.get("cp_profile"),
    }


def artifact_presence(ctx_data: dict[str, Any]) -> dict[str, bool]:
    """The 6-artifact evidence-pack presence map (trade -> settlement)."""
    return {
        "trade_msg": ctx_data.get("trade") is not None,
        "alloc_msg": num(ctx_data.get("allocations_count")) > 0,
        "confirm_msg": ctx_data.get("confirmation") is not None,
        "affirm_msg": ctx_data.get("affirmation") is not None,
        "ssi_msg": ctx_data.get("ssi_snapshot") is not None,
        "settle_msg": ctx_data.get("settlement_status") is not None,
    }
