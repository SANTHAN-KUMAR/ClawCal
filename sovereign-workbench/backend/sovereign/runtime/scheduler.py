"""Resource-governed agent runtime.

This is the scheduler that decides *whether* an agent may run, *with which model*,
and *at whose expense*. Design commitments:

* **Deterministic.** No learned policy. Every admission outcome is reproducible
  from the queue state and a resource snapshot, and carries a human-readable reason.
* **Residency-batched.** Among equally eligible queued tasks, one whose model is
  already resident is preferred. Grouping queued work by model is what stops a
  single GPU from spending its day loading and unloading weights.
* **Starvation-free.** Queued tasks age; a BATCH task eventually outranks a
  freshly-arrived HIGH one rather than waiting forever.
* **Preemption at workflow boundaries.** A higher-priority task that cannot be
  admitted asks the cheapest-to-displace running task to checkpoint and yield.
  Nothing is killed mid-tool-call.
* **Refuses rather than hangs.** A task whose context estimate exceeds the live
  KV budget is rejected with an explanation, not queued into a silent stall.
"""
from __future__ import annotations

import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

from .. import audit, db, hardware, router
from ..config import settings
from ..gateway import gateway
from ..gateway.registry import registry
from .control import BudgetExceeded, Cancelled, Paused, TaskControl, latest_checkpoint
from .residency import residency

# A runner takes (task_row, control, resume_state) and returns a result dict.
Runner = Callable[[dict[str, Any], TaskControl, dict[str, Any] | None], dict[str, Any]]

TERMINAL_STATES = {"COMPLETED", "FAILED", "TERMINATED", "REJECTED"}


# Workflows that never call a model. Routing them anyway made a sovereignty
# self-test load a 12 GB model it would never use — on the test machine that
# load is what pushed the host into the OOM killer.
MODEL_FREE_WORKFLOWS = {"sovereignty_proof"}


class QueueFull(RuntimeError):
    """Back-pressure: the queue is at its configured depth."""


def _default_mode() -> str:
    from ..policy.tool_policy import policy
    return policy.mode


@dataclass
class AdmissionDecision:
    outcome: str                       # ADMIT | QUEUE | DEFER | REJECT
    reason: str
    model: str = ""
    backend: str = ""
    routing: dict[str, Any] = field(default_factory=dict)
    residency: dict[str, Any] = field(default_factory=dict)
    context_budget: int = 0
    preempt: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"outcome": self.outcome, "reason": self.reason, "model": self.model,
                "backend": self.backend, "routing": self.routing,
                "residency": self.residency, "context_budget": self.context_budget,
                "preempt": self.preempt}


