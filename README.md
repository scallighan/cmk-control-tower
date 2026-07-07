# cmk-control-tower

A trade **reconciliation control tower** built on the Microsoft Agent Framework
(`agent_framework`) workflow engine, running against a **real Azure SQL ledger
database** (`cmk-sqldb-ledger`).

It expands on [implodingduck/maf-reconcile-agent](https://github.com/implodingduck/maf-reconcile-agent):
where the original validated a CSV against an in-memory securities master, this
version has you **upload a CSV of counterparty confirmations** and a
reconciliation **agent** reads each row, looks up the booked trade it references
in SQL Server, and decides for itself whether the confirmation matches the
firm's economics of record. Clean confirmations finalize immediately; any the
agent flags as broken enter a human-in-the-loop approval gate.

All database access is **read-only** — the ledger tables are never modified.
Proposed corrections are applied to an in-memory copy purely to produce the
reconciled report.

## Flow (`control-tower-agent/main.py`)

```
upload confirmations CSV -> look up each referenced booked trade (SQL, read-only)
    -> reconciliation agent decides matched vs broken + proposes cfm_* fixes
    -> switch:
         all matched  -> finalize (emit confirmed report)
         any broken   -> HUMAN approve / deny / modify
                      -> apply decision (in memory), re-verify, emit report
```

1. **Ingest** — parse the uploaded CSV (`trade_id, source, cfm_price, cfm_qty,
   cfm_gross`), look up each `trade_id` in `demo4_trades` (the economics of
   record). Rows with no booked trade are reported as skipped.
2. **Reconciliation agent** — an Azure AI Foundry chat agent receives each
   confirmation paired with its booked trade and returns structured
   (`pydantic`) per-row verdicts: `matched` (bool), a short `summary`, and
   minimal field-level corrections to the confirmation fields
   (`cfm_price`, `cfm_qty`, `cfm_gross`) for any break.
3. **Route** — a switch-case edge sends fully-matched uploads straight to
   finalize; any break goes to the human approval gate.
4. **Human in the loop** — the workflow suspends via `ctx.request_info()`; the
   caller approves, denies, or modifies the suggestions and resumes with
   `workflow.run(responses=...)`.
5. **Apply & finalize** — approved corrections are applied to the in-memory
   confirmation copy, each pair is deterministically re-verified, and the
   reconciled report is emitted.

## Data source

Azure SQL database `cmk-sqldb-ledger` on `cmk-sqldb-srv.database.windows.net`.
The confirmations come from the uploaded CSV; the workflow reads these `demo4_`
tables for the booked economics and reference detail:

| Table | Role |
| --- | --- |
| `demo4_trades` | Booked trade — economics of record (`qty`, `price`, `gross_amt`, `ccy`) |
| `demo4_securities` | Securities master (`cusip`, `ccy`, `reference_price`) |
| `demo4_counterparties` | Approved-counterparty list (shown in the detail modal) |

These are SQL Server *ledger* tables (tamper-evident), so the app treats them as
strictly read-only.

## Prerequisites

- **ODBC Driver 18 for SQL Server** installed locally (used by `pyodbc`):
  ```bash
  curl -sSL https://packages.microsoft.com/keys/microsoft.asc | sudo tee /etc/apt/trusted.gpg.d/microsoft.asc > /dev/null
  curl -sSL https://packages.microsoft.com/config/ubuntu/24.04/prod.list | sudo tee /etc/apt/sources.list.d/mssql-release.list
  sudo apt-get update && sudo ACCEPT_EULA=Y apt-get install -y msodbcsql18 unixodbc-dev
  ```
- **Azure CLI login** (`az login`) — the same `AzureCliCredential` is used for
  both the Foundry chat model and the Entra token for Azure SQL. Your identity
  needs `db_datareader` (or equivalent) on `cmk-sqldb-ledger`.
- **Azure AI Foundry** project endpoint + a deployed chat model.

## Run (CLI)

```bash
cd control-tower-agent
pip install -r requirements.txt
cp .env.example .env      # fill in FOUNDRY_PROJECT_ENDPOINT / FOUNDRY_MODEL
az login                  # AzureCliCredential
python main.py                        # reconciles samples/confirmations.csv
python main.py path/to/your.csv       # or your own confirmations CSV
```

`.env` provides `FOUNDRY_PROJECT_ENDPOINT`, `FOUNDRY_MODEL`, and optional
`SQL_SERVER`, `SQL_DATABASE`, and `SQL_LOGIN_TIMEOUT` (seconds to wait for the
serverless DB to resume, default 90).

The uploaded CSV has one row per counterparty confirmation:

```csv
trade_id,source,cfm_price,cfm_qty,cfm_gross
TRD-0000001,FIX,21.3527,8800,187903.76
```

When the agent flags breaks you'll be prompted at the console to
`approve` / `deny` / `modify` the proposed confirmation corrections.

## Run (web server + React UI)

The same workflow is also exposed over HTTP by a FastAPI server
(`control-tower-agent/server.py`), with a React (Vite + TypeScript) front end in
`control-tower-agent/web/`.

**Backend** — from `control-tower-agent/` (needs `az login` + a populated `.env`):

```bash
pip install -r requirements.txt
uvicorn server:app --reload --port 8000
```

API:

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/runs` | Upload a confirmations CSV (multipart `file`); runs until it suspends for approval (or completes) |
| `GET` | `/api/runs` | List runs |
| `GET` | `/api/runs/{run_id}` | Current state of a run (agent assessment + pending approval + outputs) |
| `POST` | `/api/runs/{run_id}/decision` | Submit `approve` / `deny` / `modify` and resume |
| `GET` | `/api/trades/{trade_id}` | Full DB detail behind a booked trade (trade, securities master, counterparties) |

The server keeps live workflow instances in memory keyed by `run_id` so a
suspended workflow can be resumed by the approval call.

**Frontend** — from `control-tower-agent/web/`:

```bash
npm install
npm run dev        # http://localhost:5173, proxies /api -> http://localhost:8000
```

Upload a confirmations CSV and the UI shows the agent's per-row assessment
(matched vs break, with the agent's summary). For breaks, review the proposed
corrections in a table (edit values inline or drop individual changes), then
**Approve**, **Apply my edits** (modify), or **Deny all**, and see the final
reconciled report. Click any **trade ID** to open a modal with the full detail —
the uploaded confirmation, the booked trade, the securities-master row, and both
counterparties — with the mismatched values highlighted. Trade detail is served
by `GET /api/trades/{trade_id}`.
