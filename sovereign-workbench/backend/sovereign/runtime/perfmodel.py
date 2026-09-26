"""Every performance number carries its basis (v2 §4, principle 5).

A figure the scheduler or the UI shows about performance is one of:

    measured    observed on this machine at least MEASURED_MIN times
    calibrated  observed, but fewer times than that
    prior       not observed; derived from model size — shown as a range
    unknown     no honest figure exists; the client says so instead of guessing

A profile row with an impossible value (the vision model's 0 MB residency, a
load time written into the VRAM column) is flagged invalid and ignored: a
wrong measurement is worse than none, because it looks like one.

This module is used in exactly three places: model cards, admission wait
estimates, and the hardware probe's filesystem warning. It is never on the
critical path of a feature.
"""
from __future__ import annotations

import statistics
import time
from typing import Any

from .. import db
from ..gateway.registry import ModelCard, registry

MEASURED_MIN = 5
PRIOR_SPREAD = (0.5, 3.0)       # a prior is shown as [0.5x, 3x] of its point value

# Plausibility bounds for one profile row. Outside them the number is a bug in
# whatever recorded it, not a property of the hardware.
_BOUNDS = {"cold_load_s": (0.05, 1800.0), "decode_tps": (0.1, 5000.0),
           "vram_resident_mb": (64.0, 1_000_000.0)}

BASIS_RANK = {"measured": 0, "calibrated": 1, "prior": 2, "unknown": 3}


def weakest(*bases: str) -> str:
    return max(bases, key=lambda b: BASIS_RANK.get(b, 3)) if bases else "unknown"


def validate(profile: dict[str, Any]) -> str | None:
    """Why this profile row cannot be trusted, or None."""
    problems = []
    for key, (lo, hi) in _BOUNDS.items():
        v = profile.get(key)
        if v is None:
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            problems.append(f"{key} is not a number")
            continue
        if key == "vram_resident_mb" and v == 0.0:
            problems.append("vram_resident_mb is 0, which no resident model can be")
        elif not lo <= v <= hi:
            problems.append(f"{key}={v:g} is outside the plausible range "
                            f"[{lo:g}, {hi:g}]")
    return "; ".join(problems) or None


def _samples(profile: dict[str, Any], key: str) -> int:
    if key == "cold_load_s":
        n = profile.get("load_samples") or 0
        # Rows written before per-metric counters: a load time exists, so it
        # was observed at least once, but its count was mixed with decode.
        return int(n) if n else (1 if profile.get("cold_load_s") else 0)
    if key == "decode_tps":
        n = profile.get("decode_samples") or 0
        return int(n) if n else int(profile.get("samples") or 0) \
            if profile.get("decode_tps") else 0
    if key == "vram_resident_mb":
        return int(profile.get("load_samples") or 0) or (
            1 if profile.get("vram_resident_mb") else 0)
    return 0


def _prior(card: ModelCard, key: str) -> float:
    size = card.weights_mb or card.est_vram_mb or 4096
    if key == "cold_load_s":
        return max(2.0, size / 1100.0)       # NVMe read + init, ~1.1 GB/s effective
    if key == "decode_tps":
        return max(6.0, 60.0 * card.cap("speed"))
    if key == "vram_resident_mb":
        return float(card.est_vram_mb or card.weights_mb or 4096)
    return 0.0


def metric(card: ModelCard, key: str) -> dict[str, Any]:
    """One performance figure for a model, with its basis."""
    prof = card.profile or {}
    invalid = validate(prof)
    v = prof.get(key)
    bad_field = invalid and key in (invalid or "")
    if v and not bad_field:
        n = _samples(prof, key)
        basis = "measured" if n >= MEASURED_MIN else "calibrated"
        return {"value": round(float(v), 2), "basis": basis, "samples": n,
                "range": None, "valid": True}
    point = _prior(card, key)
    return {"value": round(point, 2), "basis": "prior", "samples": 0,
            "range": [round(point * PRIOR_SPREAD[0], 1),
                      round(point * PRIOR_SPREAD[1], 1)],
            "valid": not bad_field,
            "why": (f"profile row ignored: {invalid}" if bad_field
                    else "not yet observed on this machine; size-derived prior")}


