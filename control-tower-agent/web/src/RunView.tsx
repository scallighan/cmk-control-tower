import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  ChatMessage,
  DecisionAction,
  DisputeSummary,
  DraftCommunication,
  IntakeResult,
  PredictionResult,
  ReconstructionResult,
  RemediationResult,
  RootCauseResult,
  RunState,
  StageEvent,
  StageName,
  getChat,
  sendChat,
} from "./api";
import TradeModal from "./TradeModal";

interface Props {
  dispute: DisputeSummary;
  run: RunState | null;
  stages: Record<string, StageEvent>;
  streaming: boolean;
  busy: boolean;
  error: string | null;
  resolutionOptions: string[];
  onBack: () => void;
  onDecide: (action: DecisionAction, finalResolution?: string, note?: string) => void;
  revisedStages: Set<string>;
  onRerun: (stage: string, feedback: string) => void;
}

type Phase = "pending" | "processing" | "done";

function num(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  return v.toLocaleString("en-US", { maximumFractionDigits: digits });
}

function usd(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  return v.toLocaleString("en-US", { style: "currency", currency: "USD" });
}

function sevClass(sev: string | undefined): string {
  const s = (sev ?? "").toLowerCase();
  if (s === "high" || s === "critical") return "risk-high";
  if (s === "medium") return "risk-medium";
  return "risk-low";
}

// Plain-text rendering of a drafted counterparty communication.
function emailText(c: DraftCommunication): string {
  return `To: ${c.to}\nSubject: ${c.subject}\n\n${c.body}`;
}

// Copies arbitrary text to the clipboard with a brief "Copied!" confirmation.
// Falls back to a hidden textarea for non-secure (http) contexts where the
// async Clipboard API is unavailable.
function CopyButton({ text, label = "Copy to clipboard" }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try {
        document.execCommand("copy");
      } finally {
        document.body.removeChild(ta);
      }
    }
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1500);
  };
  return (
    <button type="button" className="ghost sm" onClick={copy}>
      {copied ? "Copied!" : label}
    </button>
  );
}

function IODetails({
  io,
}: {
  io: { input?: unknown; output?: unknown } | undefined;
}) {
  if (!io || (io.input === undefined && io.output === undefined)) return null;
  return (
    <details className="issues io-details">
      <summary>input / output</summary>
      {io.input !== undefined && (
        <>
          <div className="io-label">input</div>
          <pre className="output">{JSON.stringify(io.input, null, 2)}</pre>
        </>
      )}
      {io.output !== undefined && (
        <>
          <div className="io-label">output</div>
          <pre className="output">{JSON.stringify(io.output, null, 2)}</pre>
        </>
      )}
    </details>
  );
}

function PhaseBadge({ phase, extra }: { phase: Phase; extra?: React.ReactNode }) {
  if (phase === "processing")
    return (
      <span className="badge badge-processing">
        <span className="spinner" aria-hidden /> processing
      </span>
    );
  if (phase === "pending") return <span className="badge badge-idle">queued</span>;
  return <>{extra ?? <span className="badge badge-ok">done</span>}</>;
}

function StageCard({
  step,
  title,
  phase,
  status,
  io,
  revised,
  children,
}: {
  step: string;
  title: string;
  phase: Phase;
  status?: React.ReactNode;
  io?: { input?: unknown; output?: unknown };
  revised?: boolean;
  children?: React.ReactNode;
}) {
  return (
    <div className={`agent-card phase-${phase}${revised ? " revised" : ""}`}>
      <div className="agent-card-head">
        <span className="agent-step">{step}</span>
        <h3>{title}</h3>
        {revised && phase !== "processing" && <span className="badge badge-revised">revised</span>}
        <PhaseBadge phase={phase} extra={phase === "done" ? status : undefined} />
      </div>
      {phase === "processing" && (
        <div className="agent-card-body muted">{revised ? "Re-running…" : "Working…"}</div>
      )}
      {phase === "done" && (
        <div className="agent-card-body">
          {children}
          <IODetails io={io} />
        </div>
      )}
    </div>
  );
}

