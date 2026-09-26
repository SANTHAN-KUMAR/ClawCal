"""The administration surface (v2 §3.1, item 4).

Registry rows, the default permission mode and runtime quotas used to be edited
by SQL, environment variables or a shell script. They are edited here, behind
the `admin` role, and every change is itself a decision row naming who made it
and what it changed from. Overrides persist in `control_settings`, so they
survive a restart; environment variables remain the boot-time defaults.

Egress is deliberately *not* editable from here: applying or removing the host
firewall needs root, and a web route that could lower it would be the most
valuable thing on the appliance to steal. The route reports its state and the
command an administrator runs.
"""
from __future__ import annotations

import dataclasses
import time
from typing import Any

from .. import db
from ..config import settings
from . import decisions
from .identity import Principal

# Quotas an admin may change at runtime, and their sane bounds.
EDITABLE_LIMITS: dict[str, tuple[float, float]] = {
    "max_concurrent_agents": (1, 64),
    "max_concurrent_per_user": (1, 64),
    "max_queue_depth": (1, 100_000),
    "task_time_budget_s": (60, 86_400),
    "max_agent_steps": (2, 200),
    "min_residency_dwell_s": (0, 3600),
    "priority_aging_s": (5, 86_400),
}

EDITABLE_CARD_FIELDS = {"caps", "ctx_max", "role", "notes", "enabled",
                        "prompt_adapter", "kv_mb_per_1k", "est_vram_mb"}


def _set_limit_attr(name: str, value: Any) -> None:
    # Settings are frozen dataclasses so nothing mutates them by accident; the
    # admin surface is the one deliberate exception, and it replaces the whole
    # limits object rather than editing it in place.
    new = dataclasses.replace(settings.limits, **{name: value})
    object.__setattr__(settings, "limits", new)


def load_overrides() -> dict[str, Any]:
    """Apply persisted overrides at startup."""
    applied: dict[str, Any] = {}
    for row in db.query("SELECT key, value FROM control_settings"):
        key, raw = row["key"], row["value"]
        if key.startswith("limits."):
            name = key.split(".", 1)[1]
            if name in EDITABLE_LIMITS:
                cur = getattr(settings.limits, name)
                _set_limit_attr(name, type(cur)(float(raw)))
                applied[key] = raw
        elif key == "policy.default_mode":
            from ..policy.tool_policy import policy
            policy.mode = raw
            applied[key] = raw
    return applied


def _persist(key: str, value: Any, by: str) -> None:
    db.upsert("control_settings", {"key": key, "value": str(value),
                                   "changed_by": by, "changed_at": time.time()},
              key="key")


def limits() -> dict[str, Any]:
    return {k: getattr(settings.limits, k) for k in EDITABLE_LIMITS}


def set_limits(changes: dict[str, Any], admin: Principal) -> dict[str, Any]:
    admin.require("admin", "changing runtime quotas")
    for name, value in changes.items():
        if name not in EDITABLE_LIMITS:
            raise ValueError(f"{name!r} is not an editable limit; editable: "
                             f"{sorted(EDITABLE_LIMITS)}")
        lo, hi = EDITABLE_LIMITS[name]
        cur = getattr(settings.limits, name)
        try:
            v = type(cur)(float(value))
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a number")
        if not lo <= v <= hi:
            raise ValueError(f"{name}={v} is outside [{lo:g}, {hi:g}]")
        _set_limit_attr(name, v)
        _persist(f"limits.{name}", v, admin.name)
        decisions.record("admission", "LIMIT_SET", f"{name}: {cur} -> {v}",
                         subject_kind="limit", subject_id=name,
                         principal=admin.name,
                         basis={"from": cur, "to": v, "via": "admin"})
    from ..runtime.scheduler import scheduler
    scheduler.wake()
    return limits()


