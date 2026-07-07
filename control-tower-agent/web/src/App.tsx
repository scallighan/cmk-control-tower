import { useMemo, useRef, useState } from "react";
import {
  AssessedItem,
  DecisionAction,
  FieldSuggestion,
  PendingApproval,
  RunState,
  startRun,
  submitDecision,
} from "./api";
import ConfirmationModal from "./ConfirmationModal";
import WorkflowModal from "./WorkflowModal";

interface EditableSuggestion extends FieldSuggestion {
  dropped: boolean;
}

export default function App() {
  const [file, setFile] = useState<File | null>(null);
  const [run, setRun] = useState<RunState | null>(null);
  const [edits, setEdits] = useState<EditableSuggestion[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [detailId, setDetailId] = useState<string | null>(null);
  const [showWorkflow, setShowWorkflow] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  const pending: PendingApproval | undefined = run?.pending[0];

  const detailItem = useMemo(
    () => run?.items.find((i) => i.trade_id === detailId) ?? null,
    [run, detailId]
  );

  function loadEdits(approval: PendingApproval | undefined) {
    setEdits((approval?.suggestions ?? []).map((s) => ({ ...s, dropped: false })));
  }

  async function onStart() {
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      const state = await startRun(file);
      setRun(state);
      loadEdits(state.pending[0]);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function onDecision(action: DecisionAction) {
    if (!run || !pending) return;
    setBusy(true);
    setError(null);
    try {
      const modified: FieldSuggestion[] =
        action === "modify"
          ? edits.filter((e) => !e.dropped).map(({ dropped: _dropped, ...s }) => s)
          : [];
      const state = await submitDecision(run.run_id, pending.request_id, action, modified);
      setRun(state);
      loadEdits(state.pending[0]);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function updateEdit(index: number, value: string) {
    setEdits((prev) => prev.map((e, i) => (i === index ? { ...e, suggested_value: value } : e)));
  }

  function toggleDrop(index: number) {
    setEdits((prev) => prev.map((e, i) => (i === index ? { ...e, dropped: !e.dropped } : e)));
  }

  function reset() {
    setRun(null);
    setEdits([]);
    setFile(null);
    setError(null);
    if (fileInput.current) fileInput.current.value = "";
  }

  const matched = run?.items.filter((i) => i.matched) ?? [];
  const broken = run?.items.filter((i) => !i.matched) ?? [];

  return (
    <div className="page">
      <header>
        <h1>CMK Control Tower</h1>
        <p className="subtitle">
          Upload counterparty confirmations · an agent reconciles them against the booked ledger trades
        </p>
        <button className="ghost" onClick={() => setShowWorkflow(true)}>
          Show agent workflow
        </button>
      </header>

      <section className="controls">
        <label className="file-picker">
          <input
            ref={fileInput}
            type="file"
            accept=".csv,text/csv"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            disabled={busy || !!run}
          />
          <span>{file ? file.name : "Choose confirmations CSV…"}</span>
        </label>
        <button onClick={onStart} disabled={busy || !file || !!run}>
          {busy && !run ? "Reconciling…" : "Reconcile confirmations"}
        </button>
        {run && <span className="run-id">run: {run.run_id}</span>}
      </section>

      {error && <div className="error">⚠ {error}</div>}

      {run && (
        <section className="panel">
          <h2>
            Agent assessment
            <span className="badge badge-ok">{matched.length} matched</span>
            <span className="badge badge-warn">{broken.length} break(s)</span>
          </h2>
          <table>
            <thead>
              <tr>
                <th>Trade</th>
                <th>Source</th>
                <th>Verdict</th>
                <th>cfm_price</th>
                <th>cfm_qty</th>
                <th>cfm_gross</th>
                <th>Agent summary</th>
              </tr>
            </thead>
            <tbody>
              {run.items.map((item: AssessedItem) => (
                <tr key={item.trade_id} className={item.matched ? "" : "row-break"}>
                  <td>
                    <button className="link" onClick={() => setDetailId(item.trade_id)}>
                      {item.trade_id}
                    </button>
                  </td>
                  <td>{item.source}</td>
                  <td>
                    <span className={`badge ${item.matched ? "badge-ok" : "badge-warn"}`}>
                      {item.matched ? "matched" : "break"}
                    </span>
                  </td>
                  <td className="mono">{String(item.confirmation.cfm_price ?? "")}</td>
                  <td className="mono">{String(item.confirmation.cfm_qty ?? "")}</td>
                  <td className="mono">{String(item.confirmation.cfm_gross ?? "")}</td>
                  <td className="reason">{item.summary}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {run.unknown_trade_ids.length > 0 && (
            <p className="muted">
              Skipped {run.unknown_trade_ids.length} row(s) with no booked trade:{" "}
              {run.unknown_trade_ids.join(", ")}
            </p>
          )}
        </section>
      )}

      {run && pending && (
        <section className="panel">
          <h2>
            Human review required
            <span className="badge badge-warn">{pending.suggestions.length} proposed correction(s)</span>
          </h2>

          {pending.summaries.length > 0 && (
            <ul className="break-summary">
              {pending.summaries.map((s) => (
                <li key={s.trade_id}>
                  <button className="link" onClick={() => setDetailId(s.trade_id)}>
                    {s.trade_id}
                  </button>{" "}
                  — {s.summary}
                </li>
              ))}
            </ul>
          )}

          {pending.suggestions.length === 0 ? (
            <p>The agent could not propose any corrections. You can only deny.</p>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Trade</th>
                  <th>Field</th>
                  <th>Current</th>
                  <th>Suggested (editable)</th>
                  <th>Reason</th>
                  <th>Drop</th>
                </tr>
              </thead>
              <tbody>
                {edits.map((s, i) => (
                  <tr key={`${s.trade_id}-${s.field}`} className={s.dropped ? "dropped" : ""}>
                    <td>
                      <button className="link" onClick={() => setDetailId(s.trade_id)}>
                        {s.trade_id}
                      </button>
                    </td>
                    <td><code>{s.field}</code></td>
                    <td className="mono">{s.current_value}</td>
                    <td>
                      <input
                        className="mono"
                        value={s.suggested_value}
                        onChange={(e) => updateEdit(i, e.target.value)}
                        disabled={busy || s.dropped}
                      />
                    </td>
                    <td className="reason">{s.reason}</td>
                    <td>
                      <input
                        type="checkbox"
                        checked={s.dropped}
                        onChange={() => toggleDrop(i)}
                        disabled={busy}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <div className="actions">
            <button className="approve" onClick={() => onDecision("approve")} disabled={busy}>
              Approve as suggested
            </button>
            <button
              className="modify"
              onClick={() => onDecision("modify")}
              disabled={busy || pending.suggestions.length === 0}
            >
              Apply my edits
            </button>
            <button className="deny" onClick={() => onDecision("deny")} disabled={busy}>
              Deny all
            </button>
          </div>
        </section>
      )}

      {run && run.status === "completed" && (
        <section className="panel">
          <h2>
            Reconciliation complete
            <span className="badge badge-ok">done</span>
          </h2>
          {run.outputs.length === 0 ? (
            <p>No output produced.</p>
          ) : (
            run.outputs.map((out, i) => <pre key={i} className="output">{out}</pre>)
          )}
          <button onClick={reset}>Start another run</button>
        </section>
      )}

      {detailItem && (
        <ConfirmationModal item={detailItem} onClose={() => setDetailId(null)} />
      )}

      {showWorkflow && <WorkflowModal run={run} onClose={() => setShowWorkflow(false)} />}
    </div>
  );
}
