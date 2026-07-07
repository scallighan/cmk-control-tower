"""Read-only data access for the CMK Control Tower reconciliation agent.

Connects to the Azure SQL ``cmk-sqldb-ledger`` database using an Entra ID
(Azure AD) access token obtained from the local ``az login`` session, exactly
like the Foundry client in ``main.py`` (both use ``AzureCliCredential``).

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
from azure.identity import AzureCliCredential

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


def _token_struct(credential: AzureCliCredential) -> bytes:
    """Fetch an Entra token for Azure SQL and pack it the way ODBC expects."""
    token = credential.get_token(_DATABASE_SCOPE).token.encode("utf-16-le")
    return struct.pack("=i", len(token)) + token


def get_connection(credential: AzureCliCredential | None = None) -> pyodbc.Connection:
    """Open a read-only connection to the ledger DB using an Entra token."""
    credential = credential or AzureCliCredential()
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
    trade_id: str, credential: AzureCliCredential | None = None
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
    trade_ids: list[str], credential: AzureCliCredential | None = None
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


def fetch_securities(sec_ids: set[str], credential: AzureCliCredential | None = None) -> dict[str, Security]:
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


def fetch_counterparties(cp_ids: set[str], credential: AzureCliCredential | None = None) -> dict[str, Counterparty]:
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