// Conversational review assistant for the dispute currently at the approval
// gate. Renders as a floating widget pinned to the lower-right corner: a
// launcher button that opens a popup chat window. Restores its transcript from
// the server on mount.
function ChatPanel({
  runId,
  onRerun,
}: {
  runId: string;
  onRerun: (stage: string, feedback: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const logRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    let active = true;
    getChat(runId)
      .then((h) => active && setMessages(h))
      .catch(() => {});
    return () => {
      active = false;
    };
  }, [runId]);

  useEffect(() => {
    if (open) logRef.current?.scrollTo({ top: logRef.current.scrollHeight });
  }, [messages, sending, open]);

  async function send(text: string) {
    const msg = text.trim();
    if (!msg || sending) return;
    setSending(true);
    setErr(null);
    setInput("");
    setMessages((m) => [...m, { role: "user", content: msg }]);
    try {
      const res = await sendChat(runId, msg);
      setMessages(res.history);
      // The assistant requested a step rerun via its tool — drive it in the main view.
      if (res.rerun) onRerun(res.rerun.stage, res.rerun.feedback);
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setSending(false);
    }
  }

  const suggestions = [
    "Summarize what each step concluded.",
    "Why this recommended resolution?",
    "What are the biggest risks if I approve?",
    "Is the ledger proof trustworthy here?",
    "Re-run the root cause treating this as a fee break, not economic.",
  ];

  if (!open) {
    return (
      <button
        type="button"
        className="chat-fab"
        onClick={() => setOpen(true)}
        aria-label="Open review assistant"
      >
        <span className="chat-fab-icon" aria-hidden>
          💬
        </span>
        Review assistant
      </button>
    );
  }

  return (
    <div className="chat-widget" role="dialog" aria-label="Review assistant">
      <div className="chat-widget-head">
        <div>
          <strong>Review assistant</strong>
          <span className="badge badge-cat">ask about this dispute</span>
        </div>
        <button
          type="button"
          className="chat-close"
          onClick={() => setOpen(false)}
          aria-label="Minimize review assistant"
        >
          ×
        </button>
      </div>
      <div className="chat-log" ref={logRef}>
        {messages.length === 0 && !sending && (
          <div className="chat-empty muted">
            Ask about what each step did, probe the evidence, or pressure-test the proposal before
            you decide. The assistant sees the full run and can query the read-only ledger.
          </div>
        )}
        {messages.map((m, i) => (
          <div key={i} className={`chat-msg chat-${m.role}`}>
            <span className="chat-role">{m.role === "user" ? "You" : "Assistant"}</span>
            {m.role === "assistant" ? (
              <div className="chat-bubble chat-markdown">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{m.content}</ReactMarkdown>
              </div>
            ) : (
              <div className="chat-bubble">{m.content}</div>
            )}
          </div>
        ))}
        {sending && (
          <div className="chat-msg chat-assistant">
            <span className="chat-role">Assistant</span>
            <div className="chat-bubble muted">
              <span className="spinner" aria-hidden /> thinking…
            </div>
          </div>
        )}
      </div>
      {messages.length === 0 && (
        <div className="chat-suggestions">
          {suggestions.map((s) => (
            <button key={s} type="button" className="ghost sm" disabled={sending} onClick={() => send(s)}>
              {s}
            </button>
          ))}
        </div>
      )}
      {err && <div className="error">⚠ {err}</div>}
      <form
        className="chat-input-row"
        onSubmit={(e) => {
          e.preventDefault();
          send(input);
        }}
      >
        <input
          type="text"
          value={input}
          placeholder="Ask about this dispute…"
          disabled={sending}
          onChange={(e) => setInput(e.target.value)}
        />
        <button type="submit" className="primary" disabled={sending || !input.trim()}>
          Send
        </button>
      </form>
    </div>
  );
}

