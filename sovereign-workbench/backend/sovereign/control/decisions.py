"""The uniform decision record.

Eight authorities decide things in this system — admission, residency, routing,
registry, tool policy, evidence, sovereignty. Each used to write *something* in
its own shape: a task_events row here, an audit detail blob there. A reviewer
asking "show me every decision this system made about my task" had to know
seven tables and read the code to join them.

Every authority now also writes one row here, in one shape:

    (authority, subject, outcome, reason, basis, principal, ts, prev_hash)

The rows are hash-chained on their own, and every row is announced in the audit
log with its hash. That cross-reference is what makes removal detectable at
both ends: delete a decision and its chain breaks and the audit log names a
hash that no longer exists; truncate the tail of decisions and the audit log
still holds the hashes of the rows that were cut; tamper with the audit log and
its own anchor fails (D-13).
"""
from __future__ import annotations

import hashlib
import threading
from typing import Any

from .. import audit, db

AUTHORITIES = ("admission", "residency", "routing", "registry", "tool_policy",
               "evidence", "sovereignty", "trust")

GENESIS = "0" * 64
_lock = threading.Lock()


def _digest(prev: str, ts: float, authority: str, subject_kind: str,
            subject_id: str, task_id: str, session_id: str, outcome: str,
            reason: str, basis: str, principal: str) -> str:
    payload = "\x1f".join([prev, f"{ts:.6f}", authority, subject_kind, subject_id,
                           task_id, session_id, outcome, reason, basis, principal])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def record(authority: str, outcome: str, reason: str = "", *,
           subject_kind: str = "", subject_id: str = "",
           task_id: str | None = None, session_id: str | None = None,
           basis: Any = None, principal: str | None = None) -> str:
    """Append one decision. Returns its id.

    `principal` is who the decision was made *for or by*: the task owner for an
    admission, the approver for an approval, the admin for a registry edit. The
    system itself is the principal `system`, never an anonymous string.
    """
    if authority not in AUTHORITIES:
        raise ValueError(f"unknown authority {authority!r}; "
                         f"the control plane has {AUTHORITIES}")
    if session_id is None and task_id:
        row = db.query_one("SELECT conversation_id FROM tasks WHERE id=?", (task_id,))
        session_id = row["conversation_id"] if row else None
    basis_s = db.jdump(basis) if basis is not None else ""
    reason = (reason or "")[:2000]
    principal = principal or "system"
    did = db.new_id("dec")
    ts = db.now()
    with _lock:
        last = db.query_one("SELECT hash FROM decisions ORDER BY seq DESC LIMIT 1")
        prev = last["hash"] if last else GENESIS
        h = _digest(prev, ts, authority, subject_kind or "", subject_id or "",
                    task_id or "", session_id or "", outcome, reason, basis_s,
                    principal)
        db.insert("decisions", {
            "id": did, "ts": ts, "authority": authority,
            "subject_kind": subject_kind or "", "subject_id": subject_id or "",
            "task_id": task_id, "session_id": session_id, "outcome": outcome,
            "reason": reason, "basis": basis_s, "principal": principal,
            "prev_hash": prev, "hash": h})
    audit.record("decision", authority, outcome=outcome, actor=principal,
                 task_id=task_id,
                 detail={"decision_id": did, "hash": h,
                         "subject": f"{subject_kind}:{subject_id}".strip(":")})
    audit.bus.publish({"type": "decision", "id": did, "authority": authority,
                       "outcome": outcome, "task_id": task_id,
                       "session_id": session_id, "reason": reason[:300]})
    return did


def for_task(task_id: str) -> list[dict[str, Any]]:
    return _rows("SELECT * FROM decisions WHERE task_id=? ORDER BY seq", (task_id,))


def for_session(session_id: str) -> list[dict[str, Any]]:
    return _rows("SELECT * FROM decisions WHERE session_id=? ORDER BY seq",
                 (session_id,))


def recent(limit: int = 200, authority: str | None = None) -> list[dict[str, Any]]:
    if authority:
        return _rows("SELECT * FROM decisions WHERE authority=? "
                     "ORDER BY seq DESC LIMIT ?", (authority, limit))
    return _rows("SELECT * FROM decisions ORDER BY seq DESC LIMIT ?", (limit,))


def _rows(sql: str, params: tuple) -> list[dict[str, Any]]:
    out = db.rows_to_dicts(db.query(sql, params))
    for r in out:
        r["basis"] = db.jload(r["basis"], r["basis"] or None)
    return out


def verify() -> dict[str, Any]:
    """Recompute the decision chain and cross-check it against the audit log."""
    rows = db.query("SELECT * FROM decisions ORDER BY seq ASC")
    prev = GENESIS
    hashes: list[str] = []
    for r in rows:
        if r["prev_hash"] != prev:
            return {"ok": False, "broken_at": r["seq"], "entries": len(rows),
                    "reason": "decision prev_hash mismatch: a decision before this "
                              "one was removed or edited"}
        h = _digest(prev, r["ts"], r["authority"], r["subject_kind"] or "",
                    r["subject_id"] or "", r["task_id"] or "",
                    r["session_id"] or "", r["outcome"], r["reason"] or "",
                    r["basis"] or "", r["principal"] or "")
        if h != r["hash"]:
            return {"ok": False, "broken_at": r["seq"], "entries": len(rows),
                    "reason": "decision hash mismatch: this row was edited"}
        hashes.append(h)
        prev = h

    announced = []
    for a in db.query("SELECT detail FROM audit_log WHERE category='decision' "
                      "ORDER BY seq ASC"):
        d = db.jload(a["detail"], {}) or {}
        if d.get("hash"):
            announced.append(d["hash"])
    if announced != hashes:
        present = set(hashes)
        missing = [h for h in announced if h not in present]
        return {"ok": False, "entries": len(rows), "announced": len(announced),
                "reason": (f"{len(missing)} decision(s) announced in the audit log "
                           f"are missing from the decision record"
                           if missing else
                           "the decision record holds rows the audit log never "
                           "announced")}
    return {"ok": True, "entries": len(rows), "head": prev}
