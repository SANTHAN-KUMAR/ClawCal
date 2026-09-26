"""Admission when the node is out of reach (sovereign-workbench-v2.md §8, §14).

Attached, the node classifies, places and pins (`/api/admit`). Detached, the
device is — by definition — away from the node, so the same decision is made
here, from the signed manifest the node issued before the device left:

* the task class comes from a small, deterministic rule set (the same kind of
  rules as the node's router: file types and verbs, never a model call);
* **extract** and **vision** are refused with the reason — they need the node's
  OCR, corpus and gate;
* **draft**, **code** and **calc** run on the local engine only if the manifest's
  `task_classes_allowed` includes them (the measured floor, RQ1/T12);
* the model is the one the manifest pins.

Every decision is appended to the chained log, so the node sees on rejoin what
this device decided while it was away.
"""
from __future__ import annotations

import re
import uuid
from typing import Any

from . import bundle, trust

# Checked in order; the first match decides. Deliberately conservative: when a
# request could need the node's perception pipeline, it is sent there.
RULES: list[tuple[str, str]] = [
    (r"\b(p&id|pid|drawing|schematic|isometric|photo(graph)?|image|picture|"
     r"handwrit\w*|scan(ned)?|ocr|nameplate)\b", "vision"),
    (r"\b(extract|pull out|tabulate|itemi[sz]e|list the findings|inspection "
     r"report)\b", "extract"),
    (r"\b(python|javascript|typescript|bash|script|function|unit test|pytest|"
     r"refactor|debug|compile|repository|codebase|code)\b", "code"),
    (r"\b(calculate|compute|margin|derate|corrosion rate|remaining life|"
     r"thickness|stress|flow rate|percentage|ratio)\b", "calc"),
]


def classify(prompt: str) -> tuple[str, str]:
    text = (prompt or "").lower()
    for pattern, cls in RULES:
        m = re.search(pattern, text)
        if m:
            return cls, f"matched {m.group(0)!r}"
    return "draft", "no specialised signal; drafting"


def admit(prompt: str) -> dict[str, Any]:
    lease = trust.enforce_lease()
    man = bundle.load_manifest()
    spec, signal = classify(prompt)
    allowed = list((man.get("policy") or {}).get("task_classes_allowed") or [])
    base = {"session_id": f"local-{uuid.uuid4().hex[:12]}",
            "task_id": f"local-{uuid.uuid4().hex[:12]}", "task_type": spec,
            "spec_class": spec, "model": man["model"]["id"],
            "model_reason": (f"the signed manifest pins {man['model']['id']} for "
                             f"{man['device_class']}"),
            "grade": lease.get("grade"), "clearance": lease.get("clearance"),
            "node_tools": [], "local_tools": ["read", "edit", "glob", "grep"],
            "execute_local": bool(lease.get("execute_local")),
            "agent": f"clawcal-{spec}", "signals": [signal], "decided": "on-device"}
    from . import slices
    held = slices.local()
    if spec == "extract" and held:
        # An exported slice carries documents the node already read — spans and
        # extracted values — so extraction over *those* documents can run here,
        # held to the node's own gate rules. New scanned material still cannot.
        reason = (f"extract runs on this device against the exported slice "
                  f"({', '.join(d['title'] for s in held for d in s['docs'])}); the "
                  f"slice's spans are the served set and the gate is the node's own")
        out = dict(base, admitted=True, placement="client", reason=reason,
                   why=f"{spec} ({signal}); {reason}. {base['model_reason']}",
                   node_tools=[], local_tools=base["local_tools"]
                   + ["slice_documents", "retrieve", "deliver"])
        trust.log("admitted_detached", {k: out[k] for k in (
            "task_id", "spec_class", "placement", "model", "admitted")})
        return out
    if spec in ("extract", "vision"):
        reason = (f"{spec} needs the node's ingestion, OCR and vision models, corpus "
                  f"and provenance gate; this device is detached. Re-attach to the "
                  f"node for it")
        out = dict(base, admitted=False, placement="refused", reason=reason, why=reason)
    elif spec not in allowed:
        reason = (f"this device's manifest allows {', '.join(allowed) or 'no'} task "
                  f"classes detached, not {spec}: the local model is below the "
                  f"measured floor for it")
        out = dict(base, admitted=False, placement="refused", reason=reason, why=reason)
    else:
        reason = (f"{spec} runs on this device's own engine because it is detached; "
                  f"no node tools and no team corpus")
        out = dict(base, admitted=True, placement="client", reason=reason,
                   why=f"{spec} ({signal}); {reason}. {base['model_reason']}")
        if held:
            out["local_tools"] = base["local_tools"] + ["slice_documents", "retrieve",
                                                        "deliver"]
    trust.log("admitted_detached", {k: out[k] for k in (
        "task_id", "spec_class", "placement", "model", "admitted")})
    return out
