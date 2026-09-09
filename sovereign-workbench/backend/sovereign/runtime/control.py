"""Per-task execution control handle.

The scheduler never reaches into a running agent. Instead each task gets a
`TaskControl`, and the agent cooperates with it at *workflow boundaries* -- after
a tool observation, before the next reasoning step. That is deliberately not
token-level preemption: pausing between steps needs no support from the inference
engine, so a backend change cannot break preemption.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .. import audit, db
from ..config import CHECKPOINT_DIR, settings


class Paused(Exception):
    """Raised inside a runner when the scheduler has asked it to yield."""


class Cancelled(Exception):
    """Raised inside a runner when the task was terminated."""


class BudgetExceeded(Exception):
    """Raised when the task exhausted its wall-clock or step budget."""


@dataclass
class TaskControl:
    task_id: str
    priority: str
    started_at: float = field(default_factory=time.time)
    time_budget_s: float = float(settings.limits.task_time_budget_s)
    max_steps: int = settings.limits.max_agent_steps

    _pause: threading.Event = field(default_factory=threading.Event)
    _cancel: threading.Event = field(default_factory=threading.Event)
    _resume: threading.Event = field(default_factory=threading.Event)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    step: int = 0
    pause_reason: str = ""
    on_event: Callable[[dict[str, Any]], None] | None = None

    # -- scheduler side ---------------------------------------------------
    def request_pause(self, reason: str) -> None:
        with self._lock:
            self.pause_reason = reason
            self._resume.clear()
            self._pause.set()

    def request_resume(self) -> None:
        with self._lock:
            self._pause.clear()
            self._resume.set()

    def request_cancel(self, reason: str = "terminated by operator") -> None:
        with self._lock:
            self.pause_reason = reason
            self._cancel.set()
            self._resume.set()          # unblock anything waiting

    @property
    def pause_requested(self) -> bool:
        return self._pause.is_set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    # -- runner side ------------------------------------------------------
    def heartbeat(self) -> None:
        db.update("tasks", "id", self.task_id, {"heartbeat_at": time.time()})

    def elapsed(self) -> float:
        return time.time() - self.started_at

    def checkpoint_barrier(self, state: dict[str, Any], label: str = "") -> None:
        """Call at every safe boundary.

        Persists resumable state, then honours pause/cancel/budget. A runner that
        never calls this simply cannot be preempted -- which is why the harness
        calls it once per step.
        """
        self.heartbeat()
        self.save_checkpoint(state, label or f"step {self.step}")

        if self._cancel.is_set():
            raise Cancelled(self.pause_reason or "cancelled")
        if self.elapsed() > self.time_budget_s:
            raise BudgetExceeded(
                f"task exceeded its {self.time_budget_s:.0f}s wall-clock budget")
        if self.step >= self.max_steps:
            raise BudgetExceeded(f"task exceeded its {self.max_steps}-step budget")
        if self._pause.is_set():
            raise Paused(self.pause_reason or "preempted by higher-priority work")

    def save_checkpoint(self, state: dict[str, Any], reason: str = "") -> str:
        cid = db.new_id("ckpt")
        blob = db.jdump(state)
        db.insert("checkpoints", {
            "id": cid, "task_id": self.task_id, "step": self.step,
            "reason": reason, "state_blob": blob, "ts": time.time()})
        # Also on disk, so a control-plane restart can still resume.
        (CHECKPOINT_DIR / f"{self.task_id}.json").write_text(blob)
        return cid

    def emit(self, kind: str, label: str = "", detail: str = "",
             payload: Any = None) -> None:
        """Append to the agent trace the workbench renders."""
        with self._lock:
            seq_row = db.query_one(
                "SELECT COALESCE(MAX(seq), 0) AS s FROM task_events WHERE task_id=?",
                (self.task_id,))
            seq = (seq_row["s"] if seq_row else 0) + 1
            db.insert("task_events", {
                "task_id": self.task_id, "seq": seq, "kind": kind, "label": label,
                "detail": detail[:4000] if isinstance(detail, str) else db.jdump(detail),
                "payload": db.jdump(payload) if payload is not None else None,
                "ts": time.time()})
        ev = {"type": "task_event", "task_id": self.task_id, "seq": seq, "kind": kind,
              "label": label, "detail": detail if isinstance(detail, str) else "",
              "payload": payload}
        audit.bus.publish(ev)
        if self.on_event:
            try:
                self.on_event(ev)
            except Exception:
                pass


def latest_checkpoint(task_id: str) -> dict[str, Any] | None:
    row = db.query_one(
        "SELECT * FROM checkpoints WHERE task_id=? ORDER BY step DESC, ts DESC LIMIT 1",
        (task_id,))
    if not row:
        return None
    return {"id": row["id"], "step": row["step"], "reason": row["reason"],
            "state": db.jload(row["state_blob"], {}), "ts": row["ts"]}
