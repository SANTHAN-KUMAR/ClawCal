"""Model residency manager.

The core claim of this project is that on one mid-range GPU, model selection is a
memory-residency decision. This module is where that decision is actually made and
measured.

It tracks which models hold VRAM, enforces a minimum dwell time so the scheduler
cannot oscillate, decides whether a new model can be admitted alongside the current
set or requires an eviction, and records the measured cost of every transition so
future admissions use real numbers rather than estimates.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .. import audit, db, hardware
from ..config import settings
from ..gateway import gateway
from ..gateway.registry import ModelCard, registry


@dataclass
class ResidentEntry:
    model: str
    since: float
    vram_mb: float
    gpu_fraction: float = 1.0
    cpu_mb: float = 0.0              # the part the backend placed in host RAM
    pinned_until: float = 0.0
    tasks_served: int = 0
    last_used: float = field(default_factory=time.time)

    @property
    def dwell_s(self) -> float:
        return time.time() - self.since


@dataclass
class ResidencyPlan:
    feasible: bool
    reason: str
    evictions: list[str] = field(default_factory=list)
    load_cost_s: float = 0.0
    vram_after_mb: float = 0.0
    already_resident: bool = False
    # The shortfall is host RAM, not VRAM. RAM is shared with everything else on
    # the machine, so it can free up by itself; but a smaller model that fits
    # now is better than waiting for one that does not.
    ram_bound: bool = False
    # Whether waiting could ever change the answer. An infeasible plan with
    # nothing resident to evict will still be infeasible in an hour, so queueing
    # on it is a deadlock rather than back-pressure — the scheduler needs to
    # know the difference and fall back to a smaller model instead.
    transient: bool = True


class ResidencyManager:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._entries: dict[str, ResidentEntry] = {}
        self._transitions: list[dict[str, Any]] = []
        # When VRAM was first seen held by no resident model. A backend reports
        # an unload before the GPU releases the memory, so for a while the
        # memory looks "foreign"; treating it as permanent rejected a task that
        # would have fitted three seconds later.
        self._unattributed_since: float | None = None

    # -- observation ------------------------------------------------------
    def refresh(self) -> dict[str, ResidentEntry]:
        """Reconcile our view with what the backend actually holds."""
        with self._lock:
            live = {}
            for m in gateway.resident():
                name = m.get("model") or m.get("name")
                if not name:
                    continue
                prev = self._entries.get(name)
                live[name] = ResidentEntry(
                    model=name,
                    since=prev.since if prev else time.time(),
                    vram_mb=m.get("vram_mb", 0.0),
                    gpu_fraction=m.get("gpu_fraction", 1.0),
                    cpu_mb=m.get("cpu_mb", 0.0) or 0.0,
                    pinned_until=prev.pinned_until if prev else 0.0,
                    tasks_served=prev.tasks_served if prev else 0,
                    last_used=prev.last_used if prev else time.time(),
                )
            self._entries = live
            return dict(live)

    def resident_names(self) -> list[str]:
        return list(self.refresh().keys())

    def state(self) -> dict[str, Any]:
        entries = self.refresh()
        snap = hardware.snapshot()
        return {
            "resident": [
                {"model": e.model, "vram_mb": round(e.vram_mb, 1),
                 "gpu_fraction": e.gpu_fraction, "dwell_s": round(e.dwell_s, 1),
                 "tasks_served": e.tasks_served,
                 "evictable": e.dwell_s >= settings.limits.min_residency_dwell_s}
                for e in entries.values()
            ],
            "free_vram_mb": round(snap["free_vram_mb"], 1),
            "total_vram_mb": round(snap["gpu"].get("total_mb", 0.0), 1),
            "usable_vram_mb": round(snap["usable_vram_mb"], 1),
            "free_ram_mb": round(snap["memory"].get("available_mb", 0.0), 1),
            "min_dwell_s": settings.limits.min_residency_dwell_s,
            "recent_transitions": self._transitions[-12:],
        }

    # -- planning ---------------------------------------------------------
    def plan(self, card: ModelCard, priority: str = "MEDIUM") -> ResidencyPlan:
        """Can this model be made resident, and at what cost?

        Returns a plan rather than acting, so the admission controller can reject
        or queue a task *before* any VRAM is disturbed.
        """
        with self._lock:
            entries = self.refresh()
            snap = hardware.snapshot()
            need = card.residency_mb

            # VRAM this appliance may allocate, and how much of it our own
            # resident models are holding. `usable_vram_mb` already excludes the
            # reserve, so subtracting the reserve again from free VRAM — as this
            # did — double-charges it and rejects a model that fits. gpt-oss
            # needs 7241 MB against 7488 MB usable and was refused for want of
            # 147 MB that had been deducted twice.
            usable = snap["usable_vram_mb"]
            ours = sum(e.vram_mb for e in entries.values())
            # Anything held by processes we do not manage (the display server,
            # another tenant) is genuinely unavailable.
            foreign = max(0.0, snap["gpu"].get("used_mb", 0.0) - ours)
            headroom = max(0.0, usable - foreign - ours)
            if foreign > 512:
                if self._unattributed_since is None:
                    self._unattributed_since = time.time()
            else:
                self._unattributed_since = None
            in_transition = (self._unattributed_since is not None
                             and time.time() - self._unattributed_since < 60.0)

            if card.name in entries:
                e = entries[card.name]
                return ResidencyPlan(True, f"{card.name} already resident "
                                           f"({e.vram_mb:.0f} MB, dwell "
                                           f"{e.dwell_s:.0f}s)",
                                     already_resident=True,
                                     vram_after_mb=snap["free_vram_mb"])

            fits = min(need, usable)
            if fits <= headroom:
                ok, why, _, _ = hardware.memory_verdict(card, headroom, snap)
                if not ok:
                    return ResidencyPlan(False, why, transient=True, ram_bound=True)
                return ResidencyPlan(True,
                                     f"{card.name} fits in {headroom:.0f} MB of free "
                                     f"VRAM without evicting anything; {why}",
                                     load_cost_s=card.cold_load_s,
                                     vram_after_mb=headroom - need)

            # Need to evict. Choose victims by (lowest priority of served work,
            # longest idle), respecting the dwell floor.
            victims: list[str] = []
            reclaimed = 0.0
            blocked: list[str] = []
            for e in sorted(entries.values(), key=lambda x: (x.last_used, -x.dwell_s)):
                if e.dwell_s < settings.limits.min_residency_dwell_s and priority != "CRITICAL":
                    blocked.append(
                        f"{e.model} has held VRAM for only {e.dwell_s:.0f}s of the "
                        f"{settings.limits.min_residency_dwell_s:.0f}s minimum dwell")
                    continue
                victims.append(e.model)
                reclaimed += e.vram_mb
                if headroom + reclaimed >= fits:
                    break

            if headroom + reclaimed >= fits:
                freed_ram = sum(entries[v].cpu_mb for v in victims if v in entries)
                ok, why, _, _ = hardware.memory_verdict(
                    card, headroom + reclaimed, snap, freed_ram_mb=freed_ram)
                if not ok:
                    return ResidencyPlan(False, why, transient=True, ram_bound=True)
                evict_cost = sum(
                    (registry.get(v).cold_load_s if registry.get(v) else 0.0)
                    for v in victims)
                return ResidencyPlan(
                    True,
                    f"admitting {card.name} requires evicting {', '.join(victims)} to "
                    f"reclaim {reclaimed:.0f} MB; total residency cost about "
                    f"{card.cold_load_s + evict_cost * 0.15:.0f}s",
                    evictions=victims,
                    load_cost_s=card.cold_load_s + evict_cost * 0.15,
                    vram_after_mb=headroom + reclaimed - need)

            # Could waiting help? Only if something is currently resident that
            # will become evictable once its dwell expires. With nothing
            # resident, or nothing left under the dwell floor, the shortfall is
            # permanent and the caller must pick a different model.
            transient = bool(blocked) or in_transition
            if in_transition and not blocked:
                blocked.append(f"{foreign:.0f} MB of VRAM is held by no resident "
                               f"model — a load or unload in progress, or another "
                               f"process; re-checking")
            detail = ("; ".join(blocked) if blocked
                      else ("nothing is resident to evict, so waiting cannot free "
                            "any more" if not entries
                            else "nothing further is evictable"))
            return ResidencyPlan(
                False,
                f"{card.name} needs {fits:.0f} MB of VRAM but only "
                f"{headroom + reclaimed:.0f} MB can be made available "
                f"({detail})",
                transient=transient)

    # -- action -----------------------------------------------------------
    def apply(self, card: ModelCard, plan: ResidencyPlan,
              task_id: str | None = None) -> float:
        """Execute a plan. Returns measured seconds spent on residency changes."""
        if plan.already_resident:
            with self._lock:
                if card.name in self._entries:
                    self._entries[card.name].tasks_served += 1
                    self._entries[card.name].last_used = time.time()
            return 0.0

        t0 = time.time()
        for victim in plan.evictions:
            vc = registry.get(victim)
            if vc:
                gateway.evict(vc, reason=f"make room for {card.name}")
        cost = gateway.pin(card, seconds=int(settings.limits.min_residency_dwell_s * 20))
        total = round(time.time() - t0, 2)

        with self._lock:
            self._transitions.append({
                "model": card.name, "evicted": plan.evictions,
                "measured_s": total, "planned_s": round(plan.load_cost_s, 2),
                "ts": time.time()})
            self._transitions = self._transitions[-64:]
            self.refresh()
            if card.name in self._entries:
                self._entries[card.name].tasks_served += 1

        # Feed the measurement back so the next plan is more accurate than this one.
        self._learn_load_cost(card, total)
        audit.record("runtime", "residency_change", task_id=task_id, detail={
            "model": card.name, "evicted": plan.evictions,
            "measured_s": total, "planned_s": round(plan.load_cost_s, 2),
            "reason": plan.reason})
        from ..control import decisions
        decisions.record("residency", "LOADED", plan.reason, subject_kind="model",
                         subject_id=card.name, task_id=task_id,
                         basis={"evicted": plan.evictions, "measured_s": total,
                                "planned_s": round(plan.load_cost_s, 2),
                                "planned_basis": _load_basis(card)})
        return total

    def _learn_load_cost(self, card: ModelCard, measured_s: float) -> None:
        resident_mb = 0.0
        for m in gateway.resident():
            if (m.get("model") or m.get("name")) == card.name:
                resident_mb = float(m.get("vram_mb", 0.0) or 0.0)
        from . import perfmodel
        perfmodel.record_load(card.name, measured_s, resident_mb)

    def evict_all(self, reason: str = "operator requested") -> list[str]:
        evicted = []
        for name in list(self.refresh()):
            c = registry.get(name)
            if c and gateway.evict(c, reason=reason):
                evicted.append(name)
        self.refresh()
        return evicted

    def concurrency_limit(self) -> tuple[int, str]:
        """How many agents may run at once on this machine, and why.

        Concurrency is a memory question, not a preference. Two 5-7 GB models
        plus their KV caches do not fit in 8 GB of VRAM, so admitting two agents
        that need different models does not give parallelism — it gives eviction
        thrash, or two agents sharing one model and halving each other's decode
        rate. Where only one model fits, the honest limit is one, and a
        higher-priority task then displaces rather than joins.
        """
        configured = settings.limits.max_concurrent_agents
        snap = hardware.snapshot()
        usable = snap["usable_vram_mb"]
        cards = [c for c in registry.all() if c.cap("text") >= 0.5]
        if not cards or usable <= 0:
            return configured, "no hardware profile available; using the configured limit"

        typical = sorted(c.residency_mb for c in cards)[len(cards) // 2]
        fits = int(usable // typical) if typical else configured
        limit = max(1, min(configured, fits))
        return limit, (
            f"{usable:.0f} MB of allocatable VRAM holds about {fits} model(s) of the "
            f"typical {typical:.0f} MB footprint, so at most {limit} agent(s) may run "
            f"concurrently (configured ceiling {configured})")

    def context_budget_tokens(self, card: ModelCard) -> int:
        """How many context tokens are actually affordable right now.

        This is the guard against the documented worst failure mode of local
        serving: a request larger than the KV pool that is queued forever with no
        error. We refuse such a task at admission instead of hanging on it.
        """
        snap = hardware.snapshot()
        weights = min(card.residency_mb, snap["usable_vram_mb"])
        kv_mb = max(0.0, snap["usable_vram_mb"] - weights)
        per_1k = card.kv_cost_per_1k

        # Tokens that fit in the VRAM left after the weights.
        by_vram = int((kv_mb / per_1k) * 1000) if per_1k > 0 else 0

        # A model too large for VRAM already runs part-offloaded; its KV cache
        # spills to host RAM, which is far slower but not unbounded. Allow a
        # bounded amount so a deep reasoner is usable rather than unusable.
        if (card.weights_mb or card.residency_mb) > snap["usable_vram_mb"]:
            spare_ram = max(0.0, snap["memory"].get("available_mb", 0.0)
                            - settings.limits.ram_reserve_mb)
            by_ram = int((min(spare_ram, 2048.0) / per_1k) * 1000) if per_1k else 0
            by_vram = max(by_vram, by_ram)

        budget = min(card.ctx_max, max(2048, by_vram))
        return int(budget * settings.limits.kv_safety_factor)


def _load_basis(card: ModelCard) -> str:
    try:
        from . import perfmodel
        return perfmodel.metric(card, "cold_load_s")["basis"]
    except Exception:
        return "unknown"


residency = ResidencyManager()
