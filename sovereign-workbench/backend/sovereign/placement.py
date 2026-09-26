"""B1, extended with placement (sovereign-workbench-v2.md §8).

The router's rule stands: one decision per task, never per step; deterministic
and explainable, not an LLM call; the model pinned for the whole plan so the
node's prefix cache and a client's expert cache stay warm. v2 adds a second
dimension — *where* each stage runs — from the device profile:

    rules + exemplars          (router.classify — unchanged)
        ↓
    class ∈ { code · draft · extract · vision · calc }
        ↓
    × device (class, grade, mode)
        ↓
    placement ∈ { node · client · split }
        ↓
    model + tool profile + harness agent, PINNED
        ↓
    decision row: class, placement, reason     ("why this model, here")

Placement rules, all subject to the trust-domain policy:

* **extract** and **vision** always run on the node: they need the ingestion
  pipeline, the OCR and vision models, the corpus and the gate.
* **draft**, **code** and **calc** run where the pinned model is: the node when
  attached, the client when detached.
* **split** (RQ2) — retrieve on the node, draft on the client, gate on the node
  against the spans *it* served — only when the policy turns it on.

A heavy task arriving while the node is saturated **waits rather than evicts**:
admission already queues; the external-loop surfaces take a slot or wait.
"""
from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from . import db, router

# The task taxonomy the router already has, folded onto the five classes the
# placement rules are written in.
SPEC_CLASS = {
    "coding": "code",
    "deliverable_drafting": "draft", "summarisation": "draft",
    "knowledge_qa": "draft", "general": "draft",
    "calculation": "calc",
    "document_extraction": "extract", "engineering_analysis": "extract",
    "vision_understanding": "vision", "drawing_analysis": "vision",
}
NODE_ONLY = ("extract", "vision")

# The node's tools a client harness may reach, by class (MCP names).
NODE_TOOLS = {
    "code": ["retrieve", "read_page", "calculate", "stage", "execute_remote"],
    "draft": ["retrieve", "read_page", "extract_values", "calculate", "deliver"],
    "calc": ["retrieve", "read_page", "extract_values", "calculate",
             "execute_remote"],
    "extract": ["retrieve", "read_page", "extract_values", "calculate", "deliver"],
    "vision": ["retrieve", "read_page", "extract_values", "deliver"],
}
SPLIT_TOOLS = ["retrieve", "read_page", "extract_values", "deliver"]
LOCAL_TOOLS = ["read", "edit", "glob", "grep"]


@dataclass
class Placement:
    spec_class: str
    placement: str                    # node | client | split | refused
    reason: str
    node_tools: list[str] = field(default_factory=list)
    local_tools: list[str] = field(default_factory=list)
    execute_local: bool = False

    @property
    def refused(self) -> bool:
        return self.placement == "refused"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def place(task_type: str, device: Any = None, *,
          detached_classes: list[str] | None = None) -> Placement:
    """Where a task of this type runs for this requester. Pure and explainable.

    `device` is a `DeviceContext` (or None for the node's own console and the
    web workbench). `detached_classes` are the task classes the device's signed
    manifest allows it to run locally.
    """
    from .control import trust
    spec = SPEC_CLASS.get(task_type, "draft")
    pol = trust.policy()
    if device is None:
        return Placement(spec, "node",
                         f"{spec} work requested from the node's own surfaces runs "
                         f"on the node", node_tools=NODE_TOOLS[spec])
    rules = trust.rules_for(device.grade)
    local = list(LOCAL_TOOLS)
    xl = bool(rules.get("execute_local"))

    if device.mode == "detached":
        if spec in NODE_ONLY:
            return Placement(spec, "refused",
                             f"{spec} needs the ingestion pipeline, the OCR and vision "
                             f"models, the corpus and the provenance gate, which live "
                             f"on the node; this device is detached. Re-attach, or "
                             f"ask for draft, code or calc work")
        allowed = detached_classes or []
        if spec not in allowed:
            return Placement(spec, "refused",
                             f"this device's signed manifest allows "
                             f"{', '.join(allowed) or 'no'} task classes detached, "
                             f"not {spec}: the local model is below the measured "
                             f"floor for it (RQ1/T12)")
        return Placement(spec, "client",
                         f"{spec} runs on the client's own engine because the device "
                         f"is detached; no node tools, no team corpus",
                         local_tools=local, execute_local=xl)

    if spec in NODE_ONLY:
        return Placement(spec, "node",
                         f"{spec} always runs on the node: it needs the ingestion "
                         f"pipeline, the OCR and vision models, the corpus and the "
                         f"gate", node_tools=NODE_TOOLS[spec], local_tools=local,
                         execute_local=xl)
    if spec == "draft" and pol.get("split_placement") and device.device_class \
            and device.device_class in pol.get("shipped_classes", []):
        return Placement(spec, "split",
                         f"split placement is enabled: the node retrieves and records "
                         f"the spans it serves, the client drafts on its "
                         f"{device.device_class} engine, and the node's gate checks "
                         f"the draft against the served spans",
                         node_tools=SPLIT_TOOLS, local_tools=local, execute_local=xl)
    return Placement(spec, "node",
                     f"{spec} runs where the pinned model is — on the node, because "
                     f"the device is attached", node_tools=NODE_TOOLS[spec],
                     local_tools=local, execute_local=xl)


