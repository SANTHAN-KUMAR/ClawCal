"""The transcript: one ordered trajectory per session.

Plan, tool calls, observations, refusals, approvals, decisions, evidence
classes and the final answer, merged from the rows the control plane already
persists. It is the single source the web client, the terminal client and the
audit export render from, so the same task looks the same in all three (B3).

Nothing here is recomputed from model output. Every entry points back at the row
it came from.
"""
from __future__ import annotations

import time
from typing import Any

from .. import db, outcomes
from . import decisions

# task_events kinds that are noise in a transcript (the harness emits them for
# the live trace, and the transcript already carries the same fact elsewhere).
_SKIP_EVENTS = {"submitted"}

# Decision authorities worth showing inline. tool_policy ALLOW decisions for
# read-only tools would double every tool call, so only the ones a human cares
# about are kept.
_SHOW_POLICY = {"ASK", "DENY", "APPROVED", "DENIED", "MODE_SET"}


def _entry(kind: str, ts: float, task_id: str | None, label: str = "",
           detail: str = "", outcome: str | None = None,
           data: Any = None, ref: str = "") -> dict[str, Any]:
    e: dict[str, Any] = {"kind": kind, "ts": ts, "task_id": task_id,
                         "label": label, "detail": detail}
    if outcome:
        e["outcome"] = outcome
    if data is not None:
        e["data"] = data
    if ref:
        e["ref"] = ref
    return e


def for_task(task: dict[str, Any]) -> list[dict[str, Any]]:
    tid = task["id"]
    out: list[dict[str, Any]] = [_entry(
        "user", task["created_at"], tid, "Request", task["prompt"],
        data={"attachments": db.jload(task.get("attachments"), []) or [],
              "owner": task.get("owner"), "priority": task.get("priority"),
              "task_type": task.get("task_type"),
              "permission_mode": task.get("policy_mode")},
        ref=f"tasks:{tid}")]

    calls = {r["id"]: dict(r) for r in db.query(
        "SELECT id, tool, outcome, outcome_reason, decision, principal "
        "FROM tool_calls WHERE task_id=?", (tid,))}

    for ev in db.query("SELECT seq, kind, label, detail, payload, ts FROM "
                       "task_events WHERE task_id=? ORDER BY seq", (tid,)):
        if ev["kind"] in _SKIP_EVENTS:
            continue
        payload = db.jload(ev["payload"], None)
        outcome = None
        if isinstance(payload, dict):
            outcome = payload.get("outcome")
            cid = payload.get("call_id")
            if cid and cid in calls and not outcome:
                outcome = calls[cid]["outcome"]
        if ev["kind"] == "completed":
            continue                     # replaced by the answer entry below
        out.append(_entry(ev["kind"], ev["ts"], tid, ev["label"] or "",
                          ev["detail"] or "", outcome,
                          data=_slim(ev["kind"], payload),
                          ref=f"task_events:{tid}:{ev['seq']}"))

    for d in decisions.for_task(tid):
        if d["authority"] == "tool_policy" and d["outcome"] not in _SHOW_POLICY:
            continue
        out.append(_entry("decision", d["ts"], tid,
                          f"{d['authority']}: {d['outcome']}", d["reason"] or "",
                          data={"authority": d["authority"],
                                "outcome": d["outcome"],
                                "principal": d["principal"],
                                "basis": d["basis"], "hash": d["hash"]},
                          ref=f"decisions:{d['id']}"))

    result = db.jload(task.get("result"), {}) or {}
    if task["state"] in ("COMPLETED",) and result:
        o = result.get("outcome") or {}
        out.append(_entry(
            "answer", task.get("finished_at") or time.time(), tid, "Answer",
            result.get("summary", ""),
            o.get("headline"),
            data={"annotated": result.get("annotated"),
                  "outcome": o,
                  "provenance": {"counts": (result.get("provenance") or {}).get(
                      "counts")},
                  "artifacts": result.get("artifacts") or [],
                  "calculations": len(result.get("calculations") or [])},
            ref=f"tasks:{tid}:result"))
    elif task["state"] in ("FAILED", "REJECTED", "TERMINATED"):
        out.append(_entry("stopped", task.get("finished_at") or task["created_at"],
                          tid, task["state"].title(), task.get("state_reason") or "",
                          outcomes.CANNOT_DETERMINE, ref=f"tasks:{tid}"))
    elif task["state"] in ("QUEUED", "PAUSED", "ADMITTED", "RUNNING", "ATTACHED"):
        out.append(_entry("status", time.time(), tid, task["state"].title(),
                          task.get("state_reason") or "", ref=f"tasks:{tid}"))

    out.sort(key=lambda e: (e["ts"], _ORDER.get(e["kind"], 5)))
    return out


# Ties in timestamp (same millisecond) resolve in narrative order.
_ORDER = {"user": 0, "decision": 1, "admitted": 2, "plan": 3, "answer": 9,
          "stopped": 9, "status": 9}