export default function RunView({
  dispute,
  run,
  stages,
  streaming,
  busy,
  error,
  resolutionOptions,
  onBack,
  onDecide,
  revisedStages,
  onRerun,
}: Props) {
  const [showTrade, setShowTrade] = useState(false);
  const pending = run?.pending[0] ?? null;
  const req = pending?.request ?? null;
  const [resolution, setResolution] = useState<string>("");
  const [note, setNote] = useState<string>("");

  const brokenField = run?.derived?.economics.broken_field ?? null;

  // Data for a stage comes from the live stream output, falling back to the
  // final RunState typed fields once the run settles.
  function stageIO(name: StageName): { input?: unknown; output?: unknown } | undefined {
    if (stages[name]) return { input: stages[name].input, output: stages[name].output };
    return run?.stage_io?.[name];
  }
  function phaseOf(name: StageName, hasData: boolean): Phase {
    const s = stages[name];
    if (s) return s.phase === "done" ? "done" : "processing";
    return hasData ? "done" : "pending";
  }
  function output<T>(name: StageName, runVal: T | null | undefined): T | undefined {
    // Prefer the live streamed stage output so a rerun re-renders each card as
    // soon as that stage finishes. Falling back to runVal keeps stages populated
    // when viewing a settled run that wasn't streamed (output taken from RunState).
    return ((stages[name]?.output as T | undefined) ?? runVal) ?? undefined;
  }

  const intake = output<IntakeResult>("intake", run?.intake);
  const prediction = output<PredictionResult>("prediction", run?.prediction);
  const reconstruction = output<ReconstructionResult>("reconstruction", run?.reconstruction);
  const rootCause = output<RootCauseResult>("root_cause", run?.root_cause);
  const remediation = output<RemediationResult>("remediation", run?.remediation);

  return (
    <>
      <section className="panel">
        <div className="run-head">
          <button className="ghost sm" onClick={onBack}>
            ← Queue
          </button>
          <h2 style={{ margin: 0 }}>
            <button className="linklike big" onClick={() => setShowTrade(true)}>
              {dispute.dispute_id}
            </button>
            <span className="badge badge-cat">{dispute.category}</span>
            {streaming ? (
              <span className="badge badge-processing">
                <span className="spinner" aria-hidden /> agents running
              </span>
            ) : run ? (
              <span className={`badge ${run.status === "completed" ? "badge-ok" : "badge-warn"}`}>
                {run.status === "completed" ? "completed" : "awaiting approval"}
              </span>
            ) : null}
          </h2>
        </div>
        <p className="muted">
          Trade{" "}
          <button className="linklike" onClick={() => setShowTrade(true)}>
            {dispute.trade_id}
          </button>{" "}
          · filed by {dispute.filer_name ?? dispute.filer_cp_id} · {dispute.ticker ?? "—"} ({dispute.side})
        </p>
        {error && <div className="error">⚠ {error}</div>}
      </section>

      <section className="agent-grid">
        {/* Context load */}
        <StageCard
          step="0"
          title="Load dispute context"
          phase={phaseOf("context", !!(run?.dispute || stages.context))}
          io={stageIO("context")}
          status={<span className="badge badge-ok">loaded</span>}
        >
          <p className="muted">Full trade lifecycle assembled from the SQL ledger (read-only).</p>
        </StageCard>

        {/* Intake */}
        <StageCard
          step="1"
          title="Intake Agent"
          phase={phaseOf("intake", !!intake)}
          revised={revisedStages.has("intake")}
          io={stageIO("intake")}
          status={intake && <span className={`badge ${sevClass(intake.severity)}`}>{intake.severity}</span>}
        >
          {intake && (
            <>
              <p>
                <b>Classification:</b> {intake.classification}
              </p>
              <p>
                <b>Evidence:</b> { intake.evidence_completeness_pct < 1 ? Math.round(intake.evidence_completeness_pct * 100) : intake.evidence_completeness_pct}% complete
              </p>
              <p className="muted">{intake.routing_notes}</p>
            </>
          )}
        </StageCard>

        {/* Prediction */}
        <StageCard
          step="2"
          title="Prediction Agent"
          phase={phaseOf("prediction", !!prediction)}
          revised={revisedStages.has("prediction")}
          io={stageIO("prediction")}
          status={
            prediction && (
              <span
                className={`badge risk-${
                  prediction.pre_cutoff_risk_score >= 1
                    ? "high"
                    : prediction.pre_cutoff_risk_score >= 0.5
                    ? "medium"
                    : "low"
                }`}
              >
                risk {num(prediction.pre_cutoff_risk_score)}
              </span>
            )
          }
        >
          {prediction && (
            <>
              <p>
                <b>Primary driver:</b> {prediction.primary_risk_driver}
              </p>
              <p>
                <b>Time sensitivity:</b> {prediction.time_sensitivity}
              </p>
              <ul className="finding">
                <li>timing breach: {String(prediction.signal_breakdown.timing_breach_flag)}</li>
                <li>affirm status: {prediction.signal_breakdown.affirm_status || "—"}</li>
                <li>ssi mismatch prob: {num(prediction.signal_breakdown.ssi_mismatch_prob)}</li>
                <li>cp fail propensity: {num(prediction.signal_breakdown.cp_fail_propensity)}</li>
                <li>liquidity stress: {num(prediction.signal_breakdown.liquidity_stress_score)}</li>
              </ul>
            </>
          )}
        </StageCard>

        {/* Reconstruction */}
        <StageCard
          step="3"
          title="Reconstruction / Evidence Agent"
          phase={phaseOf("reconstruction", !!reconstruction)}
          revised={revisedStages.has("reconstruction")}
          io={stageIO("reconstruction")}
          status={
            reconstruction && (
              <span className={`badge ${reconstruction.ledger_verified ? "badge-ok" : "badge-warn"}`}>
                {reconstruction.ledger_verified ? "ledger verified" : "unverified"}
              </span>
            )
          }
        >
          {reconstruction && (
            <>
              <p>
                <b>Completeness:</b> {reconstruction.evidence_completeness_pct < 1 ? Math.round(reconstruction.evidence_completeness_pct * 100) : reconstruction.evidence_completeness_pct}% ·{" "}
                <b>proof:</b> {num(reconstruction.proof_integrity_score)}
              </p>
              <div className="artifact-grid">
                {Object.entries(reconstruction.artifacts).map(([k, v]) => (
                  <span key={k} className={`artifact ${v ? "have" : "miss"}`}>
                    {v ? "✓" : "✗"} {k}
                  </span>
                ))}
              </div>
              {reconstruction.gaps.length > 0 && (
                <p className="muted">Gaps: {reconstruction.gaps.join(", ")}</p>
              )}
            </>
          )}
        </StageCard>

        {/* Root cause */}
        <StageCard
          step="4"
          title="Root-Cause Agent"
          phase={phaseOf("root_cause", !!rootCause)}
          revised={revisedStages.has("root_cause")}
          io={stageIO("root_cause")}
          status={rootCause && <span className="badge risk-high">conf {num(rootCause.confidence)}</span>}
        >
          {rootCause && (
            <>
              <p>
                <b>Break type:</b> {rootCause.primary_break_type} · <b>resolution:</b>{" "}
                {rootCause.recommended_resolution}
              </p>
              {rootCause.break_details.broken_field && (
                <ul className="finding">
                  <li>field: {rootCause.break_details.broken_field}</li>
                  <li>break amount: {usd(rootCause.break_details.break_amount)}</li>
                  <li>responsible: {rootCause.break_details.responsible_party}</li>
                </ul>
              )}
              <p className="muted">{rootCause.root_cause_narrative}</p>
            </>
          )}
        </StageCard>

        {/* Remediation */}
        <StageCard
          step="5"
          title="Remediation Agent"
          phase={phaseOf("remediation", !!remediation)}
          revised={revisedStages.has("remediation")}
          io={stageIO("remediation")}
          status={remediation && <span className="badge badge-warn">{remediation.proposed_action}</span>}
        >
          {remediation && (
            <>
              <p>
                <b>Proposed:</b> {remediation.proposed_action}
                {remediation.proposed_amount != null && ` · ${usd(remediation.proposed_amount)}`}
              </p>
              {remediation.regulatory_cost_if_unresolved != null && (
                <p className="muted">
                  Regulatory cost if unresolved: {usd(remediation.regulatory_cost_if_unresolved)}
                </p>
              )}
              <details className="issues">
                <summary>Draft communication</summary>
                <div className="draft-email">
                  <p className="mono">
                    <b>To:</b> {remediation.draft_communication.to}
                    <br />
                    <b>Subject:</b> {remediation.draft_communication.subject}
                  </p>
                  <p className="draft-body">{remediation.draft_communication.body}</p>
                </div>
                <div className="draft-actions">
                  <CopyButton text={emailText(remediation.draft_communication)} />
                </div>
              </details>
            </>
          )}
        </StageCard>
      </section>

      {/* Floating conversational review assistant (lower-right corner) */}
      {run && <ChatPanel runId={run.run_id} onRerun={onRerun} />}

      {/* HITL approval gate */}
      {req && pending && (
        <section className="panel review">
          <h2>
            Orchestrator · human approval
            <span className="badge badge-warn">action required</span>
          </h2>
          <p>
            The orchestrator proposes <b>{req.proposed_action}</b>
            {req.proposed_amount != null && (
              <>
                {" "}
                of <b>{usd(req.proposed_amount)}</b>
              </>
            )}{" "}
            on {req.dispute_id}. Approver role: <b>{req.approver_role}</b>
            {req.requires_dual_approval && (
              <span className="badge badge-warn" style={{ marginLeft: 8 }}>
                dual approval
              </span>
            )}
            .
          </p>
          <p className="muted">{req.hitl_summary}</p>

          <div className="decision-controls">
            <label>
              Final resolution (optional override)
              <select value={resolution} onChange={(e) => setResolution(e.target.value)}>
                <option value="">— use proposed —</option>
                {resolutionOptions.map((r) => (
                  <option key={r} value={r}>
                    {r}
                  </option>
                ))}
              </select>
            </label>
            <label className="grow">
              Note
              <input
                type="text"
                value={note}
                placeholder="rationale (optional)"
                onChange={(e) => setNote(e.target.value)}
              />
            </label>
          </div>

          <div className="actions">
            <button
              className="approve"
              disabled={busy}
              onClick={() => onDecide("approve", resolution || undefined, note)}
            >
              Approve
            </button>
            <button
              className="modify"
              disabled={busy || !resolution}
              onClick={() => onDecide("modify", resolution || undefined, note)}
            >
              Approve with modified resolution
            </button>
            <button className="deny" disabled={busy} onClick={() => onDecide("deny", undefined, note)}>
              Deny
            </button>
          </div>
        </section>
      )}

      {/* Ledger outcome */}
      {run && run.status === "completed" && run.approval && (
        <section className="panel">
          <h2>
            Ledger outcome
            <span className="badge badge-ok">committed (simulated ACL)</span>
          </h2>
          <p>
            Decision <b>{run.approval.action}</b> by {run.approval.approver}
            {run.approval.resolution && (
              <>
                {" "}
                · resolution <b>{run.approval.resolution}</b>
              </>
            )}
            {run.approval.note && <> · “{run.approval.note}”</>}
          </p>
          {run.receipts.length > 0 && (
            <table className="detail-table">
              <tbody>
                {run.receipts.map((r, i) => (
                  <tr key={i}>
                    <th>{r.artifact}</th>
                    <td className="mono">
                      txn {r.transaction_id} · {r.digest_hash.slice(0, 24)}…
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {run.outputs.map((o, i) => (
            <pre key={i} className="output">
              {o}
            </pre>
          ))}
        </section>
      )}

      {showTrade && (
        <TradeModal
          disputeId={dispute.dispute_id}
          tradeId={dispute.trade_id}
          confirmation={run?.confirmation ?? (stages.context?.output as any)?.confirmation ?? null}
          derived={run?.derived ?? (stages.context?.output as any)?.derived ?? null}
          brokenField={brokenField ?? (stages.context?.output as any)?.derived?.economics?.broken_field ?? null}
          onClose={() => setShowTrade(false)}
        />
      )}
    </>
  );
}
