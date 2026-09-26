"""The control plane, made explicit (v2 §3.1).

The control plane is the set of components that *decide* rather than *do*. It
never touches a model, a document or a socket itself. It answers eight
questions, and every answer is a persisted, attributable decision row:

    admission    may this task run now, in which slot, at what context budget
    residency    which model is resident, what must be evicted, at what cost
    routing      which model serves this task, and why
    registry     what models exist and whether the backend actually serves them
    tool_policy  may this tool call happen; must a human approve it
    evidence     is this claim established, derived, interpreted or unsupported
    sovereignty  did anything try to leave, and can the log be trusted
    trust        which devices belong to the domain, at what grade, under
                 which lease, and what each grade may receive

This package is the one boundary the API layer and the clients talk to. They
never import `runtime`, `router`, `policy` or `evidence` directly; a test holds
that (tests/test_control_plane.py).

Attributes are resolved lazily. The data plane (the tool gateway, the policy
engine) records decisions through this package, and eager imports here would
close an import cycle through the scheduler.
"""
from __future__ import annotations

import importlib
from typing import Any

_LAZY = {
    # authorities
    "admission": ("sovereign.runtime.scheduler", "scheduler"),
    "residency": ("sovereign.runtime.residency", "residency"),
    "routing": ("sovereign.router", None),
    "registry": ("sovereign.gateway.registry", "registry"),
    "tool_policy": ("sovereign.policy.tool_policy", None),
    "evidence": ("sovereign.evidence.provenance", None),
    "sovereignty": ("sovereign.policy.egress", None),
    "injection": ("sovereign.policy.injection", None),
    "perfmodel": ("sovereign.runtime.perfmodel", None),
    # plane services
    "identity": ("sovereign.control.identity", None),
    "sessions": ("sovereign.control.sessions", None),
    "transcript": ("sovereign.control.transcript", None),
    "decisions": ("sovereign.control.decisions", None),
    "report": ("sovereign.control.report", None),
    "admin": ("sovereign.control.admin", None),
    # the trust domain (sovereign-workbench-v2.md)
    "trust": ("sovereign.control.trust", None),
    "devices": ("sovereign.control.devices", None),
    "leases": ("sovereign.control.leases", None),
    "anchors": ("sovereign.control.anchors", None),
    "attestation": ("sovereign.control.attestation", None),
    "placement": ("sovereign.placement", None),
    "profiler": ("sovereign.profiler", None),
    "bundles": ("sovereign.bundles", None),
    "gate": ("sovereign.evidence.gate", None),
    "served": ("sovereign.evidence.served", None),
}

AUTHORITIES = ("admission", "residency", "routing", "registry", "tool_policy",
               "evidence", "sovereignty", "trust")


def __getattr__(name: str) -> Any:
    if name not in _LAZY:
        raise AttributeError(f"the control plane has no {name!r}")
    module, attr = _LAZY[name]
    mod = importlib.import_module(module)
    value = getattr(mod, attr) if attr else mod
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(list(_LAZY) + ["AUTHORITIES"])
