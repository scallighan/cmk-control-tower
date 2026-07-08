// Typed client for the CMK Control Tower FastAPI backend.
//
// The UI lists OPEN disputes from the SQL ledger; selecting one starts a
// Microsoft Agent Framework run whose five specialist agents produce structured
// findings and suspend at the human-in-the-loop approval gate.

export interface DisputeSummary {
  dispute_id: string;
  trade_id: string;
  category: string;
  status: string;
  notional_usd: string | number | null;
  filer_cp_id: string | null;
  cp_buy_id: string | null;
  cp_sell_id: string | null;
  opened_ts_utc: string | null;
  resolution: string | null;
  buy_name: string | null;
  sell_name: string | null;
  filer_name: string | null;
  completeness_pct: string | number | null;
  side: string | null;
  ccy: string | null;
  ticker: string | null;
  cusip: string | null;
}

// --- Agent finding shapes (mirror control-tower-agent/agents.py) ------------

export interface IntakeEntities {
  trade_id: string | null;
  counterparty_id: string | null;
  security_id: string | null;
  settlement_date: string | null;
  notional_usd: number | null;
}

export interface IntakeResult {
  dispute_id: string;
  classification: string;
  severity: string;
  entities: IntakeEntities;
  evidence_completeness_pct: number;
  recommended_agents: string[];
  routing_notes: string;
}

export interface PredictionSignals {
  timing_breach_flag: boolean;
  affirm_status: string;
  ssi_mismatch_prob: number;
  cp_fail_propensity: number;
  liquidity_stress_score: number;
}

export interface PredictionResult {
  trade_id: string;
  pre_cutoff_risk_score: number;
  primary_risk_driver: string;
  time_sensitivity: string;
  signal_breakdown: PredictionSignals;
  recommended_actions: string[];
  confidence: number;
}

export interface EvidenceArtifacts {
  confirmation: boolean;
  affirmation: boolean;
  settlement_instruction: boolean;
  settlement_status: boolean;
  ssi_snapshot: boolean;
  evidence_pack: boolean;
  communications: boolean;
}

export interface ReconstructionResult {
  dispute_id: string;
  evidence_completeness_pct: number;
  artifacts: EvidenceArtifacts;
  gaps: string[];
  ledger_verified: boolean;
  proof_integrity_score: number;
  acl_lag_minutes: number | null;
  ssi_freshness_days: number | null;
  reconstruction_notes: string;
}

export interface BreakDetails {
  broken_field: string | null;
  booked_value: string | null;
  confirmed_value: string | null;
  break_amount: number | null;
  responsible_party: string | null;
}

export interface RootCauseResult {
  dispute_id: string;
  primary_break_type: string;
  confidence: number;
  break_details: BreakDetails;
  recommended_resolution: string;
  requires_hitl: boolean;
  root_cause_narrative: string;
}

export interface DraftCommunication {
  to: string;
  subject: string;
  body: string;
}

export interface RemediationResult {
  dispute_id: string;
  proposed_action: string;
  proposed_amount: number | null;
  regulatory_cost_if_unresolved: number | null;
  urgency_hours: number | null;
  draft_communication: DraftCommunication;
  hitl_summary: string;
  hitl_required: boolean;
  approval_deadline_utc?: string;
  approver_role: string;
}

// --- HITL request / ledger artifacts ---------------------------------------

export interface DisputeApprovalRequest {
  dispute_id: string;
  category: string;
  proposed_action: string;
  proposed_amount: number | null;
  approver_role: string;
  requires_dual_approval: boolean;
  hitl_summary: string;
  draft_communication: DraftCommunication;
  root_cause_narrative: string;
}

export interface PendingApproval {
  request_id: string;
  request: DisputeApprovalRequest;
}

export interface ApprovalRecord {
  approval_id: string;
  dispute_id: string;
  action: string;
  approver: string;
  resolution: string | null;
  note: string;
  decided_at: string;
}

export interface ACLReceipt {
  dispute_id: string;
  artifact: string;
  digest_hash: string;
  transaction_id: string;
  simulated: boolean;
}

export interface DerivedSignals {
  economics: {
    broken_field: string | null;
    confirm_status: string | null;
    price_break_amount: number | null;
    qty_break_amount: number | null;
    gross_break_amount: number | null;
    has_economic_break: boolean;
  };
  ssi: Record<string, unknown>;
  timing: Record<string, unknown>;
  settlement_status: Record<string, unknown> | null;
  cp_profile: Record<string, unknown> | null;
}

export type RunStatus = "running" | "awaiting_approval" | "completed";

export interface RunState {
  run_id: string;
  dispute_id: string;
  created_at: string;
  status: RunStatus;
  dispute: Record<string, unknown> | null;
  trade: Record<string, unknown> | null;
  confirmation: Record<string, unknown> | null;
  derived: DerivedSignals | null;
  intake: IntakeResult | null;
  prediction: PredictionResult | null;
  reconstruction: ReconstructionResult | null;
  root_cause: RootCauseResult | null;
  remediation: RemediationResult | null;
  stage_io: Record<string, { input: unknown; output: unknown }>;
  pending: PendingApproval[];
  approval: ApprovalRecord | null;
  receipts: ACLReceipt[];
  outputs: string[];
}

// Pipeline stage identifiers, in execution order, as streamed by the server.
export type StageName =
  | "context"
  | "intake"
  | "prediction"
  | "reconstruction"
  | "root_cause"
  | "remediation"
  | "orchestrator";