class Scheduler:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._running: dict[str, TaskControl] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._runners: dict[str, Runner] = {}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._loop_thread: threading.Thread | None = None
        self._last_admission: list[dict[str, Any]] = []
        self._paused_for: dict[str, str] = {}   # paused task -> task that displaced it
        # Last admission outcome recorded per task. The queue is re-evaluated
        # every tick; a decision row is written only when the answer changes,
        # or the record would hold thousands of identical QUEUE rows per task.
        self._last_outcome: dict[str, tuple[str, str]] = {}

    # -- registration -----------------------------------------------------
    def register_runner(self, workflow: str, runner: Runner) -> None:
        self._runners[workflow] = runner

    def _runner_for(self, workflow: str) -> Runner | None:
        return self._runners.get(workflow) or self._runners.get("general")

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        if self._loop_thread and self._loop_thread.is_alive():
            return
        self._recover_orphans()
        self._stop.clear()
        self._loop_thread = threading.Thread(target=self._loop, name="scheduler",
                                             daemon=True)
        self._loop_thread.start()
        limit, why = residency.concurrency_limit()
        audit.record("runtime", "scheduler_started",
                     detail={"max_concurrent": limit, "reason": why})

    def _recover_orphans(self) -> None:
        """Reconcile tasks left mid-flight by a previous control-plane process.

        A row still marked RUNNING at startup belongs to a process that no longer
        exists — the control plane was restarted, or it crashed. Left alone these
        rows are counted against concurrency and per-user quotas forever, and the
        scheduler quietly stops admitting anything; that failure presents as a
        queue that never moves, with a correct-sounding reason.

        Recovery follows the architecture's rule that a control-plane failure must
        not leave work in an uncontrolled state: a task with a checkpoint becomes
        PAUSED and is resumable, and one without becomes FAILED. Neither silently
        resumes with privileges it was never re-granted.
        """
        orphans = db.rows_to_dicts(db.query(
            "SELECT id, title, state, steps_used FROM tasks "
            "WHERE state IN ('ADMITTED','RUNNING','RESUMING')"))
        if not orphans:
            return
        recovered, failed = [], []
        for o in orphans:
            ckpt = latest_checkpoint(o["id"])
            if ckpt:
                self._set_state(
                    o["id"], "PAUSED",
                    f"the control plane restarted while this task was running; it "
                    f"is resumable from its checkpoint at step {ckpt['step']}")
                recovered.append(o["id"])
            else:
                self._set_state(
                    o["id"], "FAILED",
                    "the control plane restarted while this task was running and "
                    "no checkpoint had been taken, so it cannot be resumed")
                failed.append(o["id"])
        audit.record("runtime", "orphans_recovered", outcome="DEGRADED",
                     detail={"resumable": recovered, "unrecoverable": failed,
                             "count": len(orphans)})

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    # -- submission -------------------------------------------------------
    def submit(self, *, title: str, prompt: str, workflow: str = "general",
               owner: str = "system", department: str = "default",
               priority: str | None = None,
               attachments: list[dict[str, Any]] | None = None,
               parent_id: str | None = None,
               task_type: str | None = None,
               conversation_id: str | None = None,
               policy_mode: str | None = None,
               model: str | None = None) -> str:
        depth = db.query_one("SELECT COUNT(*) AS n FROM tasks "
                             "WHERE state IN ('QUEUED','PAUSED')")["n"]
        if depth >= settings.limits.max_queue_depth:
            audit.record("task", "rejected_queue_full", outcome="REJECTED",
                         actor=owner, detail={"depth": depth})
            raise QueueFull(f"the queue holds {depth} tasks, at its configured "
                            f"limit of {settings.limits.max_queue_depth}; try again "
                            f"when some have finished")
        # An explicit workflow pins the task type; a caller who selected
        # `engineering_qa` should not have their task reclassified as extraction
        # because the prompt happens to begin with the word "extract".
        pinned = task_type or router.WORKFLOW_TASK_TYPE.get(workflow)
        # A follow-up turn inherits the conversation's documents. Asked "explain
        # this file" and then "what about the valves", the second turn arrives
        # with no attachment of its own and would otherwise be answered against
        # nothing.
        attachments = list(attachments or [])
        if conversation_id and not attachments:
            for row in db.query(
                    "SELECT attachments FROM tasks WHERE conversation_id=? "
                    "ORDER BY created_at DESC LIMIT 8", (conversation_id,)):
                prior = db.jload(row["attachments"], []) or []
                if prior:
                    attachments = prior
                    break

        cls = router.classify(prompt, attachments=attachments, model=model,
                              workflow=pinned, priority_override=priority)
        tid = db.new_id("task")
        db.insert("tasks", {
            "id": tid, "parent_id": parent_id,
            "conversation_id": conversation_id or tid,
            "title": title[:200], "prompt": prompt,
            "owner": owner, "department": department, "workflow": workflow,
            "task_type": cls.task_type, "priority": cls.priority,
            "effective_priority": float(router.PRIORITY_RANK[cls.priority]),
            "state": "QUEUED",
            "state_reason": "awaiting admission",
            "required_caps": db.jdump(cls.profile.needs),
            "est_context_tokens": cls.est_context_tokens,
            "policy_mode": policy_mode or _default_mode(),
            "attachments": db.jdump(attachments or []),
            "requested_model": cls.requested_model,
            "created_at": time.time(),
        })
        db.insert("task_events", {
            "task_id": tid, "seq": 1, "kind": "submitted", "label": "Task accepted",
            "detail": f"classified as {cls.task_type} at {cls.priority} priority "
                      f"(confidence {cls.confidence})",
            "payload": db.jdump({"signals": cls.signals,
                                 "est_context_tokens": cls.est_context_tokens}),
            "ts": time.time()})
        audit.record("task", "submitted", task_id=tid, actor=owner, detail={
            "title": title, "task_type": cls.task_type, "priority": cls.priority,
            "signals": cls.signals})
        audit.bus.publish({"type": "task_state", "task_id": tid, "state": "QUEUED"})
        self.wake()
        return tid

    # -- admission --------------------------------------------------------
    def evaluate(self, task: dict[str, Any]) -> AdmissionDecision:
        """The admission controller. Pure function of task + live resources."""
        cls = router.Classification(
            task_type=task["task_type"] or "general",
            confidence=1.0, priority=task["priority"],
            est_context_tokens=task["est_context_tokens"] or 4096,
            modality="text",
            reasoning=router.TASK_TYPES.get(task["task_type"] or "general",
                                            router.TASK_TYPES["general"]).reasoning,
            # The operator's explicit choice survives to admission. It used to be
            # parsed at submission and then dropped here, so "use gpt-oss" was
            # silently ignored.
            requested_model=task.get("requested_model"))

        # 1. Concurrency and quota.
        with self._lock:
            running = list(self._running)
        limit, limit_why = residency.concurrency_limit()
        if len(running) >= limit:
            victim = self._preemption_candidate(task, running)
            if victim:
                return AdmissionDecision(
                    "DEFER",
                    f"all {limit} agent slot(s) are busy ({limit_why}); "
                    f"{task['priority']} priority outranks running task {victim}, "
                    f"which will be asked to checkpoint and yield",
                    preempt=victim)
            return AdmissionDecision(
                "QUEUE",
                f"all {limit} agent slot(s) are busy ({limit_why}) and no running "
                f"task is lower priority than this one")

        # Count only agents this scheduler is actually running. A stale RUNNING
        # row cannot hold a quota slot; `_recover_orphans` clears those at start,
        # and intersecting with the live set makes the check robust even if one
        # slips through.
        with self._lock:
            live = set(self._running)
        owner_live = 0
        for tid in live:
            row = db.query_one("SELECT owner FROM tasks WHERE id=?", (tid,))
            if row and row["owner"] == task["owner"]:
                owner_live += 1
        if owner_live >= settings.limits.max_concurrent_per_user:
            return AdmissionDecision(
                "QUEUE",
                f"user {task['owner']} already has {owner_live} running agents, "
                f"at the per-user quota of {settings.limits.max_concurrent_per_user}")

        if task.get("workflow") in MODEL_FREE_WORKFLOWS:
            return AdmissionDecision(
                "ADMIT", f"the {task['workflow']} workflow uses no model, so it needs "
                         f"an agent slot but no VRAM or host RAM for weights",
                routing={"reason": "no model required"})

        # 2. Route. Context capacity is part of selection, so the router can
        # choose a smaller model with a cheaper KV cache rather than the task
        # being refused for not fitting the preferred one.
        resident = residency.resident_names()
        decision = router.select_model(
            cls, resident=resident,
            budget_for=residency.context_budget_tokens)
        if not decision.model:
            return AdmissionDecision("REJECT", decision.reason,
                                     routing=decision.to_dict())
        card = registry.get(decision.model)
        if card is None:
            return AdmissionDecision("REJECT", f"model {decision.model} vanished from "
                                               f"the registry", routing=decision.to_dict())

        # 3. Backend health.
        health = gateway.backend_health().get(card.backend, {})
        if health.get("status") not in ("up", "degraded", None):
            alt = gateway.fallback_chain(card)
            if not alt:
                return AdmissionDecision(
                    "REJECT",
                    f"backend {card.backend} is {health.get('status')} and no healthy "
                    f"fallback serves the required capabilities",
                    routing=decision.to_dict())
            return AdmissionDecision(
                "DEFER", f"backend {card.backend} is {health.get('status')}; deferring "
                         f"to fallback {alt[0].name}", model=alt[0].name,
                backend=alt[0].backend, routing=decision.to_dict())

        # 4. Residency feasibility.
        plan = residency.plan(card, task["priority"])
        if not plan.feasible and plan.transient and not plan.ram_bound:
            # Something resident is still inside its dwell floor. Waiting will
            # genuinely free it, so queueing is back-pressure rather than a stall.
            return AdmissionDecision("QUEUE", plan.reason, model=card.name,
                                     backend=card.backend, routing=decision.to_dict(),
                                     residency={"feasible": False,
                                                "reason": plan.reason})
        if not plan.feasible:
            # Nothing is resident to evict, so this shortfall will look exactly
            # the same in an hour. Queueing on it is a deadlock — observed in
            # use, a CRITICAL drawing task sat in QUEUED indefinitely against
            # "nothing further is evictable" while the GPU was idle. Try the
            # models that do fit, cheapest first, and only reject if none does.
            tried = [f"{card.name} ({plan.reason})"]
            for alt in gateway.fallback_chain(card):
                alt_plan = residency.plan(alt, task["priority"])
                if not alt_plan.feasible:
                    tried.append(f"{alt.name} ({alt_plan.reason})")
                    continue
                budget = residency.context_budget_tokens(alt)
                if (task["est_context_tokens"] or 0) > budget:
                    tried.append(f"{alt.name} (context budget {budget} too small)")
                    continue
                return AdmissionDecision(
                    "DEFER",
                    f"{card.name} does not fit in available VRAM and nothing is "
                    f"resident to evict, so this cannot improve by waiting. "
                    f"Falling back to {alt.name}, which fits. {alt_plan.reason}",
                    model=alt.name, backend=alt.backend,
                    routing=decision.to_dict(),
                    residency={"feasible": True, "reason": alt_plan.reason,
                               "fallback_from": card.name})
            if plan.ram_bound:
                # Host RAM is shared with the rest of the machine and frees up
                # when other programs exit, so this is back-pressure, not a
                # permanent refusal — but it waits visibly, with the numbers.
                return AdmissionDecision(
                    "QUEUE",
                    "waiting for host memory: " + " | ".join(tried[:3]),
                    model=card.name, backend=card.backend,
                    routing=decision.to_dict(),
                    residency={"feasible": False, "ram_bound": True,
                               "reason": plan.reason})
            return AdmissionDecision(
                "REJECT",
                "no available model fits in this machine's VRAM right now, and "
                "nothing is resident that could be evicted to make room. "
                + " | ".join(tried[:4]),
                model=card.name, backend=card.backend,
                routing=decision.to_dict(),
                residency={"feasible": False, "reason": plan.reason})

        # 5. Context / KV budget. The router has already excluded models that
        # cannot hold this task, so reaching here with a shortfall means no model
        # can. Refuse with an explanation rather than queueing into a silent
        # stall, which is the documented worst failure mode of local serving.
        budget = residency.context_budget_tokens(card)
        if (task["est_context_tokens"] or 0) > budget:
            return AdmissionDecision(
                "REJECT",
                f"this task needs about {task['est_context_tokens']} context tokens; "
                f"the largest budget any available local model can offer right now "
                f"is {budget} tokens ({card.name}, after {card.residency_mb:.0f} MB "
                f"of weights). Split the document set, or free VRAM by evicting "
                f"another model.",
                model=card.name, backend=card.backend, routing=decision.to_dict(),
                context_budget=budget)

        # Grant what the task needs, not everything the machine could afford.
        # The KV cache is allocated at the size requested, so handing a 6 000-token
        # task a 28 000-token window costs real VRAM and pushes the model further
        # off the GPU. Headroom of 1.6x plus 2 000 tokens covers tool observations
        # accumulating over the trajectory; the harness trims within it.
        needed = int((task["est_context_tokens"] or 4096) * 1.6) + 2048
        granted = max(2048, min(budget, needed))

        return AdmissionDecision(
            "ADMIT",
            f"{decision.reason}. {plan.reason}. Context granted {granted} tokens "
            f"of {budget} affordable, sized to this task's estimated "
            f"{task['est_context_tokens']}.",
            model=card.name, backend=card.backend, routing=decision.to_dict(),
            residency={"feasible": True, "reason": plan.reason,
                       "evictions": plan.evictions,
                       "load_cost_s": round(plan.load_cost_s, 1),
                       "already_resident": plan.already_resident},
            context_budget=granted)

    def _preemption_candidate(self, task: dict[str, Any],
                              running: list[str]) -> str | None:
        """Lowest-priority running task strictly below the incoming one."""
        incoming = router.PRIORITY_RANK.get(task["priority"], 2)
        best: tuple[int, float, str] | None = None
        for tid in running:
            row = db.query_one("SELECT priority, started_at FROM tasks WHERE id=?", (tid,))
            if not row:
                continue
            rank = router.PRIORITY_RANK.get(row["priority"], 2)
            if rank >= incoming:
                continue
            key = (rank, -(row["started_at"] or 0.0), tid)
            if best is None or key < best:
                best = key
        return best[2] if best else None

    # -- queue ordering ---------------------------------------------------
    def _ordered_queue(self) -> list[dict[str, Any]]:
        """Priority, then aging, then residency batching, then FIFO."""
        rows = db.rows_to_dicts(db.query(
            "SELECT * FROM tasks WHERE state IN ('QUEUED','PAUSED') "
            "ORDER BY created_at ASC"))
        if not rows:
            return []
        resident = set(residency.resident_names())
        now = time.time()
        scored = []
        for r in rows:
            base = float(router.PRIORITY_RANK.get(r["priority"], 2))
            waited = now - (r["created_at"] or now)
            aged = base + waited / settings.limits.priority_aging_s
            # A paused task outranks a fresh one of equal priority: finishing work
            # in flight beats starting more of it.
            if r["state"] == "PAUSED":
                aged += 0.75
            batch_bonus = 0.5 if (r.get("selected_model") in resident) else 0.0
            # Written only when it moved, so a long queue is not rewritten in
            # full every tick.
            if abs((r.get("effective_priority") or 0.0) - aged) > 0.05:
                db.update("tasks", "id", r["id"], {"effective_priority": aged})
            r["effective_priority"] = round(aged, 3)
            scored.append((-(aged + batch_bonus), r["created_at"] or 0.0, r))
        scored.sort(key=lambda x: (x[0], x[1]))
        return [s[2] for s in scored]

    # -- main loop --------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                audit.record("runtime", "scheduler_error", outcome="FAILED",
                             detail=traceback.format_exc()[:2000])
            self._wake.wait(timeout=1.5)
            self._wake.clear()

    def _tick(self) -> None:
        self._reap_dead()
        for task in self._ordered_queue():
            with self._lock:
                if len(self._running) >= settings.limits.max_concurrent_agents:
                    # Still evaluate, so a high-priority arrival can trigger preemption.
                    pass
            decision = self.evaluate(task)
            self._record_admission(task, decision)

            if decision.outcome == "ADMIT":
                self._launch(task, decision)
            elif decision.outcome == "DEFER" and decision.preempt:
                self._preempt(decision.preempt, task)
                break                    # re-evaluate next tick once the slot frees
            elif decision.outcome == "REJECT":
                self._set_state(task["id"], "REJECTED", decision.reason)
                audit.record("task", "rejected", outcome="REJECTED",
                             task_id=task["id"], detail=decision.to_dict())
            else:
                if task["state"] == "QUEUED" and task["state_reason"] != decision.reason:
                    db.update("tasks", "id", task["id"],
                              {"state_reason": decision.reason})

    def _record_admission(self, task: dict[str, Any],
                          decision: AdmissionDecision) -> None:
        entry = {"task_id": task["id"], "title": task["title"],
                 "priority": task["priority"], "ts": time.time(),
                 **decision.to_dict()}
        with self._lock:
            self._last_admission.append(entry)
            self._last_admission = self._last_admission[-80:]
        audit.bus.publish({"type": "admission", **{k: entry[k] for k in
                                                   ("task_id", "outcome", "reason",
                                                    "model", "priority")}})
        # The verdict, not its live numbers. A "waiting for host memory" reason
        # embeds the current free RAM, which moves every tick; keyed on the raw
        # text, one waiting task wrote 471 identical QUEUE decisions.
        import re as _re
        key = (decision.outcome, _re.sub(r"\d+(?:\.\d+)?", "#", decision.reason))
        with self._lock:
            changed = self._last_outcome.get(task["id"]) != key
            self._last_outcome[task["id"]] = key
            if len(self._last_outcome) > 4096:
                self._last_outcome.clear()
        if changed:
            from ..control import decisions
            basis: dict[str, Any] = {"priority": task["priority"],
                                     "model": decision.model,
                                     "context_budget": decision.context_budget,
                                     "residency": decision.residency}
            if decision.outcome in ("QUEUE", "DEFER"):
                try:
                    from . import perfmodel
                    basis["wait"] = perfmodel.wait_estimate(task["id"])
                except Exception as exc:          # a missing estimate is not fatal
                    basis["wait"] = {"basis": "unknown", "why": str(exc)[:120]}
            decisions.record("admission", decision.outcome, decision.reason,
                             subject_kind="task", subject_id=task["id"],
                             task_id=task["id"], principal=task.get("owner"),
                             basis=basis)

    def _launch(self, task: dict[str, Any], decision: AdmissionDecision) -> None:
        tid = task["id"]
        card = registry.get(decision.model) if decision.model else None
        if card is None and decision.model:
            self._set_state(tid, "FAILED", "selected model disappeared")
            return

        resume = latest_checkpoint(tid) if task["state"] == "PAUSED" else None
        db.update("tasks", "id", tid, {
            "state": "ADMITTED", "state_reason": decision.reason,
            "selected_model": decision.model, "selected_backend": decision.backend,
            "routing_reason": decision.routing.get("reason", ""),
            "admitted_at": time.time(),
            "queue_wait_s": time.time() - (task["created_at"] or time.time()),
            "est_vram_mb": int(card.residency_mb) if card else 0,
        })
        audit.record("task", "admitted", task_id=tid, detail=decision.to_dict())
        from ..control import decisions
        routing = decision.routing or {}
        decisions.record(
            "routing", decision.model or "NO_MODEL",
            routing.get("reason") or decision.reason,
            subject_kind="task", subject_id=tid, task_id=tid,
            principal=task.get("owner"),
            basis={"task_type": task.get("task_type"),
                   "capability_fit": routing.get("capability_fit"),
                   "resident_reuse": routing.get("resident_reuse"),
                   "considered": [{k: c.get(k) for k in ("model", "fit", "score",
                                                         "resident", "load_s")}
                                  for c in (routing.get("considered") or [])],
                   "rejected": routing.get("rejected") or []})

        ctl = TaskControl(task_id=tid, priority=task["priority"])
        ctl.step = (resume or {}).get("step", 0)
        with self._lock:
            self._running[tid] = ctl

        ctl.emit("admitted", "Admission granted", decision.reason,
                 {"model": decision.model, "backend": decision.backend,
                  "routing": decision.routing, "residency": decision.residency,
                  "context_budget": decision.context_budget})

        th = threading.Thread(target=self._run_task,
                              args=(dict(task), ctl, decision, resume),
                              name=f"agent-{tid}", daemon=True)
        with self._lock:
            self._threads[tid] = th
        th.start()

    def _run_task(self, task: dict[str, Any], ctl: TaskControl,
                  decision: AdmissionDecision, resume: dict[str, Any] | None) -> None:
        tid = task["id"]
        card = registry.get(decision.model) if decision.model else None
        try:
            # Make the model resident before flipping to RUNNING, and charge the
            # measured cost to the trace so the operator sees where time went.
            if card:
                plan = residency.plan(card, task["priority"])
                if plan.evictions:
                    ctl.emit("residency", "Evicting to make room",
                             f"evicting {', '.join(plan.evictions)} to load "
                             f"{card.name}", {"evictions": plan.evictions})
                cost = residency.apply(card, plan, task_id=tid)
                if cost > 0.5:
                    ctl.emit("residency", "Model made resident",
                             f"{card.name} loaded in {cost:.1f}s "
                             f"({card.residency_mb:.0f} MB)", {"seconds": cost})

            self._set_state(tid, "RUNNING", "agent executing")
            db.update("tasks", "id", tid, {"started_at": time.time()})
            ctl.started_at = time.time()

            task["selected_model"] = decision.model
            task["context_budget"] = decision.context_budget
            task.setdefault("conversation_id", task.get("conversation_id"))
            runner = self._runner_for(task["workflow"])
            if runner is None:
                raise RuntimeError(f"no runner registered for workflow "
                                   f"{task['workflow']!r}")
            result = runner(task, ctl, (resume or {}).get("state"))

            row = db.query_one("SELECT state FROM tasks WHERE id=?", (tid,))
            if row and row["state"] == "TERMINATED":
                return          # stopped mid-flight; do not report success
            db.update("tasks", "id", tid, {
                "result": db.jdump(result), "finished_at": time.time(),
                "runtime_s": ctl.elapsed(), "steps_used": ctl.step})
            self._set_state(tid, "COMPLETED", "finished")
            ctl.emit("completed", "Task complete",
                     result.get("summary", "") if isinstance(result, dict) else "")
            audit.record("task", "completed", task_id=tid,
                         detail={"runtime_s": round(ctl.elapsed(), 1),
                                 "steps": ctl.step})

        except Paused as exc:
            db.update("tasks", "id", tid, {"runtime_s": ctl.elapsed(),
                                           "steps_used": ctl.step})
            self._set_state(tid, "PAUSED", str(exc))
            ctl.emit("paused", "Checkpointed and paused", str(exc))
            audit.record("task", "paused", outcome="PAUSED", task_id=tid,
                         detail={"reason": str(exc), "step": ctl.step})

        except Cancelled as exc:
            row = db.query_one("SELECT state FROM tasks WHERE id=?", (tid,))
            if not row or row["state"] != "TERMINATED":
                self._set_state(tid, "TERMINATED", str(exc))
                ctl.emit("terminated", "Task terminated", str(exc))
            audit.record("task", "terminated", outcome="TERMINATED", task_id=tid,
                         detail=str(exc))

        except BudgetExceeded as exc:
            self._set_state(tid, "FAILED", str(exc))
            ctl.emit("failed", "Budget exceeded", str(exc))
            audit.record("task", "budget_exceeded", outcome="FAILED", task_id=tid,
                         detail=str(exc))

        except Exception as exc:
            row = db.query_one("SELECT state FROM tasks WHERE id=?", (tid,))
            if row and row["state"] == "TERMINATED":
                return          # the operator already stopped this
            tb = traceback.format_exc()
            db.update("tasks", "id", tid, {"error": tb[:4000]})
            self._set_state(tid, "FAILED", f"{type(exc).__name__}: {exc}"[:400])
            ctl.emit("failed", "Task failed", f"{type(exc).__name__}: {exc}"[:400])
            audit.record("task", "failed", outcome="FAILED", task_id=tid,
                         detail=tb[:2000])

        finally:
            with self._lock:
                self._running.pop(tid, None)
                self._threads.pop(tid, None)
                displaced = [k for k, v in self._paused_for.items() if v == tid]
                for k in displaced:
                    self._paused_for.pop(k, None)
            self.wake()

    # -- preemption / control --------------------------------------------
    def _preempt(self, victim_id: str, incoming: dict[str, Any]) -> None:
        with self._lock:
            ctl = self._running.get(victim_id)
            if ctl is None or ctl.pause_requested:
                return
            self._paused_for[victim_id] = incoming["id"]
        reason = (f"preempted at a workflow checkpoint so {incoming['priority']} "
                  f"priority task {incoming['id']} ({incoming['title'][:60]}) can run")
        ctl.request_pause(reason)
        db.update("tasks", "id", victim_id, {"state_reason": reason})
        ctl.emit("preempt_requested", "Pause requested", reason)
        audit.record("runtime", "preemption_requested", outcome="PAUSED",
                     task_id=victim_id,
                     detail={"displaced_by": incoming["id"], "reason": reason})

    def pause(self, task_id: str, reason: str = "paused on request") -> bool:
        with self._lock:
            ctl = self._running.get(task_id)
        if not ctl:
            return False
        ctl.request_pause(reason)
        return True

    def resume(self, task_id: str) -> bool:
        row = db.query_one("SELECT state FROM tasks WHERE id=?", (task_id,))
        if not row or row["state"] != "PAUSED":
            return False
        self._set_state(task_id, "QUEUED", "requeued for resumption from checkpoint")
        self.wake()
        return True

    def cancel(self, task_id: str, reason: str = "terminated on request") -> bool:
        with self._lock:
            ctl = self._running.get(task_id)
        if ctl:
            ctl.request_cancel(reason)
            # The agent only notices at its next checkpoint, which can be a
            # generation away — up to a minute. Pressing Stop and watching
            # nothing happen for a minute reads as a broken button, so the task
            # is marked terminated now and its slot released; the thread winds
            # down behind that and cannot resurrect the state, because
            # `_run_task` only writes terminal states it owns.
            self._set_state(task_id, "TERMINATED", reason)
            ctl.emit("terminated", "Stopped on request", reason)
            with self._lock:
                self._running.pop(task_id, None)
            self.wake()
            return True
        row = db.query_one("SELECT state FROM tasks WHERE id=?", (task_id,))
        if row and row["state"] not in TERMINAL_STATES:
            self._set_state(task_id, "TERMINATED", reason)
            return True
        return False

    # -- health -----------------------------------------------------------
    def _reap_dead(self) -> None:
        """Detect agents that stopped heartbeating and fail them explicitly."""
        cutoff = time.time() - settings.limits.heartbeat_grace_s
        with self._lock:
            live = dict(self._running)
        for tid, ctl in live.items():
            row = db.query_one("SELECT heartbeat_at, started_at FROM tasks WHERE id=?",
                               (tid,))
            if not row:
                continue
            last = row["heartbeat_at"] or row["started_at"] or time.time()
            th = self._threads.get(tid)
            if last < cutoff and (th is None or not th.is_alive()):
                self._set_state(tid, "FAILED",
                                f"agent stopped heartbeating for more than "
                                f"{settings.limits.heartbeat_grace_s:.0f}s")
                audit.record("runtime", "agent_unresponsive", outcome="FAILED",
                             task_id=tid)
                with self._lock:
                    self._running.pop(tid, None)
                    self._threads.pop(tid, None)

    def _set_state(self, task_id: str, state: str, reason: str = "") -> None:
        db.update("tasks", "id", task_id, {"state": state, "state_reason": reason[:500]})
        audit.bus.publish({"type": "task_state", "task_id": task_id, "state": state,
                           "reason": reason})

    # -- introspection ----------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            running = list(self._running)
            admissions = list(self._last_admission[-25:])
            paused_for = dict(self._paused_for)
        queued = db.rows_to_dicts(db.query(
            "SELECT id,title,priority,effective_priority,state,state_reason,"
            "task_type,selected_model,created_at FROM tasks "
            "WHERE state IN ('QUEUED','PAUSED') ORDER BY effective_priority DESC"))
        active = db.rows_to_dicts(db.query(
            "SELECT id,title,priority,state,task_type,selected_model,started_at,"
            "steps_used FROM tasks WHERE state IN ('ADMITTED','RUNNING')"))
        return {
            "running": active,
            "running_ids": running,
            "queued": queued,
            "paused_for": paused_for,
            "admissions": admissions,
            "residency": residency.state(),
            "hardware": hardware.sampler.latest,
            "limits": {
                "max_concurrent_agents": residency.concurrency_limit()[0],
                "concurrency_reason": residency.concurrency_limit()[1],
                "configured_ceiling": settings.limits.max_concurrent_agents,
                "max_concurrent_per_user": settings.limits.max_concurrent_per_user,
                "min_residency_dwell_s": settings.limits.min_residency_dwell_s,
                "priority_aging_s": settings.limits.priority_aging_s,
                "task_time_budget_s": settings.limits.task_time_budget_s,
            },
        }


scheduler = Scheduler()