def card_basis(card: ModelCard) -> dict[str, Any]:
    prof = card.profile or {}
    return {"cold_load_s": metric(card, "cold_load_s"),
            "decode_tps": metric(card, "decode_tps"),
            "residency_mb": metric(card, "vram_resident_mb"),
            "profile_invalid": validate(prof)}


def trusted_profile_value(card: ModelCard, key: str) -> float | None:
    """The profile value if valid, else None. What admission is allowed to use."""
    prof = card.profile or {}
    invalid = validate(prof) or ""
    v = prof.get(key)
    if v and key not in invalid:
        return float(v)
    return None


# ------------------------------------------------------------ learning

def record_load(model: str, measured_s: float, resident_mb: float) -> None:
    """Fold one observed cold load into the profile. Ignores warm no-ops."""
    if measured_s < 0.4:
        return
    row = db.query_one("SELECT cold_load_s, load_samples, vram_resident_mb "
                       "FROM model_profiles WHERE model=?", (model,))
    n = int((row["load_samples"] if row else 0) or 0)
    if row and row["cold_load_s"]:
        n_eff = max(1, n)
        blended = (float(row["cold_load_s"]) * n_eff + measured_s) / (n_eff + 1)
        update: dict[str, Any] = {"cold_load_s": round(blended, 2),
                                  "load_samples": n_eff + 1,
                                  "measured_at": time.time()}
        # Only a real observation of residency may overwrite residency. The
        # previous code wrote the *load time* here whenever the model was not
        # seen resident, which is how a 20.5 "MB" footprint entered the table.
        if resident_mb > 0:
            update["vram_resident_mb"] = round(resident_mb, 1)
        db.update("model_profiles", "model", model, update)
    else:
        db.upsert("model_profiles", {
            "model": model, "cold_load_s": round(measured_s, 2),
            "vram_resident_mb": round(resident_mb, 1) if resident_mb > 0 else None,
            "measured_at": time.time(), "load_samples": 1}, key="model")
    _revalidate(model)


def record_decode(model: str, tps: float) -> None:
    if tps <= 0:
        return
    row = db.query_one("SELECT decode_tps, decode_samples, samples FROM "
                       "model_profiles WHERE model=?", (model,))
    if row and row["decode_tps"]:
        n = int(row["decode_samples"] or row["samples"] or 1)
        db.update("model_profiles", "model", model, {
            "decode_tps": round((float(row["decode_tps"]) * n + tps) / (n + 1), 2),
            "decode_samples": n + 1, "samples": n + 1})
    else:
        db.upsert("model_profiles", {"model": model, "decode_tps": round(tps, 2),
                                     "decode_samples": 1, "samples": 1,
                                     "measured_at": time.time()}, key="model")
    _revalidate(model)


def _revalidate(model: str) -> None:
    row = db.query_one("SELECT * FROM model_profiles WHERE model=?", (model,))
    if row:
        db.update("model_profiles", "model", model,
                  {"invalid_reason": validate(dict(row))})
    registry.invalidate()


def revalidate_all() -> list[dict[str, Any]]:
    """Flag every stored profile row; called at startup."""
    out = []
    for row in db.query("SELECT * FROM model_profiles"):
        why = validate(dict(row))
        db.update("model_profiles", "model", row["model"], {"invalid_reason": why})
        if why:
            out.append({"model": row["model"], "invalid": why})
    registry.invalidate()
    return out


# ------------------------------------------------------------ wait estimates

