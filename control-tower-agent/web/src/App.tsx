import { useEffect, useMemo, useRef, useState } from "react";
import {
  DecisionAction,
  DisputeSummary,
  RunState,
  StageEvent,
  listDisputes,
  openRerunStream,
  openRunStream,
  startRun,
  submitDecision,
} from "./api";
import RunView from "./RunView";
import WorkflowModal from "./WorkflowModal";

function fmtUsd(v: string | number | null | undefined): string {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return String(v);
  return n.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
}

function fmtPct(v: string | number | null | undefined): string {
  if (v === null || v === undefined || v === "") return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return String(v);
  return `${Math.round(n * 100)}%`;
}

const RESOLUTION_OPTIONS = [
  "ADJUSTED",
  "REBOOKED",
  "CLAIMED",
  "WRITTEN_OFF",
  "BUY_IN",
  "NO_ACTION",
];

export default function App() {
  const [disputes, setDisputes] = useState<DisputeSummary[]>([]);
  const [loadingList, setLoadingList] = useState(true);
  const [listError, setListError] = useState<string | null>(null);

  const [run, setRun] = useState<RunState | null>(null);
  const [activeDispute, setActiveDispute] = useState<DisputeSummary | null>(null);
  const [stages, setStages] = useState<Record<string, StageEvent>>({});
  const [streaming, setStreaming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [revisedStages, setRevisedStages] = useState<Set<string>>(new Set());
  const [runError, setRunError] = useState<string | null>(null);
  const [showWorkflow, setShowWorkflow] = useState(false);
  const streamCloser = useRef<(() => void) | null>(null);

  useEffect(() => {
    let active = true;
    listDisputes()
      .then((d) => active && setDisputes(d))
      .catch((e) => active && setListError((e as Error).message))
      .finally(() => active && setLoadingList(false));
    return () => {
      active = false;
    };
  }, []);

  // Tear down any live stream on unmount.
  useEffect(() => () => streamCloser.current?.(), []);

  const filerCounts = useMemo(() => {
    const m = new Map<string, number>();
    disputes.forEach((d) => m.set(d.category, (m.get(d.category) ?? 0) + 1));
    return m;
  }, [disputes]);

  async function openDispute(d: DisputeSummary) {
    streamCloser.current?.();
    setActiveDispute(d);
    setRun(null);
    setStages({});
    setRevisedStages(new Set());
    setRunError(null);
    setStreaming(true);
    try {
      const created = await startRun(d.dispute_id);
      streamCloser.current = openRunStream(created.run_id, {
        onStage: (ev) =>
          setStages((prev) => ({
            ...prev,
            [ev.stage]:
              ev.phase === "done"
                ? ev
                : // keep any previously captured input/output while re-processing
                  { ...prev[ev.stage], ...ev },
          })),
        onState: (state) => {
          setRun(state);
          setStreaming(false);
        },
        onError: (message) => {
          setRunError(message);
          setStreaming(false);
        },
      });
    } catch (e) {
      setRunError((e as Error).message);
      setStreaming(false);
    }
  }

  async function decide(action: DecisionAction, finalResolution?: string, note = "") {
    if (!run || run.pending.length === 0) return;
    const pending = run.pending[0];
    setBusy(true);
    setRunError(null);
    try {
      setRun(await submitDecision(run.run_id, pending.request_id, action, finalResolution, note));
    } catch (e) {
      setRunError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  // Kick off a live, streamed rerun of a step (and everything downstream) that the
  // Review Assistant requested. Reuses the pipeline-card update path so the affected
  // agents visibly re-process, and marks the re-run stages as "revised" for highlight.
  function rerun(stage: string, feedback: string) {
    if (!run || streaming) return;
    streamCloser.current?.();
    setRevisedStages(new Set());
    setRunError(null);
    setStreaming(true);
    streamCloser.current = openRerunStream(run.run_id, stage, feedback, {
      onStage: (ev) => {
        if (ev.revised) setRevisedStages((prev) => new Set(prev).add(ev.stage));
        setStages((prev) => ({
          ...prev,
          [ev.stage]:
            ev.phase === "done"
              ? ev
              : { ...prev[ev.stage], ...ev },
        }));
      },
      onState: (state) => {
        setRun(state);
        setStreaming(false);
      },
      onError: (message) => {
        setRunError(message);
        setStreaming(false);
      },
    });
  }

  function backToQueue() {
    streamCloser.current?.();
    streamCloser.current = null;
    setRun(null);
    setActiveDispute(null);
    setStages({});
    setStreaming(false);
    setRevisedStages(new Set());
    setRunError(null);
  }

  return (
    <div className="app">
      <header className="app-header">
        <div>
          <h1>CMK Control Tower</h1>
          <p className="subtitle">
            Counterparty dispute resolution · Microsoft Agent Framework · human-in-the-loop
          </p>
        </div>
        <button className="ghost" onClick={() => setShowWorkflow(true)}>
          Show agent workflow
        </button>
      </header>

      {!activeDispute ? (
        <section className="panel">
          <h2>
            Open dispute queue
            {!loadingList && <span className="badge badge-warn">{disputes.length} open</span>}
          </h2>
          <p className="muted">
            Live from the <code>cmk-sqldb-ledger</code> SQL ledger. Select a dispute to launch the
            five-agent pipeline (Intake → Prediction → Reconstruction → Root-Cause → Remediation) and
            route it through orchestrator approval.
          </p>
          {listError && <div className="error">⚠ {listError}</div>}
          {loadingList ? (
            <p className="muted">Loading disputes…</p>
          ) : (
            <table className="grid">
              <thead>
                <tr>
                  <th>Dispute</th>
                  <th>Category</th>
                  <th>Notional</th>
                  <th>Filed by</th>
                  <th>Security</th>
                  <th>Evidence</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {disputes.map((d) => (
                  <tr key={d.dispute_id}>
                    <td className="mono">{d.dispute_id}</td>
                    <td>
                      <span className="badge badge-cat">{d.category}</span>
                    </td>
                    <td className="mono">{fmtUsd(d.notional_usd)}</td>
                    <td>{d.filer_name ?? d.filer_cp_id ?? "—"}</td>
                    <td>
                      {d.ticker ? (
                        <span>
                          {d.ticker} <span className="muted">· {d.side}</span>
                        </span>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td>
                      <span className={`badge ${Number(d.completeness_pct) >= 0.9 ? "badge-ok" : "badge-warn"}`}>
                        {fmtPct(d.completeness_pct)}
                      </span>
                    </td>
                    <td>
                      <button className="primary sm" onClick={() => openDispute(d)}>
                        Run agents →
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {!loadingList && filerCounts.size > 0 && (
            <p className="muted">
              By category:{" "}
              {[...filerCounts.entries()]
                .sort((a, b) => b[1] - a[1])
                .map(([k, v]) => `${k} (${v})`)
                .join(" · ")}
            </p>
          )}
        </section>
      ) : (
        <RunView
          dispute={activeDispute}
          run={run}
          stages={stages}
          streaming={streaming}
          busy={busy}
          error={runError}
          resolutionOptions={RESOLUTION_OPTIONS}
          onBack={backToQueue}
          onDecide={decide}
          revisedStages={revisedStages}
          onRerun={rerun}
        />
      )}

      {showWorkflow && <WorkflowModal run={run} onClose={() => setShowWorkflow(false)} />}
    </div>
  );
}
