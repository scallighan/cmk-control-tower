"""Ledger artifacts written by the Orchestrator (simulated Azure Confidential Ledger).

The dispute pipeline reads the tamper-evident ``demo4_*`` SQL ledger tables
strictly read-only. When a human approves (or denies/modifies) a remediation,
the Orchestrator does **not** write back to the ledger; instead it computes a
real SHA-256 digest over the decision and returns a simulated ``ACLReceipt``
(digest + stubbed transaction id) alongside an in-memory ``ApprovalRecord``.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_digest(payload: Any) -> str:
    """Deterministic SHA-256 over a JSON-canonical payload."""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class HumanDecision(BaseModel):
    """The human-in-the-loop decision on a remediation proposal."""

    action: Literal["approve", "deny", "modify"]
    approver: str = "ops_analyst"
    # For ``modify``: override the resolution the Remediation Agent proposed.
    final_resolution: str | None = None
    note: str = ""


class ApprovalRecord(BaseModel):
    """Orchestrator output: the HITL decision (simulated ledger write)."""

    approval_id: str = Field(default_factory=lambda: f"APR-{uuid.uuid4().hex[:10]}")
    dispute_id: str
    action: Literal["approve", "deny", "modify"]
    approver: str = "ops_analyst"
    resolution: str = "PENDING"
    note: str = ""
    decided_at: str = Field(default_factory=_now)


class ACLReceipt(BaseModel):
    """Simulated Azure Confidential Ledger receipt for a committed artifact."""

    dispute_id: str
    artifact: str  # e.g. "EvidencePack", "ApprovalRecord"
    digest_hash: str
    transaction_id: str = Field(
        default_factory=lambda: f"ACLTXN-SIM-{uuid.uuid4().int % 100_000_000:08d}"
    )
    collection_id: str = "cmk-control-tower"
    committed_at: str = Field(default_factory=_now)
    simulated: bool = True