def _slim(kind: str, payload: Any) -> Any:
    """Keep what a reader needs; drop bulky internals like whole drawings."""
    if not isinstance(payload, dict):
        return payload
    if kind in ("drawing", "retrieval", "extraction", "deliverable", "sandbox"):
        keep = {k: payload[k] for k in ("tool", "outcome", "outcome_reason",
                                        "call_id", "fields", "survey", "name",
                                        "sha256", "exit_code", "engine", "network",
                                        "status", "found", "path")
                if k in payload}
        if kind == "retrieval":
            keep["passages"] = [
                {k: p.get(k) for k in ("doc_title", "doc_id", "page_no", "region")}
                for p in (payload.get("passages") or [])[:12]]
        return keep
    return payload


def for_session(session_id: str) -> dict[str, Any]:
    tasks = db.rows_to_dicts(db.query(
        "SELECT * FROM tasks WHERE conversation_id=? ORDER BY created_at",
        (session_id,)))
    entries: list[dict[str, Any]] = []
    # Session-level decisions (mode changes) that belong to no task.
    for d in decisions.for_session(session_id):
        if d["task_id"]:
            continue
        entries.append(_entry("decision", d["ts"], None,
                              f"{d['authority']}: {d['outcome']}", d["reason"] or "",
                              data={"authority": d["authority"],
                                    "outcome": d["outcome"],
                                    "principal": d["principal"],
                                    "basis": d["basis"], "hash": d["hash"]},
                              ref=f"decisions:{d['id']}"))
    for t in tasks:
        entries.extend(for_task(t))
    entries.sort(key=lambda e: (e["ts"], _ORDER.get(e["kind"], 5)))
    for i, e in enumerate(entries, 1):
        e["seq"] = i
    return {"session_id": session_id, "tasks": [t["id"] for t in tasks],
            "entries": entries}


# ------------------------------------------------------------ plain text render

_BADGE = {outcomes.ESTABLISHED: "[ESTABLISHED]", outcomes.INTERPRETED: "[INTERPRETED]",
          outcomes.CANNOT_DETERMINE: "[CANNOT DETERMINE]",
          outcomes.DEGRADED: "[DEGRADED]"}


def render_text(transcript: dict[str, Any], *, verbose: bool = False,
                width: int = 100) -> str:
    """The transcript as plain text. The terminal client prints exactly this."""
    lines: list[str] = []
    for e in transcript["entries"]:
        lines.extend(render_entry(e, verbose=verbose, width=width))
    return "\n".join(lines)


def render_entry(e: dict[str, Any], *, verbose: bool = False,
                 width: int = 100) -> list[str]:
    kind = e["kind"]
    badge = _BADGE.get(e.get("outcome") or "", "")
    detail = (e.get("detail") or "").strip()
    if kind == "user":
        att = (e.get("data") or {}).get("attachments") or []
        head = f"> {detail}"
        tail = [f"  attached: {', '.join(a.get('title') or a.get('doc_id', '?') for a in att)}"] \
            if att else []
        return ["", head] + tail
    if kind == "answer":
        data = e.get("data") or {}
        o = data.get("outcome") or {}
        out = ["", f"== Answer {badge}".rstrip()]
        for d in o.get("degraded") or []:
            out.append(f"   DEGRADED: {d}")
        out.append(detail)
        counts = (data.get("provenance") or {}).get("counts") or {}
        if counts:
            out.append(f"   evidence: {counts.get('A', 0)} source, "
                       f"{counts.get('B', 0)} derived, {counts.get('C', 0)} "
                       f"interpretation, {counts.get('D', 0)} unsupported")
        for a in data.get("artifacts") or []:
            out.append(f"   artefact: {a.get('name')} ({a.get('id')})")
        return out
    if kind == "reasoning" and not verbose:
        return []
    if kind == "decision":
        d = e.get("data") or {}
        if d.get("authority") in ("admission", "routing") or verbose \
                or d.get("outcome") in ("ASK", "DENY", "DENIED", "APPROVED",
                                        "MODE_SET", "REJECT", "REFUSED"):
            return [f"  · {e['label']} — {_clip(detail, width)}"
                    + (f" (by {d.get('principal')})"
                       if d.get("principal") not in (None, "system") else "")]
        return []
    if kind == "tool_call":
        return [f"  → {e['label']} {_clip(detail, width - 20) if verbose else ''}".rstrip()]
    if kind == "observation":
        return [f"  ← {e['label']} {badge}".rstrip()
                + (f"\n      {_clip(detail, width)}" if verbose and detail else "")]
    if kind in ("approval_pending",):
        aid = ((e.get("data") or {}).get("approval_id")) or ""
        return [f"  ? APPROVAL NEEDED {aid}: {_clip(detail, width)}"]
    if kind in ("stopped",):
        return ["", f"== {e['label']} {badge}: {detail}"]
    if kind == "status":
        return [f"  … {e['label']}: {_clip(detail, width)}"]
    if kind in ("plan", "admitted", "residency") and not verbose:
        return [f"  · {e['label']}: {_clip(detail, width)}"]
    return [f"  · {e['label']}" + (f": {_clip(detail, width)}" if detail else "")
            + (f" {badge}" if badge else "")]


def _clip(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: max(0, n - 1)] + "…"
