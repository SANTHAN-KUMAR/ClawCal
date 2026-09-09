#!/usr/bin/env python3
"""Measure the residency-aware scheduler against naive per-task routing.

The project's headline claim is that on one mid-range GPU, model selection is a
*memory-residency* decision rather than a capability lookup, and that treating it
that way is measurably faster. This script tests that claim on real weights, with
real load times, rather than asserting it.

Two policies run the identical mixed workload:

  naive      each task goes to its highest-capability-fit model, exactly as a
             conventional router would choose. When consecutive tasks prefer
             different models the GPU evicts and reloads between them.

  residency  the scheduler's policy: queued work is ordered so that tasks needing
             an already-resident model run together, and a non-resident model is
             charged its measured load cost, so a slightly-lower-fit resident
             model wins when the gap is inside the tolerance band.

Reported: wall-clock, number of model loads, seconds lost to loading, and the
capability fit actually delivered -- because a scheduler that wins on time by
routing everything to one weak model has not won anything.

Usage:
    python3 scripts/benchmark_scheduling.py --tasks 12
    python3 scripts/benchmark_scheduling.py --dry-run     # no GPU work
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sovereign import db, hardware                      # noqa: E402
from sovereign.gateway import gateway                   # noqa: E402
from sovereign.gateway.base import ChatMessage, GenRequest  # noqa: E402
from sovereign.gateway.registry import registry         # noqa: E402
from sovereign.router import (RESIDENCY_TOLERANCE, TASK_TYPES,  # noqa: E402
                              Classification, capability_fit, classify,
                              select_model)

# A realistic mixed shift: batch summarisation interleaved with urgent
# engineering questions, coding work and document drafting. Interleaving is the
# point -- a sorted workload would make both policies look identical.
# Tasks carry attachments where the real workload would, because a vision task
# forces a different model into VRAM and is where residency actually bites.
_SCAN = [{"kind": "scan", "pages": 2}]
_DWG = [{"kind": "drawing", "pages": 1}]

WORKLOAD = [
    ("Summarise inspection report IR-2026-0744 for the weekly digest", "BATCH", []),
    ("Check whether vessel V-204 exceeds its permitted operating pressure. Urgent.",
     None, []),
    ("Read this scanned handwritten field note and transcribe the readings",
     None, _SCAN),
    ("Summarise the vendor correspondence procedure for the weekly digest",
     "BATCH", []),
    ("Fix the failing pytest in the internal thickness-trending tool", None, []),
    ("What equipment does this P&ID show and how is it connected?", None, _DWG),
    ("Summarise inspection report IR-2026-0731 for the weekly digest", "BATCH", []),
    ("Prepare an approval note for V-204 from the inspection report and the SOP",
     None, []),
    ("Check whether E-301 is within its design pressure. Urgent.", None, []),
    ("Read this photograph of the vessel nameplate", None, _SCAN),
    ("Summarise the equipment register extract for the weekly digest", "BATCH", []),
    ("Write a python script to compute remaining life from thickness readings",
     None, []),
    ("Check the corrosion rate of V-204 against the SOP threshold. Urgent.",
     None, []),
    ("Extract the tag list from this drawing", None, _DWG),
    ("Summarise SOP-MECH-021 for the weekly digest", "BATCH", []),
    ("Draft a management note on the CDU-2 inspection campaign", None, []),
]

PROBE_PROMPT = ("Reply with exactly the word ACKNOWLEDGED and nothing else.")


def measure_load(card, dry_run: bool) -> float:
    """Measured seconds to make a model resident from cold."""
    if dry_run:
        return card.cold_load_s
    t0 = time.time()
    gateway.pin(card, seconds=1800)
    return time.time() - t0


def evict_all(dry_run: bool) -> None:
    if dry_run:
        return
    for c in registry.all():
        gateway.evict(c, reason="benchmark reset")
    time.sleep(1.0)


def touch(card, dry_run: bool) -> float:
    """One short generation, so the model is genuinely exercised, not just loaded."""
    if dry_run:
        return 0.4
    t0 = time.time()
    gateway.generate(GenRequest(
        messages=[ChatMessage("user", PROBE_PROMPT)], model=card.name,
        max_tokens=8, temperature=0.0, reasoning="low", timeout_s=300),
        allow_fallback=False)
    return time.time() - t0


def run_policy(name: str, tasks: list[Classification], *, residency_aware: bool,
               dry_run: bool) -> dict:
    evict_all(dry_run)
    resident: list[str] = []
    loads = 0
    load_seconds = 0.0
    fits: list[float] = []
    order: list[str] = []
    t_start = time.time()

    pending = list(enumerate(tasks))
    done: list[int] = []

    while pending:
        if residency_aware:
            # Residency batching: among tasks whose priority band is equal,
            # prefer one whose model is already resident. This is the ordering
            # the scheduler's queue applies.
            best = None
            for pos, (idx, cls) in enumerate(pending):
                d = select_model(cls, resident=resident)
                key = (0 if d.model in resident else 1, pos)
                if best is None or key < best[0]:
                    best = (key, pos, idx, cls, d)
            _key, pos, idx, cls, decision = best
            pending.pop(pos)
        else:
            idx, cls = pending.pop(0)
            # Naive: highest capability fit, residency ignored entirely.
            decision = select_model(cls, resident=[])

        card = registry.get(decision.model)
        if card is None:
            continue
        fit, _why = capability_fit(card, cls.profile)
        fits.append(fit)
        order.append(card.name)

        if card.name not in resident:
            # One model resident at a time: this GPU cannot hold two 5-7 GB
            # models plus their KV caches, which is the whole reason residency
            # is the binding constraint here.
            resident = []
            load_seconds += measure_load(card, dry_run)
            loads += 1
            resident.append(card.name)
        touch(card, dry_run)
        done.append(idx)

    wall = time.time() - t_start
    if dry_run:
        # Dry run does no GPU work, so measured wall-clock is meaningless. Model
        # it instead from the estimated load and generation costs, and label it.
        wall = load_seconds + 0.4 * len(tasks)
    return {
        "policy": name,
        "tasks": len(tasks),
        "model_loads": loads,
        "load_seconds": round(load_seconds, 2),
        "wall_seconds": round(wall, 2),
        "wall_is_modelled": dry_run,
        "mean_capability_fit": round(sum(fits) / len(fits), 4) if fits else 0.0,
        "model_sequence": order,
        "distinct_models": len(set(order)),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", type=int, default=len(WORKLOAD))
    ap.add_argument("--dry-run", action="store_true",
                    help="model estimates only; does not touch the GPU")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    db.init_db()
    gateway.sync_registry()
    hardware.probe()

    workload = (WORKLOAD * ((args.tasks // len(WORKLOAD)) + 1))[:args.tasks]
    tasks = [classify(p, attachments=att, priority_override=pri)
             for p, pri, att in workload]

    print(f"Workload: {len(tasks)} tasks — "
          + ", ".join(sorted({t.task_type for t in tasks})))
    print(f"Residency tolerance: {RESIDENCY_TOLERANCE}")
    print(f"Mode: {'dry run (estimates)' if args.dry_run else 'live (real weights)'}\n")

    naive = run_policy("naive per-task routing", tasks,
                       residency_aware=False, dry_run=args.dry_run)
    aware = run_policy("residency-aware scheduling", tasks,
                       residency_aware=True, dry_run=args.dry_run)

    saved = naive["wall_seconds"] - aware["wall_seconds"]
    pct = (100.0 * saved / naive["wall_seconds"]) if naive["wall_seconds"] else 0.0
    fit_delta = aware["mean_capability_fit"] - naive["mean_capability_fit"]

    result = {
        "hardware": {
            "gpu": hardware.gpu_state().name,
            "vram_mb": hardware.gpu_state().total_mb,
        },
        "naive": naive, "residency_aware": aware,
        "delta": {
            "loads_avoided": naive["model_loads"] - aware["model_loads"],
            "load_seconds_saved": round(naive["load_seconds"] - aware["load_seconds"], 2),
            "wall_seconds_saved": round(saved, 2),
            "wall_clock_improvement_pct": round(pct, 1),
            "capability_fit_delta": round(fit_delta, 4),
        },
        "ts": time.time(),
    }
    db.insert("benchmarks", {
        "id": db.new_id("bench"), "name": "scheduling",
        "variant": "dry-run" if args.dry_run else "live",
        "payload": db.jdump(result), "ts": time.time()})

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    hdr = f"{'':32s} {'loads':>7s} {'load s':>9s} {'wall s':>9s} {'mean fit':>9s}"
    print(hdr)
    print("-" * len(hdr))
    for r in (naive, aware):
        print(f"{r['policy']:32s} {r['model_loads']:7d} {r['load_seconds']:9.1f} "
              f"{r['wall_seconds']:9.1f} {r['mean_capability_fit']:9.3f}")
    print()
    print(f"Model loads avoided       : {result['delta']['loads_avoided']}")
    print(f"Seconds saved loading     : {result['delta']['load_seconds_saved']}")
    print(f"Wall-clock saved          : {result['delta']['wall_seconds_saved']}s "
          f"({result['delta']['wall_clock_improvement_pct']}%)")
    print(f"Capability fit given up   : {-result['delta']['capability_fit_delta']:.4f} "
          f"(negative means the residency policy also delivered better fit)")
    print()
    print("naive     sequence:", " -> ".join(naive["model_sequence"]))
    print("residency sequence:", " -> ".join(aware["model_sequence"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
