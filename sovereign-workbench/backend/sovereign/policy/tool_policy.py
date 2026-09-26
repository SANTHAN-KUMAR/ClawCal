"""Tool policy engine.

Three permission modes, set per *session* rather than per process (v2 §5.2):

    mode      reads   calculator/search   code, docgen, file writes   approval gate
    review    auto    auto                asks before each            human
    trusted   auto    auto                auto, logged                human only for PRIVILEGED
    locked    auto    auto                refused                     n/a

`review` is the default. A session's mode is read at every tool call, so
changing it takes effect at the running task's next step.

The important property is unchanged: this decision is made by the *control
plane*, from a static risk table, before the tool is invoked. No model output --
and therefore no uploaded document -- can change it. Every evaluation writes a
decision row (authority `tool_policy`).

The v1 names are accepted as aliases so an existing deployment's
SOVEREIGN_POLICY_MODE keeps meaning something: `standard` -> trusted,
`controlled` and `strict` -> review.
"""
from __future__ import annotations

import os
import threading
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


MODES = ("review", "trusted", "locked")
LEGACY_MODES = {"standard": "trusted", "controlled": "review", "strict": "review"}


def normalise_mode(mode: str | None) -> str:
    m = (mode or "").strip().lower()
    m = LEGACY_MODES.get(m, m)
    if m not in MODES:
        raise ValueError(f"unknown permission mode {mode!r}; modes are "
                         f"{', '.join(MODES)}")
    return m


def _default_from_env() -> str:
    try:
        return normalise_mode(settings.policy_mode)
    except ValueError:
        return "review"


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


def mode_for_task(task_id: str | None) -> tuple[str | None, str | None]:
    """(mode, session_id) for a task, read live from its session."""
    if not task_id:
        return None, None
    row = db.query_one(
        "SELECT t.conversation_id AS sid, t.policy_mode AS tmode, "
        "s.permission_mode AS smode FROM tasks t LEFT JOIN sessions s "
        "ON s.id = t.conversation_id WHERE t.id=?", (task_id,))
    if not row:
        return None, None
    mode = row["smode"] or row["tmode"]
    try:
        return (normalise_mode(mode) if mode else None), row["sid"]
    except ValueError:
        return "review", row["sid"]


class ToolPolicy:
    def __init__(self) -> None:
        # The default for new sessions. Changed through the admin surface.
        self.mode = _default_from_env()
        # Tools an agent may never call regardless of mode. Present so the denial
        # is a policy event with a name, not an absent capability.
        self.forbidden: set[str] = set()
        self._lock = threading.Lock()

    def set_mode(self, mode: str, *, by: str = "system") -> None:
        """Set the default mode for sessions created from now on."""
        mode = normalise_mode(mode)
        with self._lock:
            old, self.mode = self.mode, mode
        audit.record("policy", "default_mode_changed", actor=by,
                     detail={"from": old, "to": mode})

    def evaluate(self, tool_name: str, risk: Risk, args: dict[str, Any],
                 *, task_id: str | None = None,
                 allowed_tools: set[str] | None = None,
                 mode: str | None = None) -> PolicyDecision:
        if mode is None:
            mode, _ = mode_for_task(task_id)
        mode = normalise_mode(mode) if mode else self.mode
        risk = Risk(int(risk))

        if tool_name in self.forbidden:
            return PolicyDecision(False, False,
                                  f"{tool_name} is on the forbidden list for this "
                                  f"deployment", mode, risk)
        if allowed_tools is not None and tool_name not in allowed_tools:
            return PolicyDecision(False, False,
                                  f"{tool_name} is not in the tool set granted to "
                                  f"this task", mode, risk)
        if risk == Risk.READ_ONLY:
            return PolicyDecision(True, False,
                                  f"{tool_name} is READ_ONLY and runs automatically "
                                  f"in every mode", mode, risk)
        if mode == "locked":
            return PolicyDecision(
                False, False,
                f"{tool_name} is classified {risk.name}; this session is in "
                f"'locked' mode, which permits reads only. Change the session's "
                f"mode to review or trusted to allow it.", mode, risk)
        if mode == "review":
            return PolicyDecision(
                True, True,
                f"{tool_name} is classified {risk.name}; 'review' mode asks a "
                f"human before every action that writes, executes or produces a "
                f"deliverable", mode, risk)
        # trusted
        if risk >= Risk.PRIVILEGED:
            return PolicyDecision(
                True, True,
                f"{tool_name} is classified PRIVILEGED; even 'trusted' mode asks a "
                f"human before anything that touches the host or the network",
                mode, risk)
        return PolicyDecision(
            True, False,
            f"{tool_name} is classified {risk.name}; 'trusted' mode runs it "
            f"automatically and logs it", mode, risk)