# ------------------------------------------------------------ node saturation

class ExternalSlots:
    """Admission for the client harnesses' inference calls.

    Internal tasks take a slot from the scheduler. A client-side loop calls the
    node's model directly, and without a gate of its own ten laptops would each
    demand their pinned model at once and the residency manager would evict
    under a running task. The slot count is the same memory-derived limit the
    scheduler uses; a call waits for a slot instead of evicting.
    """

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._busy = 0
        self._by: dict[str, int] = {}

    def limit(self) -> int:
        from .runtime.residency import residency
        return max(1, residency.concurrency_limit()[0])

    def per_user(self) -> int:
        from .config import settings
        return max(1, settings.limits.max_concurrent_per_user)

    def acquire(self, timeout_s: float, who: str = "") -> bool:
        """A slot, waiting up to `timeout_s`. One principal may hold at most the
        per-user quota, so ten laptops of one engineer cannot starve the rest."""
        deadline = time.time() + timeout_s
        with self._cv:
            while self._busy >= self.limit() or (
                    who and self._by.get(who, 0) >= self.per_user()):
                left = deadline - time.time()
                if left <= 0:
                    return False
                self._cv.wait(min(left, 1.0))
            self._busy += 1
            if who:
                self._by[who] = self._by.get(who, 0) + 1
            return True

    def release(self, who: str = "") -> None:
        with self._cv:
            self._busy = max(0, self._busy - 1)
            if who and self._by.get(who):
                self._by[who] -= 1
                if not self._by[who]:
                    del self._by[who]
            self._cv.notify_all()

    def state(self) -> dict[str, Any]:
        return {"busy": self._busy, "limit": self.limit(), "per_user": self.per_user(),
                "by_principal": dict(self._by)}


external_slots = ExternalSlots()


def node_load() -> dict[str, Any]:
    """Queue depth and slot occupancy, the saturation signal B1 consults."""
    from .runtime.residency import residency
    q = db.query_one("SELECT COUNT(*) AS n FROM tasks WHERE state IN "
                     "('QUEUED','PAUSED')")["n"]
    r = db.query_one("SELECT COUNT(*) AS n FROM tasks WHERE state IN "
                     "('ADMITTED','RUNNING','RESUMING')")["n"]
    limit, why = residency.concurrency_limit()
    ext = external_slots.state()
    busy = r + ext["busy"]
    return {"queued": q, "running": r, "external_busy": ext["busy"],
            "slots": limit, "saturated": busy >= limit, "why": why}


# ------------------------------------------------------------------ /admit

