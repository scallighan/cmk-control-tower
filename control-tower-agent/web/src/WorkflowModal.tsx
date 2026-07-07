import { useEffect } from "react";
import { RunState } from "./api";

interface Props {
  run: RunState | null;
  onClose: () => void;
}

type StepStatus = "idle" | "active" | "done";

interface Step {
  id: string;
  title: string;
  detail: string;
  branch?: "matched" | "broken";
}

// The reconciliation workflow assembled in main.py (build_workflow).
const STEPS: Step[] = [
  {
    id: "ingest",
    title: "1 · Ingest confirmations CSV",
    detail: "Parse each uploaded row (trade_id, source, cfm_price, cfm_qty, cfm_gross).",
  },
  {
    id: "lookup",
    title: "2 · Look up booked trades (SQL, read-only)",
    detail: "Fetch the firm's economics of record from demo4_trades for each trade_id.",
  },
  {
    id: "agent",
    title: "3 · Reconciliation agent",
    detail:
      "Azure AI Foundry agent decides matched vs broken per row and proposes minimal cfm_* corrections.",
  },
  {
    id: "route",
    title: "4 · Route (switch)",
    detail: "All matched → finalize · any break → human-in-the-loop approval.",
  },
  {
    id: "finalize",
    title: "5a · Finalize matched",
    detail: "Clean confirmations are confirmed immediately.",
    branch: "matched",
  },
  {
    id: "approval",
    title: "5b · Human approval",
    detail: "A person approves, denies, or modifies the agent's proposed corrections.",
    branch: "broken",
  },
  {
    id: "report",
    title: "6 · Apply & emit reconciled report",
    detail: "Approved fixes are applied in memory, each pair is re-verified, and the report is emitted.",
  },
];

function computeStatuses(run: RunState | null): Record<string, StepStatus> {
  const s: Record<string, StepStatus> = {};
  for (const step of STEPS) s[step.id] = "idle";
  if (!run) return s;

  const hasBreaks =
    run.pending.length > 0 || run.items.some((i) => !i.matched);

  // By the time the server returns a state, ingest → agent → route have all run.
  s.ingest = "done";
  s.lookup = "done";
  s.agent = "done";
  s.route = "done";

  if (run.status === "awaiting_approval") {
    s.approval = "active";
  } else {
    // completed
    if (hasBreaks) {
      s.approval = "done";
    } else {
      s.finalize = "done";
    }
    s.report = "done";
  }
  return s;
}

export default function WorkflowModal({ run, onClose }: Props) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const status = computeStatuses(run);

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2>Agent workflow</h2>
          <button className="close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>
        <div className="modal-body">
          <p className="muted">
            Microsoft Agent Framework workflow (<code>build_workflow</code> in <code>main.py</code>).
            {run
              ? " The highlighted step is where this run currently sits."
              : " Upload a CSV to walk a run through these stages."}
          </p>
          <ol className="wf-steps">
            {STEPS.map((step) => {
              const st = status[step.id];
              return (
                <li
                  key={step.id}
                  className={`wf-step wf-${st}${step.branch ? ` wf-branch wf-${step.branch}` : ""}`}
                >
                  <span className="wf-marker" aria-hidden>
                    {st === "done" ? "✓" : st === "active" ? "▶" : "○"}
                  </span>
                  <div className="wf-body">
                    <div className="wf-title">
                      {step.title}
                      {step.branch && (
                        <span className={`badge ${step.branch === "matched" ? "badge-ok" : "badge-warn"}`}>
                          {step.branch === "matched" ? "matched path" : "break path"}
                        </span>
                      )}
                      {st === "active" && <span className="badge badge-warn">current</span>}
                    </div>
                    <div className="wf-detail">{step.detail}</div>
                  </div>
                </li>
              );
            })}
          </ol>
          <p className="muted">
            Nothing is ever written back to the ledger tables — corrections are applied to an in-memory
            copy purely to produce the reconciled report.
          </p>
        </div>
      </div>
    </div>
  );
}
