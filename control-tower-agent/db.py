"""Read-only data access for the CMK Control Tower reconciliation agent.

Connects to the Azure SQL ``cmk-sqldb-ledger`` database using an Entra ID
(Azure AD) access token obtained from the local ``az login`` session, exactly
like the Foundry client in ``main.py`` (both use ``DefaultAzureCredential``).

The database is a set of SQL Server *ledger* tables (tamper-evident). This
module only ever issues ``SELECT`` statements -- nothing here writes back.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pyodbc
from azure.identity import DefaultAzureCredential

# The ODBC connection attribute id for "use this Entra access token".
SQL_COPT_SS_ACCESS_TOKEN = 1256
# The scope/resource that Azure SQL Database expects tokens to be issued for.
_DATABASE_SCOPE = "https://database.windows.net/.default"

DEFAULT_SERVER = ""
DEFAULT_DATABASE = ""


@dataclass
class Security:
    """A row from ``demo4_securities`` -- the securities master reference."""

    sec_id: str
    cusip: str
    ticker: str
    ccy: str
    reference_price: Decimal


@dataclass
class Counterparty:
    """A row from ``demo4_counterparties`` -- the approved-counterparty list."""

    cp_id: str
    lei: str
    cp_name: str
    cp_kind: str


@dataclass
class TradeRecord:
    """A booked trade from ``demo4_trades`` -- the firm's economics of record.

    Used by the CSV-ingest flow: an uploaded confirmation is matched to one of
    these by ``trade_id`` and the agent reconciles the two.
    """

    trade_id: str
    sec_id: str
    cusip: str
    side: str
    qty: int
    price: Decimal
    gross_amt: Decimal
    ccy: str
    cp_buy_id: str
    cp_sell_id: str
    trade_date: str
    settle_date: str


def _token_struct(credential: DefaultAzureCredential) -> bytes:
    """Fetch an Entra token for Azure SQL and pack it the way ODBC expects."""
    token = credential.get_token(_DATABASE_SCOPE).token.encode("utf-16-le")
    return struct.pack("=i", len(token)) + token


def get_connection(credential: DefaultAzureCredential | None = None) -> pyodbc.Connection:
    """Open a read-only connection to the ledger DB using an Entra token."""
    credential = credential or DefaultAzureCredential()
    server = os.environ.get("SQL_SERVER", DEFAULT_SERVER)
    database = os.environ.get("SQL_DATABASE", DEFAULT_DATABASE)
    # Serverless Azure SQL can auto-pause; resuming may take a while, so allow a
    # generous, env-configurable login timeout.
    login_timeout = os.environ.get("SQL_LOGIN_TIMEOUT", "90")
    conn_str = (
        "Driver={ODBC Driver 18 for SQL Server};"
        f"Server=tcp:{server},1433;"
        f"Database={database};"
        f"Encrypt=yes;TrustServerCertificate=no;Connection Timeout={login_timeout};"
    )
    return pyodbc.connect(conn_str, attrs_before={SQL_COPT_SS_ACCESS_TOKEN: _token_struct(credential)})


def _rows_as_dicts(cursor: pyodbc.Cursor) -> list[dict[str, Any]]:
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _jsonable(value: Any) -> Any:
    """Convert DB values (Decimal, date/datetime, bytes) into JSON-friendly forms.

    Decimals are stringified to preserve full precision for display.
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    if hasattr(value, "isoformat"):  # date / datetime
        return value.isoformat()
    if isinstance(value, str):
        return value.strip()
    return value


def _row_to_public_dict(cursor: pyodbc.Cursor) -> dict[str, Any] | None:
    row = cursor.fetchone()
    if row is None:
        return None
    columns = [d[0] for d in cursor.description]
    return {col: _jsonable(val) for col, val in zip(columns, row)}


