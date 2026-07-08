# cmk-control-tower

A capital-markets **counterparty-dispute resolution control tower** built on the
Microsoft Agent Framework (`agent_framework`) workflow engine, running against a
**real Azure SQL ledger database** (`cmk-sqldb-ledger`).

It expands on [implodingduck/maf-reconcile-agent](https://github.com/implodingduck/maf-reconcile-agent):
where the original validated a CSV against an in-memory securities master, this
version presents a **live queue of open disputes** from SQL Server and, when you
select one, kicks off a **pipeline of specialized agents** that reconstruct,
diagnose and remediate the break before a human approve/deny/modify gate. The
agents assemble the full trade lifecycle from the ledger, score settlement risk,
diagnose the root cause, and draft a remediation — the orchestrator then enforces
human-in-the-loop approval and writes the outcome.

All database access is **read-only** — the ledger tables are never modified.
Each artifact is committed to a **simulated Azure Confidential Ledger** (a real
SHA-256 digest with a stubbed transaction id).

## The agents (`control-tower-agent/agents.py`, orchestrated in `main.py`)

| Agent | Responsibility | Consumes | Produces (ledger) |
| --- | --- | --- | --- |
| **Intake** | Classify the dispute type, identify the governing rule set, register the case | Counterparty allegation, exception feed | `Dispute` (case opened) |
| **Prediction** | Score at-risk trades before affirmation / settlement cutoffs | Match state, SSI status, historical fail patterns, counterparty profile | `AgentFinding` (risk score) |
| **Reconstruction / Evidence** | Assemble the full lifecycle into a canonical evidence pack | FIX/FpML, CTM state, TradeSuite affirmations, DTC events, SSI history, comms | `EvidencePack` (+ `digest_hash`) |
| **Root-Cause** | Diagnose break category & materially responsible party | Reconstructed lifecycle, similar-case corpus | `AgentFinding` (root cause) |
| **Remediation** | Draft chaser / cancel-rebook / propose economic adjustment | Root-cause output, playbooks, comms templates | `AgentFinding` (recommendation) |
| **Orchestrator** | Route between agents, enforce human-in-the-loop approval, write to ledger | All agent outputs | `ApprovalRecord`, `ACLReceipt` |

The lifecycle each agent reasons over is assembled from the ledger by
`db.fetch_dispute_context()` (trade, confirmation, affirmation, settlement
instruction/status, SSI snapshot vs current, evidence pack, communications,
counterparty profile, prior findings). Deterministic signals (economic break
amounts, SSI staleness, timing breach) are computed in `signals.py` (shared by
the workflow and the MCP tool server) so each agent stage has a fallback and the
pipeline never stalls. The typed ledger artifacts live in
`control-tower-agent/artifacts.py`.

When `MCP_SERVER_URL` is set, each specialist agent is also given the relevant
read-only ledger **MCP tools** (see [Tools (MCP server)](#tools-mcp-server)) so
it can fetch or independently verify facts on demand (e.g. Reconstruction calls
`verify_acl_proof`); without it the agents run tool-less on the inline context.

## Flow (`control-tower-agent/main.py`)

```
pick an OPEN dispute (SQL, read-only)
    -> load full dispute lifecycle context
    -> Intake       : classify dispute + severity, register case
    -> Prediction   : score pre-cutoff settlement risk
    -> Reconstruction: assemble EvidencePack, verify ledger digest
    -> Root-Cause   : diagnose break type + responsible party
    -> Remediation  : draft chaser / economic adjustment + HITL summary
    -> Orchestrator : HUMAN approve / deny / modify
                   -> write ApprovalRecord + ACLReceipts (simulated ACL)
```

1. **Load context** — the selected `dispute_id` is expanded into its full
   lifecycle from the `demo4_*` ledger tables.
2. **Intake** — classifies the dispute (`economic`, `ssi`, `affirmation`,
   `fail`, …), assigns severity, and routes.
3. **Prediction** — scores pre-cutoff settlement risk from the deterministic
   signal breakdown (economic break, SSI mismatch probability, timing breach,
   counterparty risk).
4. **Reconstruction / Evidence** — checks which lifecycle artifacts are present,
   verifies the ledger digest, and reports completeness / proof-integrity.
5. **Root-Cause** — diagnoses the primary break type, the broken field
   (booked vs confirmed value + break amount), the responsible party, and a
   recommended resolution.
6. **Remediation** — proposes the action (`ADJUST`, `REBOOK`, `SSI_UPDATE`,
   `CLAIM`, …), amount, urgency, and a drafted counterparty communication.
7. **Orchestrator (human in the loop)** — the workflow suspends via
   `ctx.request_info()`; the caller approves, denies, or overrides the
   resolution and resumes with `workflow.run(responses=...)`.
8. **Ledger** — an `ApprovalRecord` plus `ACLReceipt`s (EvidencePack +
   ApprovalRecord digests) are written to the simulated ledger.

## Data source

Azure SQL database `cmk-sqldb-ledger` on `cmk-sqldb-srv.database.windows.net`.
The workflow reads these `demo4_` tables (read-only):

| Table | Role |
| --- | --- |
| `demo4_disputes` | The dispute queue — open cases, category, notional, filer |
| `demo4_trades` | Booked trade — economics of record (`qty`, `price`, `gross_amt`) |
| `demo4_confirmations` | Counterparty confirmation (`cfm_*`, `broken_field`, status) |
| `demo4_affirmations` | Affirmation status & timing vs cutoff |
| `demo4_settlement_instructions` / `demo4_settlement_statuses` | Settlement leg & fail state |
| `demo4_ssis` | Standing settlement instructions (snapshot vs current version) |
| `demo4_evidence_packs` | Reconstructed evidence pack + digest |
| `demo4_agent_findings` / `demo4_approval_records` / `demo4_acl_receipts` | Prior findings & ledger trail |
| `demo4_communications` | Chat / email allegations |
| `demo4_counterparties` / `demo4_securities` | Reference master data |

These are SQL Server *ledger* tables (tamper-evident), so the app treats them as
strictly read-only.

## Tools (MCP server)

The tool calls the agents were designed around are served by a real
[Model Context Protocol](https://modelcontextprotocol.io) server,
`control-tower-agent/mcp_server.py` (built on [FastMCP](https://gofastmcp.com)),
backed by the same read-only ledger via `db.py`. The Microsoft Agent Framework
attaches these tools to the agents (`MCPStreamableHTTPTool`) and auto-connects
them at run time.

| Tool | Backed by | Used by |
| --- | --- | --- |
| `get_dispute_record` | `db.fetch_dispute_context` | Intake, Reconstruction, Remediation |
| `get_analytic_signals` | `signals.derived` | Prediction, Root-Cause, Remediation |
| `get_trade_lifecycle` | `demo4_trades/confirmations/affirmations` + `signals.economics` | Prediction, Root-Cause |
| `get_evidence_pack` | `signals.artifact_presence` + `demo4_evidence_packs/acl_receipts` | Reconstruction |
| `verify_acl_proof` | `EXEC dbo.usp_VerifyDisputeL1` (SHA-256 proof check) | Reconstruction |
| `query_dispute_data` | ad-hoc ledger aggregates | Intake |
| `submit_for_hitl_approval` / `write_ledger_decision` | *simulated* (no DB write) | — (the orchestrator owns the real HITL gate + receipts) |

Everything that touches the ledger is strictly read-only. The approval /
ledger-write tools are simulated on purpose — the genuine human-in-the-loop gate
and ledger receipts are produced deterministically by the orchestrator workflow,
not by an LLM tool call, so they are **not** attached to the specialist agents.

Run the server (in its own terminal, needs `az login`):

```bash
cd control-tower-agent
python mcp_server.py            # serves http://127.0.0.1:8001/mcp  (MCP_HOST / MCP_PORT)
```

Then point the agents at it by setting `MCP_SERVER_URL` in `.env`:

```
MCP_SERVER_URL=http://127.0.0.1:8001/mcp
```

Leave `MCP_SERVER_URL` unset to run the pipeline tool-less (agents reason only
over the inline context — the original behaviour).

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
cp .env.example .env       # fill in FOUNDRY_PROJECT_ENDPOINT / FOUNDRY_MODEL
az login                   # AzureCliCredential
python main.py             # lists open disputes, run the pipeline on one
python main.py DSP-0000407 # or resolve a specific dispute id
```

`.env` provides `FOUNDRY_PROJECT_ENDPOINT`, `FOUNDRY_MODEL`, optional
`SQL_SERVER`, `SQL_DATABASE`, and `SQL_LOGIN_TIMEOUT` (seconds to wait for the
serverless DB to resume, default 90), and optional `MCP_SERVER_URL` to attach the
[MCP tools](#tools-mcp-server) to the agents.

When the pipeline reaches the orchestrator you'll be prompted at the console to
`approve` / `deny` / `modify` the proposed resolution.

## Run (web server + React UI)

The same workflow is exposed over HTTP by a FastAPI server
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
| `GET` | `/api/disputes` | Open dispute work queue (from the ledger) |
| `GET` | `/api/disputes/{dispute_id}` | Full dispute context + derived signals |
| `POST` | `/api/disputes/{dispute_id}/runs` | Create a run session (returns `run_id`; the pipeline is driven by the events stream) |
| `GET` | `/api/runs/{run_id}/events` | **Server-Sent Events** stream — each agent emits `processing` then `done` (with its input + output) live, then a final `state` frame |
| `GET` | `/api/runs/{run_id}` | Current run state (all five agent findings, pending approval, receipts, outputs) |
| `POST` | `/api/runs/{run_id}/decision` | Submit `approve` / `deny` / `modify` and resume |
| `GET` | `/api/trades/{trade_id}` | Full DB detail behind a booked trade (trade, securities master, counterparties) |

The server keeps live workflow instances in memory keyed by `run_id`. The events
endpoint drives the workflow with `workflow.run(..., stream=True)` and translates
MAF `executor_invoked` / `executor_completed` events into SSE frames, so the UI
shows each agent working in real time rather than blocking until the whole
pipeline finishes; a suspended workflow is later resumed by the decision call.

**Frontend** — from `control-tower-agent/web/`:

```bash
npm install
npm run dev        # http://localhost:5173, proxies /api -> http://localhost:8000
```

The UI opens on the **open-dispute queue** (category, notional, filer, security,
evidence completeness). Selecting a dispute launches the pipeline and **streams
each agent live** — every card flips from *queued* → *processing* (spinner) →
*done* as its agent runs, and each finished agent exposes an expandable
**input / output** panel. The cards render Intake classification/severity,
Prediction risk score & driver, Reconstruction artifact grid + ledger-verified
status, Root-Cause break type / resolution / narrative, and Remediation proposal
+ drafted communication — followed by the **orchestrator approval** panel
(approve, override the resolution, or deny) and the **ledger outcome**
(ApprovalRecord + ACLReceipts).

Click the **dispute or trade ID** to open a detail modal comparing the
counterparty confirmation against the booked trade with the **broken field
highlighted** (the value the remediation adjusts), plus securities-master and
counterparty detail served by `GET /api/trades/{trade_id}`. The **Show agent
workflow** button opens a stepper visualizing the whole pipeline and
highlighting where the current run sits.