def set_default_mode(mode: str, admin: Principal) -> str:
    from ..policy.tool_policy import normalise_mode, policy
    admin.require("admin", "changing the default permission mode")
    mode = normalise_mode(mode)
    old = policy.mode
    policy.set_mode(mode, by=admin.name)
    _persist("policy.default_mode", mode, admin.name)
    decisions.record("tool_policy", "DEFAULT_MODE_SET",
                     f"default mode for new sessions: {old} -> {mode}",
                     subject_kind="policy", subject_id="default_mode",
                     principal=admin.name, basis={"from": old, "to": mode,
                                                  "via": "admin"})
    return mode


def edit_model(name: str, changes: dict[str, Any], admin: Principal) -> dict[str, Any]:
    """Edit a registry row. Adding a model stays a row, not a code change."""
    from ..gateway.registry import registry
    admin.require("admin", "editing the model registry")
    row = db.query_one("SELECT * FROM model_registry WHERE name=?", (name,))
    if not row:
        raise ValueError(f"no model {name!r} in the registry")
    bad = set(changes) - EDITABLE_CARD_FIELDS
    if bad:
        raise ValueError(f"not editable: {sorted(bad)}; editable fields are "
                         f"{sorted(EDITABLE_CARD_FIELDS)}")
    update: dict[str, Any] = {}
    for k, v in changes.items():
        if k == "caps":
            if not isinstance(v, dict) or not all(
                    isinstance(x, (int, float)) and 0 <= x <= 1 for x in v.values()):
                raise ValueError("caps must map capability axes to numbers in [0, 1]")
            merged = {**(db.jload(row["caps"], {}) or {}), **v}
            update["caps"] = db.jdump(merged)
        elif k == "enabled":
            update["enabled"] = int(bool(v))
        else:
            update[k] = v
    update["edited_by"] = admin.name
    before = {k: row[k] for k in update if k in row.keys()}
    db.update("model_registry", "name", name, update)
    registry.invalidate()
    decisions.record("registry", "EDITED", f"{name}: {sorted(changes)} changed",
                     subject_kind="model", subject_id=name, principal=admin.name,
                     basis={"before": before, "after": changes, "via": "admin"})
    card = registry.get(name)
    return card.to_dict() if card else {}


def add_model(spec: dict[str, Any], admin: Principal) -> dict[str, Any]:
    """Register a new model. It is enabled only if the backend actually serves it."""
    from ..gateway import gateway
    from ..gateway.registry import CAP_AXES, ModelCard, registry
    admin.require("admin", "adding a model")
    required = ("name", "backend", "backend_ref")
    missing = [k for k in required if not spec.get(k)]
    if missing:
        raise ValueError(f"missing {missing}")
    if db.query_one("SELECT name FROM model_registry WHERE name=?", (spec["name"],)):
        raise ValueError(f"model {spec['name']!r} already exists; edit it instead")
    caps = spec.get("caps") or {}
    unknown = set(caps) - set(CAP_AXES)
    if unknown:
        raise ValueError(f"unknown capability axes {sorted(unknown)}; axes are "
                         f"{CAP_AXES}")
    card = ModelCard(
        name=str(spec["name"]), backend=str(spec["backend"]),
        backend_ref=str(spec["backend_ref"]), family=str(spec.get("family", "")),
        role=str(spec.get("role", "generalist")),
        prompt_adapter=str(spec.get("prompt_adapter", "chat")),
        modality=str(spec.get("modality", "text")), caps=caps,
        ctx_max=int(spec.get("ctx_max", 8192)),
        weights_mb=int(spec.get("weights_mb", 0)),
        est_vram_mb=int(spec.get("est_vram_mb", 0)),
        kv_mb_per_1k=float(spec.get("kv_mb_per_1k", 0.0)), enabled=False,
        notes=str(spec.get("notes", "")))
    row = card.to_row()
    row["edited_by"] = admin.name
    db.insert("model_registry", row)
    registry.invalidate()
    decisions.record("registry", "ADDED", f"{card.name} registered on {card.backend}",
                     subject_kind="model", subject_id=card.name,
                     principal=admin.name, basis={"spec": spec, "via": "admin"})
    sync = gateway.sync_registry()
    return {"model": card.name, "enabled": card.name in sync["available"],
            "sync": sync}