def fetch_trade_detail(
    trade_id: str, credential: DefaultAzureCredential | None = None
) -> dict[str, Any] | None:
    """Fetch the full DB detail behind a booked trade for the UI modal.

    Returns the booked trade, the securities-master row, and both counterparties
    (buy/sell), or ``None`` if the trade is unknown. The counterparty
    confirmation itself comes from the uploaded CSV (held in run state), not the
    database. All values are JSON-friendly primitives.
    """
    with get_connection(credential) as conn:
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT trade_id, trade_date, settle_date, cp_buy_id, cp_sell_id, sec_id, cusip,
                   side, qty, price, gross_amt, ccy, exec_venue, exec_ts_utc, instrument_kind,
                   trader_id, book, is_stress_day
            FROM demo4_trades WHERE trade_id = ?
            """,
            trade_id,
        )
        trade = _row_to_public_dict(cursor)
        if trade is None:
            return None

        cursor.execute(
            """
            SELECT sec_id, cusip, isin, sedol, ticker, asset_class, sector, ccy,
                   reference_price, avg_daily_volume
            FROM demo4_securities WHERE sec_id = ?
            """,
            trade["sec_id"],
        )
        security = _row_to_public_dict(cursor)

        def _cp(cp_id: str | None) -> dict[str, Any] | None:
            if not cp_id:
                return None
            cursor.execute(
                """
                SELECT cp_id, lei, cp_name, cp_kind, region, size_tier, onboarded_dt
                FROM demo4_counterparties WHERE cp_id = ?
                """,
                cp_id,
            )
            return _row_to_public_dict(cursor)

        counterparty_buy = _cp(trade["cp_buy_id"])
        counterparty_sell = _cp(trade["cp_sell_id"])

    return {
        "trade": trade,
        "security": security,
        "counterparty_buy": counterparty_buy,
        "counterparty_sell": counterparty_sell,
    }


def fetch_trades_by_ids(
    trade_ids: list[str], credential: DefaultAzureCredential | None = None
) -> dict[str, TradeRecord]:
    """Fetch booked trades for the given ``trade_id`` values (order-insensitive)."""
    unique = list(dict.fromkeys(trade_ids))
    if not unique:
        return {}
    placeholders = ",".join("?" for _ in unique)
    sql = (
        "SELECT trade_id, sec_id, cusip, side, qty, price, gross_amt, ccy, "
        "cp_buy_id, cp_sell_id, trade_date, settle_date "
        f"FROM demo4_trades WHERE trade_id IN ({placeholders})"
    )
    with get_connection(credential) as conn:
        cursor = conn.cursor()
        cursor.execute(sql, *unique)
        rows = _rows_as_dicts(cursor)
    return {
        r["trade_id"]: TradeRecord(
            trade_id=r["trade_id"],
            sec_id=r["sec_id"],
            cusip=r["cusip"].strip(),
            side=r["side"].strip(),
            qty=int(r["qty"]),
            price=r["price"],
            gross_amt=r["gross_amt"],
            ccy=r["ccy"].strip(),
            cp_buy_id=r["cp_buy_id"],
            cp_sell_id=r["cp_sell_id"],
            trade_date=str(r["trade_date"]),
            settle_date=str(r["settle_date"]),
        )
        for r in rows
    }


def fetch_securities(sec_ids: set[str], credential: DefaultAzureCredential | None = None) -> dict[str, Security]:
    """Fetch the securities-master rows for the given ``sec_id`` values."""
    if not sec_ids:
        return {}
    placeholders = ",".join("?" for _ in sec_ids)
    sql = (
        "SELECT sec_id, cusip, ticker, ccy, reference_price "
        f"FROM demo4_securities WHERE sec_id IN ({placeholders})"
    )
    with get_connection(credential) as conn:
        cursor = conn.cursor()
        cursor.execute(sql, *sec_ids)
        rows = _rows_as_dicts(cursor)
    return {
        r["sec_id"]: Security(
            sec_id=r["sec_id"],
            cusip=r["cusip"].strip(),
            ticker=r["ticker"].strip(),
            ccy=r["ccy"].strip(),
            reference_price=r["reference_price"],
        )
        for r in rows
    }


def fetch_open_disputes(
    limit: int = 200, credential: DefaultAzureCredential | None = None
) -> list[dict[str, Any]]:
    """List OPEN disputes for the control-tower work queue.

    Joins counterparty names, evidence completeness, and the underlying trade's
    security/side so the UI can render a rich dispute list without extra calls.
    """
    sql = """
        SELECT TOP (?)
            d.dispute_id, d.trade_id, d.category, d.status, d.notional_usd,
            d.filer_cp_id, d.cp_buy_id, d.cp_sell_id, d.opened_ts_utc, d.resolution,
            cb.cp_name  AS buy_name,
            cs.cp_name  AS sell_name,
            cf.cp_name  AS filer_name,
            ep.completeness_pct,
            t.side, t.ccy, s.ticker, s.cusip
        FROM demo4_disputes d
        LEFT JOIN demo4_counterparties cb ON cb.cp_id = d.cp_buy_id
        LEFT JOIN demo4_counterparties cs ON cs.cp_id = d.cp_sell_id
        LEFT JOIN demo4_counterparties cf ON cf.cp_id = d.filer_cp_id
        LEFT JOIN demo4_evidence_packs  ep ON ep.dispute_id = d.dispute_id
        LEFT JOIN demo4_trades          t  ON t.trade_id  = d.trade_id
        LEFT JOIN demo4_securities      s  ON s.sec_id    = t.sec_id
        WHERE d.status = 'OPEN'
        ORDER BY d.notional_usd DESC
    """
    with get_connection(credential) as conn:
        cursor = conn.cursor()
        cursor.execute(sql, limit)
        columns = [d[0] for d in cursor.description]
        return [{c: _jsonable(v) for c, v in zip(columns, row)} for row in cursor.fetchall()]


def fetch_dispute_context(
    dispute_id: str, credential: DefaultAzureCredential | None = None
) -> dict[str, Any] | None:
    """Assemble the full evidence context an agent pipeline needs for one dispute.

    Reads the dispute and its entire trade lifecycle (trade, security,
    confirmation, affirmation, settlement instruction/status, SSI snapshot vs
    current, evidence pack, ACL receipt, communications, counterparties, and a
    light counterparty risk profile) from the ledger tables -- strictly SELECTs.
    Returns ``None`` if the dispute id is unknown.
    """
    with get_connection(credential) as conn:
        cursor = conn.cursor()

        def one(sql: str, *params: Any) -> dict[str, Any] | None:
            cursor.execute(sql, *params)
            return _row_to_public_dict(cursor)

        dispute = one(
            """
            SELECT d.dispute_id, d.trade_id, d.cp_buy_id, d.cp_sell_id, d.category,
                   d.opened_ts_utc, d.closed_ts_utc, d.status, d.filer_cp_id,
                   d.notional_usd, d.resolution, d.ttr_hours,
                   cf.cp_name AS filer_name
            FROM demo4_disputes d
            LEFT JOIN demo4_counterparties cf ON cf.cp_id = d.filer_cp_id
            WHERE d.dispute_id = ?
            """,
            dispute_id,
        )
        if dispute is None:
            return None
        trade_id = dispute["trade_id"]

        trade = one(
            """
            SELECT trade_id, trade_date, settle_date, cp_buy_id, cp_sell_id, sec_id, cusip,
                   side, qty, price, gross_amt, ccy, exec_venue, exec_ts_utc, instrument_kind,
                   trader_id, book, is_stress_day
            FROM demo4_trades WHERE trade_id = ?
            """,
            trade_id,
        )
        security = None
        if trade:
            security = one(
                """
                SELECT sec_id, cusip, isin, ticker, asset_class, sector, ccy,
                       reference_price, avg_daily_volume
                FROM demo4_securities WHERE sec_id = ?
                """,
                trade["sec_id"],
            )

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
        settlement_instruction = one(
            """
            SELECT TOP 1 si_id, trade_id, ssi_id_snap, ssi_version, bic_snap, account_snap,
                   pset_snap, cutoff_type, instructed_ts_utc, mismatch_kind
            FROM demo4_settlement_instructions WHERE trade_id = ? ORDER BY instructed_ts_utc DESC
            """,
            trade_id,
        )
        settlement_status = one(
            """
            SELECT TOP 1 ss_id, trade_id, clearing, status, status_dt, fail_age_days,
                   fail_reason, resolved_dt
            FROM demo4_settlement_statuses WHERE trade_id = ? ORDER BY status_dt DESC
            """,
            trade_id,
        )

        ssi_snapshot = None
        ssi_current = None
        if settlement_instruction and settlement_instruction.get("ssi_id_snap"):
            ssi_id = settlement_instruction["ssi_id_snap"]
            ssi_snapshot = one(
                """
                SELECT ssi_id, cp_id, sec_ccy, bic, account, pset, version,
                       valid_from, valid_to, source, last_confirmed_dt
                FROM demo4_ssis WHERE ssi_id = ?
                """,
                ssi_id,
            )
            if ssi_snapshot:
                ssi_current = one(
                    """
                    SELECT TOP 1 ssi_id, cp_id, sec_ccy, bic, account, pset, version,
                           valid_from, valid_to, last_confirmed_dt
                    FROM demo4_ssis WHERE cp_id = ? AND sec_ccy = ?
                    ORDER BY version DESC
                    """,
                    ssi_snapshot["cp_id"],
                    ssi_snapshot["sec_ccy"],
                )

        evidence_pack = one(
            """
            SELECT pack_id, dispute_id, artifacts_present, artifacts_total, completeness_pct,
                   digest_hash, assembled_ts_utc
            FROM demo4_evidence_packs WHERE dispute_id = ?
            """,
            dispute_id,
        )
        acl_receipt = one(
            """
            SELECT receipt_id, dispute_id, digest_hash, acl_txn_id, digest_gen_ts_utc,
                   acl_receipt_ts_utc, lag_minutes, verify_level_1, verify_level_2
            FROM demo4_acl_receipts WHERE dispute_id = ?
            """,
            dispute_id,
        )

        cursor.execute(
            "SELECT COUNT(*) FROM demo4_allocations WHERE trade_id = ?", trade_id
        )
        allocations_count = int(cursor.fetchone()[0])

        cursor.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN direction = 'INBOUND'  THEN 1 ELSE 0 END) AS inbound,
                   SUM(CASE WHEN direction = 'OUTBOUND' THEN 1 ELSE 0 END) AS outbound,
                   AVG(CAST(response_min AS FLOAT)) AS avg_response_min,
                   MAX(ts_utc) AS last_ts
            FROM demo4_communications WHERE dispute_id = ?
            """,
            dispute_id,
        )
        communications = _row_to_public_dict(cursor) or {}

        def cp(cp_id: str | None) -> dict[str, Any] | None:
            if not cp_id:
                return None
            return one(
                """
                SELECT cp_id, lei, cp_name, cp_kind, region, size_tier, onboarded_dt
                FROM demo4_counterparties WHERE cp_id = ?
                """,
                cp_id,
            )

        counterparty_buy = cp(dispute["cp_buy_id"])
        counterparty_sell = cp(dispute["cp_sell_id"])
        filer_cp = cp(dispute.get("filer_cp_id"))

        # Light counterparty risk profile for the filer: their dispute history.
        cp_profile: dict[str, Any] = {}
        if dispute.get("filer_cp_id"):
            cursor.execute(
                """
                SELECT COUNT(*) AS total_disputes,
                       SUM(CASE WHEN status = 'OPEN' THEN 1 ELSE 0 END) AS open_disputes,
                       SUM(CASE WHEN category = 'fail' THEN 1 ELSE 0 END) AS fail_disputes
                FROM demo4_disputes
                WHERE cp_buy_id = ? OR cp_sell_id = ? OR filer_cp_id = ?
                """,
                dispute["filer_cp_id"], dispute["filer_cp_id"], dispute["filer_cp_id"],
            )
            cp_profile = _row_to_public_dict(cursor) or {}

        cursor.execute(
            """
            SELECT agent, confidence, recommendation, created_ts_utc
            FROM demo4_agent_findings WHERE dispute_id = ? ORDER BY created_ts_utc
            """,
            dispute_id,
        )
        prior_findings = _rows_as_dicts(cursor)
        prior_findings = [{k: _jsonable(v) for k, v in r.items()} for r in prior_findings]

    return {
        "dispute": dispute,
        "trade": trade,
        "security": security,
        "confirmation": confirmation,
        "affirmation": affirmation,
        "settlement_instruction": settlement_instruction,
        "settlement_status": settlement_status,
        "allocations_count": allocations_count,
        "ssi_snapshot": ssi_snapshot,
        "ssi_current": ssi_current,
        "evidence_pack": evidence_pack,
        "acl_receipt": acl_receipt,
        "communications": communications,
        "counterparty_buy": counterparty_buy,
        "counterparty_sell": counterparty_sell,
        "filer_cp": filer_cp,
        "cp_profile": cp_profile,
        "prior_findings": prior_findings,
    }


def fetch_counterparties(cp_ids: set[str], credential: DefaultAzureCredential | None = None) -> dict[str, Counterparty]:
    """Fetch the approved-counterparty rows for the given ``cp_id`` values."""
    if not cp_ids:
        return {}
    placeholders = ",".join("?" for _ in cp_ids)
    sql = (
        "SELECT cp_id, lei, cp_name, cp_kind "
        f"FROM demo4_counterparties WHERE cp_id IN ({placeholders})"
    )
    with get_connection(credential) as conn:
        cursor = conn.cursor()
        cursor.execute(sql, *cp_ids)
        rows = _rows_as_dicts(cursor)
    return {
        r["cp_id"]: Counterparty(
            cp_id=r["cp_id"],
            lei=r["lei"].strip(),
            cp_name=r["cp_name"].strip(),
            cp_kind=r["cp_kind"].strip(),
        )
        for r in rows
    }