def runtime_basis(model: str | None, task_type: str | None) -> dict[str, Any]:
    """Expected runtime of one task of this type on this model, from history."""
    if not model:
        return {"basis": "unknown", "why": "no model has been selected yet"}
    rows = db.query(
        "SELECT runtime_s FROM tasks WHERE state='COMPLETED' AND runtime_s > 0 "
        "AND selected_model=? AND task_type IS ? ORDER BY finished_at DESC LIMIT 50",
        (model, task_type))
    xs = [float(r["runtime_s"]) for r in rows]
    if not xs:
        return {"basis": "unknown", "samples": 0,
                "why": f"first run of {model} for {task_type or 'this task type'}"}
    med = statistics.median(xs)
    return {"seconds": round(med, 1), "samples": len(xs),
            "basis": "measured" if len(xs) >= MEASURED_MIN else "calibrated",
            "p90": round(sorted(xs)[int(0.9 * (len(xs) - 1))], 1)}


def wait_estimate(task_id: str, model: str | None = None) -> dict[str, Any]:
    """How long until this queued task starts, with the basis of the figure.

    The estimate is the work ahead of it — the remainder of running tasks plus
    the queued tasks that outrank it — divided across the agent slots, plus
    the cost of making its model resident. If any term is unknown the whole
    estimate is unknown: a number built on a guess is presented as a guess or
    not at all.
    """
    from .residency import residency

    task = db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not task:
        return {"basis": "unknown", "why": "no such task"}
    model = model or task["selected_model"]
    limit = max(1, residency.concurrency_limit()[0])
    bases: list[str] = []
    samples: list[int] = []
    total = 0.0
    unknown: list[str] = []

    now = time.time()
    for r in db.query("SELECT id, selected_model, task_type, started_at FROM tasks "
                      "WHERE state IN ('ADMITTED','RUNNING') AND id != ?", (task_id,)):
        rb = runtime_basis(r["selected_model"], r["task_type"])
        if rb["basis"] == "unknown":
            unknown.append(rb["why"])
            continue
        elapsed = now - (r["started_at"] or now)
        total += max(0.0, rb["seconds"] - elapsed)
        bases.append(rb["basis"])
        samples.append(rb["samples"])

    eff = task["effective_priority"] or 0.0
    for r in db.query("SELECT id, selected_model, task_type FROM tasks WHERE "
                      "state IN ('QUEUED','PAUSED') AND id != ? AND "
                      "(effective_priority > ? OR (effective_priority = ? AND "
                      "created_at < ?))",
                      (task_id, eff, eff, task["created_at"])):
        rb = runtime_basis(r["selected_model"] or model, r["task_type"])
        if rb["basis"] == "unknown":
            unknown.append(rb["why"])
            continue
        total += rb["seconds"]
        bases.append(rb["basis"])
        samples.append(rb["samples"])

    total /= limit
    card = registry.get(model) if model else None
    if card and card.name not in residency.resident_names():
        m = metric(card, "cold_load_s")
        total += m["value"]
        bases.append(m["basis"])
        samples.append(m["samples"])

    if unknown:
        return {"basis": "unknown", "why": unknown[0],
                "text": f"wait unknown — {unknown[0]}"}
    if not bases:
        return {"seconds": 0.0, "basis": "measured", "samples": 0,
                "text": "nothing ahead of it in the queue"}
    basis = weakest(*bases)
    n = min(samples) if samples else 0
    if basis == "prior":
        lo, hi = total * PRIOR_SPREAD[0], total * PRIOR_SPREAD[1]
        return {"seconds": round(total, 1), "range": [round(lo), round(hi)],
                "basis": basis, "samples": n,
                "text": f"estimated wait {lo:.0f}–{hi:.0f} s (prior, not yet "
                        f"measured on this machine)"}
    return {"seconds": round(total, 1), "basis": basis, "samples": n,
            "text": f"estimated wait {total:.0f} s ({basis}, {n} sample"
                    f"{'s' if n != 1 else ''})"}
