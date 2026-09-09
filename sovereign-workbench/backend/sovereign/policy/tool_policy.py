"""Tool policy engine.

Three organisational postures, as the locked architecture requires:

    standard    low-risk local actions run automatically
    controlled  sensitive tool calls require human approval
    strict      anything privileged or externally relevant requires approval

The important property is that this decision is made by the *control plane*, from
a static risk table, before the tool is invoked. No model output -- and therefore
no uploaded document -- can change it.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

from .. import audit, db
from ..config import settings


class Risk(IntEnum):
    READ_ONLY = 0        # inspect local state, no side effects
    LOCAL_WRITE = 1      # write inside the task workspace
    COMPUTE = 2          # execute code in the sandbox
    DELIVERABLE = 3      # produce a document that leaves the workbench as a file
    PRIVILEGED = 4       # anything touching host or network


# Approval threshold per mode: risk >= threshold needs a human.
_THRESHOLD = {
    "standard": Risk.PRIVILEGED,
    "controlled": Risk.DELIVERABLE,
    "strict": Risk.LOCAL_WRITE,
}


@dataclass
class PolicyDecision:
    allowed: bool
    requires_approval: bool
    reason: str
    mode: str
    risk: int

    def to_dict(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "requires_approval": self.requires_approval,
                "reason": self.reason, "mode": self.mode, "risk": int(self.risk),
                "risk_name": Risk(self.risk).name}


class ToolPolicy:
    def __init__(self) -> None:
        self.mode = settings.policy_mode
        # Tools an agent may never call regardless of mode. Present so the denial
        # is a policy event with a name, not an absent capability.
        self.forbidden: set[str] = set()

    def set_mode(self, mode: str) -> None:
        if mode not in _THRESHOLD:
            raise ValueError(f"unknown policy mode {mode!r}")
        old, self.mode = self.mode, mode
        audit.record("policy", "mode_changed", detail={"from": old, "to": mode})

    def evaluate(self, tool_name: str, risk: Risk, args: dict[str, Any],
                 *, task_id: str | None = None,
                 allowed_tools: set[str] | None = None) -> PolicyDecision:
        mode = self.mode
        if tool_name in self.forbidden:
            return PolicyDecision(False, False,
                                  f"{tool_name} is on the forbidden list for this "
                                  f"deployment", mode, risk)
        if allowed_tools is not None and tool_name not in allowed_tools:
            return PolicyDecision(False, False,
                                  f"{tool_name} is not in the tool set granted to "
                                  f"this task", mode, risk)

        threshold = _THRESHOLD[mode]
        if risk >= threshold:
            return PolicyDecision(
                True, True,
                f"{tool_name} is classified {Risk(risk).name}; policy mode "
                f"'{mode}' requires human approval at {threshold.name} and above",
                mode, risk)
        return PolicyDecision(
            True, False,
            f"{tool_name} is classified {Risk(risk).name}, below the approval "
            f"threshold {threshold.name} for policy mode '{mode}'", mode, risk)


policy = ToolPolicy()


# --------------------------------------------------------------------- approvals

def request_approval(task_id: str, tool: str, args: dict[str, Any],
                     summary: str) -> str:
    aid = db.new_id("appr")
    db.insert("approvals", {
        "id": aid, "task_id": task_id, "tool": tool, "args": db.jdump(args),
        "summary": summary[:1000], "state": "PENDING", "created_at": time.time()})
    audit.record("policy", "approval_requested", outcome="PENDING", task_id=task_id,
                 detail={"tool": tool, "summary": summary[:300]})
    audit.bus.publish({"type": "approval", "id": aid, "task_id": task_id,
                       "tool": tool, "summary": summary[:300], "state": "PENDING"})
    return aid


def decide_approval(approval_id: str, approve: bool, by: str = "operator") -> bool:
    row = db.query_one("SELECT * FROM approvals WHERE id=?", (approval_id,))
    if not row or row["state"] != "PENDING":
        return False
    state = "APPROVED" if approve else "DENIED"
    db.update("approvals", "id", approval_id,
              {"state": state, "decided_by": by, "decided_at": time.time()})
    audit.record("policy", "approval_decided", outcome=state,
                 task_id=row["task_id"], actor=by,
                 detail={"tool": row["tool"], "approval_id": approval_id})
    audit.bus.publish({"type": "approval", "id": approval_id, "state": state,
                       "task_id": row["task_id"]})
    return True


def approval_state(approval_id: str) -> str:
    row = db.query_one("SELECT state FROM approvals WHERE id=?", (approval_id,))
    return row["state"] if row else "MISSING"


def wait_for_approval(approval_id: str, timeout_s: float = 300.0,
                      poll_s: float = 1.0) -> str:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        state = approval_state(approval_id)
        if state != "PENDING":
            return state
        time.sleep(poll_s)
    db.update("approvals", "id", approval_id,
              {"state": "EXPIRED", "decided_at": time.time()})
    return "EXPIRED"


def pending_approvals() -> list[dict[str, Any]]:
    return db.rows_to_dicts(db.query(
        "SELECT * FROM approvals WHERE state='PENDING' ORDER BY created_at DESC"))
