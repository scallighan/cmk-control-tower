// Typed client for the CMK Control Tower FastAPI backend.

export interface FieldSuggestion {
  trade_id: string;
  field: string;
  current_value: string;
  suggested_value: string;
  reason: string;
}

export interface AssessedItem {
  trade_id: string;
  source: string;
  matched: boolean;
  summary: string;
  confirmation: Record<string, unknown>;
  trade: Record<string, unknown>;
  suggestions: FieldSuggestion[];
}

export interface PendingApproval {
  request_id: string;
  suggestions: FieldSuggestion[];
  summaries: Array<{ trade_id: string; summary: string }>;
}

export interface RunState {
  run_id: string;
  filename: string | null;
  created_at: string;
  status: "awaiting_approval" | "completed";
  items: AssessedItem[];
  unknown_trade_ids: string[];
  pending: PendingApproval[];
  outputs: string[];
}

export type DecisionAction = "approve" | "deny" | "modify";

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

export async function startRun(file: File): Promise<RunState> {
  const form = new FormData();
  form.append("file", file);
  const res = await fetch("/api/runs", { method: "POST", body: form });
  return handle<RunState>(res);
}

export async function submitDecision(
  runId: string,
  requestId: string,
  action: DecisionAction,
  modifiedSuggestions: FieldSuggestion[] = []
): Promise<RunState> {
  const res = await fetch(`/api/runs/${runId}/decision`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      request_id: requestId,
      action,
      modified_suggestions: modifiedSuggestions,
    }),
  });
  return handle<RunState>(res);
}

export async function getTradeDetail(tradeId: string): Promise<TradeDetail> {
  const res = await fetch(`/api/trades/${encodeURIComponent(tradeId)}`);
  return handle<TradeDetail>(res);
}