export interface StageEvent {
  stage: StageName;
  phase: "processing" | "done";
  input?: unknown;
  output?: unknown;
  // Set on frames from a rerun stream so the UI can highlight the revised steps.
  revised?: boolean;
}

export interface DisputeContext {
  [key: string]: unknown;
  derived: DerivedSignals;
}

export type DecisionAction = "approve" | "deny" | "modify";

// Conversational review assistant.
export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
}

export interface ChatResponse {
  reply: string;
  history: ChatMessage[];
  // Present when the assistant asked to re-run a step — the UI drives the live rerun.
  rerun?: RerunSignal;
}

export interface RerunSignal {
  stage: string;
  feedback: string;
}

export interface TradeDetail {
  trade: Record<string, unknown> | null;
  security: Record<string, unknown> | null;
  counterparty_buy: Record<string, unknown> | null;
  counterparty_sell: Record<string, unknown> | null;
}

async function handle<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? detail;
    } catch {
      /* ignore */
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

export async function listDisputes(limit = 200): Promise<DisputeSummary[]> {
  const res = await fetch(`/api/disputes?limit=${limit}`);
  return handle<DisputeSummary[]>(res);
}

export async function getDispute(disputeId: string): Promise<DisputeContext> {
  const res = await fetch(`/api/disputes/${encodeURIComponent(disputeId)}`);
  return handle<DisputeContext>(res);
}

export async function startRun(disputeId: string): Promise<RunState> {
  const res = await fetch(`/api/disputes/${encodeURIComponent(disputeId)}/runs`, {
    method: "POST",
  });
  return handle<RunState>(res);
}

export interface RunStreamHandlers {
  onStage: (event: StageEvent) => void;
  onState: (state: RunState) => void;
  onError: (message: string) => void;
}

/**
 * Opens the SSE stream for a run and dispatches stage/state/error frames.
 * Returns a disposer that closes the underlying EventSource.
 */
export function openRunStream(runId: string, handlers: RunStreamHandlers): () => void {
  const es = new EventSource(`/api/runs/${encodeURIComponent(runId)}/events`);
  let closed = false;
  const close = () => {
    if (!closed) {
      closed = true;
      es.close();
    }
  };

  es.addEventListener("stage", (e) => {
    handlers.onStage(JSON.parse((e as MessageEvent).data) as StageEvent);
  });
  es.addEventListener("state", (e) => {
    handlers.onState(JSON.parse((e as MessageEvent).data) as RunState);
    close(); // terminal frame — stop before EventSource auto-reconnects
  });
  es.addEventListener("run_error", (e) => {
    handlers.onError((JSON.parse((e as MessageEvent).data) as { message: string }).message);
    close();
  });
  es.onerror = () => {
    if (!closed) {
      handlers.onError("Lost connection to the agent stream.");
      close();
    }
  };

  return close;
}

/**
 * Opens an SSE stream that re-runs `stage` (and every downstream stage) live,
 * dispatching the same stage/state/error frames as {@link openRunStream} so the
 * caller can reuse its pipeline-card update path. Stage frames carry `revised`.
 * Returns a disposer that closes the underlying EventSource.
 */
export function openRerunStream(
  runId: string,
  stage: string,
  feedback: string,
  handlers: RunStreamHandlers
): () => void {
  const qs = new URLSearchParams({ stage, feedback });
  const es = new EventSource(`/api/runs/${encodeURIComponent(runId)}/rerun/stream?${qs}`);
  let closed = false;
  const close = () => {
    if (!closed) {
      closed = true;
      es.close();
    }
  };

  es.addEventListener("stage", (e) => {
    handlers.onStage(JSON.parse((e as MessageEvent).data) as StageEvent);
  });
  es.addEventListener("state", (e) => {
    handlers.onState(JSON.parse((e as MessageEvent).data) as RunState);
    close(); // terminal frame — stop before EventSource auto-reconnects
  });
  es.addEventListener("run_error", (e) => {
    handlers.onError((JSON.parse((e as MessageEvent).data) as { message: string }).message);
    close();
  });
  es.onerror = () => {
    if (!closed) {
      handlers.onError("Lost connection to the rerun stream.");
      close();
    }
  };

  return close;
}

export async function getRun(runId: string): Promise<RunState> {
  const res = await fetch(`/api/runs/${encodeURIComponent(runId)}`);
  return handle<RunState>(res);
}

export async function submitDecision(
  runId: string,
  requestId: string,
  action: DecisionAction,
  finalResolution?: string,
  note = ""
): Promise<RunState> {
  const res = await fetch(`/api/runs/${encodeURIComponent(runId)}/decision`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      request_id: requestId,
      action,
      final_resolution: finalResolution ?? null,
      note,
    }),
  });
  return handle<RunState>(res);
}

export async function getTradeDetail(tradeId: string): Promise<TradeDetail> {
  const res = await fetch(`/api/trades/${encodeURIComponent(tradeId)}`);
  return handle<TradeDetail>(res);
}

/** Full chat transcript for a run (used to restore the conversation on reload). */
export async function getChat(runId: string): Promise<ChatMessage[]> {
  const res = await fetch(`/api/runs/${encodeURIComponent(runId)}/chat`);
  return handle<ChatMessage[]>(res);
}

/** Send a message to the review assistant; returns the reply + full history. */
export async function sendChat(runId: string, message: string): Promise<ChatResponse> {
  const res = await fetch(`/api/runs/${encodeURIComponent(runId)}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  });
  return handle<ChatResponse>(res);
}