policy = ToolPolicy()


# --------------------------------------------------------------------- approvals

# Approving needs at least this role. An engineer can delegate the work; only an
# approver can let a deliverable leave or code run on their behalf.
APPROVER_ROLE = "approver"


def approval_timeout_s() -> float:
    try:
        return float(os.environ.get("SOVEREIGN_APPROVAL_TIMEOUT_S", "900"))
    except ValueError:
        return 900.0


def separation_of_duties() -> bool:
    """When on, nobody may approve an action on a task they submitted."""
    return os.environ.get("SOVEREIGN_SEPARATION_OF_DUTIES", "0") == "1"


def request_approval(task_id: str, tool: str, args: dict[str, Any],
                     summary: str) -> str:
    aid = db.new_id("appr")
    _, session_id = mode_for_task(task_id)
    db.insert("approvals", {
        "id": aid, "task_id": task_id, "tool": tool, "args": db.jdump(args),
        "summary": summary[:1000], "state": "PENDING", "created_at": time.time(),
        "required_role": APPROVER_ROLE, "session_id": session_id})
    audit.record("policy", "approval_requested", outcome="PENDING", task_id=task_id,
                 detail={"tool": tool, "summary": summary[:300]})
    audit.bus.publish({"type": "approval", "id": aid, "task_id": task_id,
                       "tool": tool, "summary": summary[:300], "state": "PENDING"})
    return aid


class ApprovalRefused(PermissionError):
    """The principal may not decide this approval."""


def decide_approval(approval_id: str, approve: bool, by: str) -> bool:
    """Record a human decision. `by` is the deciding principal's name.

    Returns False if the approval is not pending. Raises ApprovalRefused when
    separation of duties forbids this principal deciding it.
    """
    if not by or by == "system":
        raise ApprovalRefused("an approval must be decided by a named human "
                              "principal, not by the system")
    row = db.query_one("SELECT a.*, t.owner FROM approvals a LEFT JOIN tasks t "
                       "ON t.id = a.task_id WHERE a.id=?", (approval_id,))
    if not row or row["state"] != "PENDING":
        return False
    if separation_of_duties() and row["owner"] == by:
        raise ApprovalRefused(f"{by} submitted this task and separation of duties "
                              f"is in force; another approver must decide it")
    state = "APPROVED" if approve else "DENIED"
    # Conditional update: two approvers pressing at once must not both win.
    with db.tx() as c:
        cur = c.execute("UPDATE approvals SET state=?, decided_by=?, decided_at=? "
                        "WHERE id=? AND state='PENDING'",
                        (state, by, time.time(), approval_id))
        if cur.rowcount != 1:
            return False
    audit.record("policy", "approval_decided", outcome=state,
                 task_id=row["task_id"], actor=by,
                 detail={"tool": row["tool"], "approval_id": approval_id})
    from ..control import decisions
    decisions.record("tool_policy", state,
                     f"{by} {state.lower()} {row['tool']}: {row['summary'] or ''}"[:600],
                     subject_kind="approval", subject_id=approval_id,
                     task_id=row["task_id"], principal=by,
                     basis={"tool": row["tool"], "self_approval": row["owner"] == by,
                            "separation_of_duties": separation_of_duties()})
    audit.bus.publish({"type": "approval", "id": approval_id, "state": state,
                       "task_id": row["task_id"]})
    return True


def approval_state(approval_id: str) -> str:
    row = db.query_one("SELECT state FROM approvals WHERE id=?", (approval_id,))
    return row["state"] if row else "MISSING"


def wait_for_approval(approval_id: str, timeout_s: float | None = None,
                      poll_s: float = 1.0, should_stop: Any = None) -> str:
    """Block until a human decides, the task is stopped, or the wait expires.

    `should_stop` lets a cancelled task stop waiting now instead of holding its
    agent slot, and its resident model, until the timeout.
    """
    deadline = time.time() + (timeout_s or approval_timeout_s())
    while time.time() < deadline:
        state = approval_state(approval_id)
        if state != "PENDING":
            return state
        if should_stop is not None and should_stop():
            with db.tx() as c:
                c.execute("UPDATE approvals SET state='CANCELLED', decided_at=? "
                          "WHERE id=? AND state='PENDING'",
                          (time.time(), approval_id))
            return approval_state(approval_id)
        time.sleep(poll_s)
    with db.tx() as c:
        c.execute("UPDATE approvals SET state='EXPIRED', decided_at=? "
                  "WHERE id=? AND state='PENDING'", (time.time(), approval_id))
    return approval_state(approval_id)


def pending_approvals() -> list[dict[str, Any]]:
    return db.rows_to_dicts(db.query(
        "SELECT * FROM approvals WHERE state='PENDING' ORDER BY created_at DESC"))
