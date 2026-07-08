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
  produces: string;
}

// The dispute pipeline assembled in main.py (build_workflow), sourced from the
// agents/ handoff briefing.
const STEPS: Step[] = [
  {
    id: "load",
    title: "0 · Load dispute context",
    detail:
      "Assemble the full lifecycle for the selected dispute from the SQL ledger (trade, confirmation, affirmation, SSI, evidence pack, comms) — read-only.",
    produces: "DisputeContext",
  },
  {
    id: "intake",
    title: "1 · Intake Agent",
    detail:
      "Classifies the dispute type, identifies the governing rule set, registers the case and computes routing.",
    produces: "Dispute (case opened)",
  },
  {
    id: "prediction",
    title: "2 · Prediction Agent",
    detail:
      "Scores at-risk trades before affirmation & settlement cutoffs from match state, SSI status, historical fail patterns and counterparty profile.",
    produces: "AgentFinding (risk score)",
  },
  {
    id: "reconstruction",
    title: "3 · Reconstruction / Evidence Agent",
    detail:
      "Assembles the full lifecycle into a canonical evidence pack and verifies the ledger digest.",
    produces: "EvidencePack (+ digest_hash)",
  },
  {
    id: "rootcause",
    title: "4 · Root-Cause Agent",
    detail:
      "Diagnoses the break category and materially responsible party against the rule engine and similar-case corpus.",
    produces: "AgentFinding (root cause)",
  },
  {
    id: "remediation",
    title: "5 · Remediation Agent",
    detail:
      "Drafts the chaser / cancel-rebook / economic adjustment proposal and the human-approval summary.",
    produces: "AgentFinding (recommendation)",
  },
  {
    id: "approval",
    title: "6 · Orchestrator · human approval",
    detail:
      "Enforces human-in-the-loop: a person approves, modifies the resolution, or denies before anything is committed.",
    produces: "—",
  },
  {
    id: "ledger",
    title: "7 · Orchestrator · write ledger",
    detail:
      "Writes the ApprovalRecord and hashes each artifact into a simulated Azure Confidential Ledger (real SHA-256 digest, stubbed transaction id).",
    produces: "ApprovalRecord, ACLReceipt",
  },
];

function computeStatuses(run: RunState | null): Record<string, StepStatus> {
  const s: Record<string, StepStatus> = {};
  for (const step of STEPS) s[step.id] = "idle";
  if (!run) return s;

  // By the time the server returns a state, load → the 5 agents have all run.
  s.load = "done";
  s.intake = run.intake ? "done" : "idle";
  s.prediction = run.prediction ? "done" : "idle";
  s.reconstruction = run.reconstruction ? "done" : "idle";
  s.rootcause = run.root_cause ? "done" : "idle";
  s.remediation = run.remediation ? "done" : "idle";

  if (run.status === "awaiting_approval") {
    s.approval = "active";
  } else {
    s.approval = "done";
    s.ledger = "done";
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
              : " Select a dispute to walk a run through these stages."}
          </p>
          <ol className="wf-steps">
            {STEPS.map((step) => {
              const st = status[step.id];
              return (
                <li key={step.id} className={`wf-step wf-${st}`}>
                  <span className="wf-marker" aria-hidden>
                    {st === "done" ? "✓" : st === "active" ? "▶" : "○"}
                  </span>
                  <div className="wf-body">
                    <div className="wf-title">
                      {step.title}
                      {st === "active" && <span className="badge badge-warn">current</span>}
                    </div>
                    <div className="wf-detail">{step.detail}</div>
                    <div className="wf-produces">
                      produces → <code>{step.produces}</code>
                    </div>
                  </div>
                </li>
              );
            })}
          </ol>
        </div>
      </div>
    </div>
  );
}