def admit(principal: Any, device: Any, *, prompt: str,
          attachments: list[dict[str, Any]] | None = None,
          session_id: str | None = None, mode: str | None = None,
          console: bool = False) -> dict[str, Any]:
    """Classify, place and pin — once — for a client harness's plan.

    Creates (or joins) a session and opens an *attached loop* task row for it:
    the row the node's tool calls, served spans, gate reports and decisions for
    this plan all hang from, so the transcript and the audit read the same way
    whether the loop ran on the node or on a laptop.
    """
    from .control import decisions, sessions, trust
    from .runtime.residency import residency
    from .router import select_model
    sweep_abandoned()

    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("prompt is required")
    if session_id:
        sessions.ensure(session_id, principal, title=prompt[:90], mode=mode)
        sessions.check_access(session_id, principal, write=True)
    else:
        session_id = sessions.create(principal, title=prompt[:90], mode=mode)["id"]

    cls = router.classify(prompt, attachments=attachments or [])
    dev_row = None
    detached_classes: list[str] = []
    if device is not None:
        dev_row = dict(db.query_one("SELECT * FROM devices WHERE id=?",
                                    (device.device_id,)))
        report = db.jload(dev_row.get("class_report"), {}) or {}
        detached_classes = list(report.get("task_classes_allowed") or [])
    grade, grade_src = trust.request_grade(dev_row, console)
    pl = place(cls.task_type, device, detached_classes=detached_classes)
    load = node_load()

    model, model_reason, routing = "", "", {}
    if pl.placement in ("node", "split"):
        dec = select_model(cls, resident=residency.resident_names(),
                           budget_for=residency.context_budget_tokens)
        routing = dec.to_dict()
        if not dec.model:
            pl = Placement(pl.spec_class, "refused", dec.reason)
        model, model_reason = dec.model, dec.reason
    elif pl.placement == "client":
        report = db.jload(dev_row.get("class_report"), {}) if dev_row else {}
        model = (report or {}).get("model", "")
        model_reason = (f"the device's signed manifest pins {model or 'no model'} "
                        f"for {(report or {}).get('device_class', 'its class')}")

    tid = db.new_id("task")
    state = "REJECTED" if pl.refused else "ATTACHED"
    now = time.time()
    db.insert("tasks", {
        "id": tid, "conversation_id": session_id, "title": prompt[:200],
        "prompt": prompt, "owner": principal.name,
        "department": getattr(principal, "department", "default"),
        "workflow": "attached_client", "task_type": cls.task_type,
        "priority": cls.priority,
        "effective_priority": float(router.PRIORITY_RANK[cls.priority]),
        "state": state, "state_reason": pl.reason[:500],
        "required_caps": db.jdump(cls.profile.needs),
        "selected_model": model or None, "routing_reason": model_reason[:1000],
        "est_context_tokens": cls.est_context_tokens,
        "attachments": db.jdump(attachments or []),
        "device_id": device.device_id if device else None, "grade": grade,
        "placement": pl.placement, "created_at": now, "admitted_at": now,
        "started_at": now, "finished_at": now if pl.refused else None})
    reason = (f"{cls.task_type} → {pl.spec_class}; placement {pl.placement}: "
              f"{pl.reason}" + (f". Model {model}: {model_reason}" if model else ""))
    if pl.placement in ("node", "split") and load["saturated"]:
        reason += (f". The node is saturated ({load['running'] + load['external_busy']}"
                   f" of {load['slots']} slots busy, {load['queued']} queued): calls "
                   f"will wait for a slot rather than evict a running task's model")
    decisions.record("routing", pl.placement.upper(), reason, subject_kind="task",
                     subject_id=tid, task_id=tid, session_id=session_id,
                     principal=principal.name,
                     basis={"task_type": cls.task_type, "spec_class": pl.spec_class,
                            "placement": pl.placement, "model": model,
                            "device": device.device_id if device else None,
                            "grade": grade, "grade_source": grade_src,
                            "signals": cls.signals, "node_load": load,
                            "tools": pl.node_tools})
    return {
        "admitted": not pl.refused, "session_id": session_id, "task_id": tid,
        "task_type": cls.task_type, "spec_class": pl.spec_class,
        "placement": pl.placement, "reason": pl.reason, "why": reason,
        "model": model, "model_alias": "generalist", "model_reason": model_reason,
        "grade": grade, "clearance": trust.clearance(grade),
        "node_tools": pl.node_tools, "local_tools": pl.local_tools,
        "execute_local": pl.execute_local, "agent": f"clawcal-{pl.spec_class}",
        "priority": cls.priority, "signals": cls.signals, "node_load": load,
        "routing": routing,
    }


def attached_task(session_id: str, principal: str,
                  device_id: str | None) -> dict[str, Any] | None:
    """The open attached-loop task a client's tool call belongs to."""
    row = db.query_one(
        "SELECT * FROM tasks WHERE conversation_id=? AND owner=? AND "
        "state='ATTACHED' AND workflow='attached_client' AND "
        "COALESCE(device_id,'') = COALESCE(?, '') "
        "ORDER BY created_at DESC LIMIT 1", (session_id, principal, device_id))
    return dict(row) if row else None


def touch(task_id: str | None) -> None:
    """The client's harness reached the node for this plan (a /v1 or MCP call).
    The launcher's startup deadline reads this: contact, not output, is what
    proves the harness is not stuck in its own initialisation."""
    if task_id:
        db.execute("UPDATE tasks SET heartbeat_at=? WHERE id=? AND state='ATTACHED'",
                   (time.time(), task_id))


def progress(task_id: str) -> dict[str, Any] | None:
    row = db.query_one("SELECT id, owner, state, heartbeat_at, steps_used, "
                       "state_reason FROM tasks WHERE id=?", (task_id,))
    return dict(row) if row else None


def sweep_abandoned(max_idle_s: float | None = None) -> int:
    """Close attached plans whose harness stopped talking to the node.

    A laptop that crashes, sleeps or loses the network never calls close; left
    alone its plan would stay ATTACHED — and its MCP session usable — forever.
    Idle past the policy's limit, the plan is closed, on the record."""
    from .control import trust
    idle = max_idle_s if max_idle_s is not None else \
        float(trust.policy().get("plan_idle_hours", 8)) * 3600
    cutoff = time.time() - idle
    rows = db.query("SELECT id FROM tasks WHERE state='ATTACHED' AND "
                    "COALESCE(heartbeat_at, started_at, created_at) < ?", (cutoff,))
    for r in rows:
        close(r["id"], "TERMINATED",
              f"abandoned: no contact from the client harness for "
              f"{idle / 3600:.1f} h")
    return len(rows)


def close(task_id: str, outcome: str = "COMPLETED", reason: str = "") -> None:
    db.execute("UPDATE tasks SET state=?, state_reason=?, finished_at=? "
               "WHERE id=? AND state='ATTACHED'",
               (outcome, reason or "the client's loop ended", time.time(), task_id))
