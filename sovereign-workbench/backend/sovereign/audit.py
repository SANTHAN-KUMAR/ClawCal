"""Tamper-evident audit log and the in-process event bus.

The audit log is append-only and hash-chained: each row commits to the previous
row's hash, so any deletion or edit of history breaks verification. This is the
"non-empty denial log" the sovereignty claim rests on -- an empty packet capture
proves nothing, a verifiable chain of refusals proves a policy was live.
"""
from __future__ import annotations

import hashlib
import queue
import threading
from typing import Any

from . import db

GENESIS = "0" * 64
_audit_lock = threading.Lock()


def _digest(prev: str, ts: float, actor: str, task_id: str, category: str,
            action: str, outcome: str, detail: str) -> str:
    payload = "\x1f".join([prev, f"{ts:.6f}", actor or "", task_id or "",
                           category, action, outcome or "", detail or ""])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def record(category: str, action: str, *, outcome: str = "OK", actor: str = "system",
           task_id: str | None = None, detail: Any = None) -> str:
    """Append one audit row. Returns its hash."""
    detail_s = db.jdump(detail) if not isinstance(detail, (str, type(None))) else (detail or "")
    ts = db.now()
    with _audit_lock, db.tx() as c:
        last = c.execute(
            "SELECT hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
        prev = last["hash"] if last else GENESIS
        h = _digest(prev, ts, actor, task_id or "", category, action, outcome, detail_s)
        cur = c.execute(
            "INSERT INTO audit_log (ts, actor, task_id, category, action, outcome, "
            "detail, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?,?)",
            (ts, actor, task_id, category, action, outcome, detail_s, prev, h))
        # The anchor advances by one per row. Recounting the whole log on every
        # write, as this did, costs O(n) per event and is the first thing to
        # degrade on a busy appliance with a year of history. The incremental
        # count is still checked against a full recount by verify_chain().
        anchor = c.execute("SELECT entries FROM audit_anchor WHERE id=1").fetchone()
        entries = (anchor["entries"] + 1) if anchor else c.execute(
            "SELECT COUNT(*) AS n FROM audit_log").fetchone()["n"]
        c.execute(
            "INSERT INTO audit_anchor (id, head, entries, max_seq, ts) "
            "VALUES (1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET head=excluded.head, "
            "entries=excluded.entries, max_seq=excluded.max_seq, ts=excluded.ts",
            (h, entries, cur.lastrowid, ts))
    bus.publish({"type": "audit", "category": category, "action": action,
                 "outcome": outcome, "task_id": task_id, "ts": ts})
    return h


def head() -> str:
    """The current chain head, from the anchor. O(1); embedded in artefacts."""
    row = db.query_one("SELECT head FROM audit_anchor WHERE id=1")
    return row["head"] if row else GENESIS


def verify_chain() -> dict[str, Any]:
    """Recompute the chain and check it against its anchor.

    Three failures are distinguished, because they mean different things:
    an edited row (hash mismatch), a removed interior row (prev_hash mismatch or
    a gap in the sequence), and a truncated tail (the anchor's head and count no
    longer match what is present).
    """
    rows = db.query("SELECT * FROM audit_log ORDER BY seq ASC")
    anchor = db.query_one("SELECT * FROM audit_anchor WHERE id=1")
    prev = GENESIS
    last_seq: int | None = None
    for r in rows:
        if last_seq is not None and r["seq"] != last_seq + 1:
            return {"ok": False, "broken_at": r["seq"], "entries": len(rows),
                    "reason": f"sequence gap: entry {last_seq + 1} is missing"}
        last_seq = r["seq"]
        if r["prev_hash"] != prev:
            return {"ok": False, "broken_at": r["seq"], "reason": "prev_hash mismatch",
                    "entries": len(rows)}
        h = _digest(prev, r["ts"], r["actor"] or "", r["task_id"] or "", r["category"],
                    r["action"], r["outcome"] or "", r["detail"] or "")
        if h != r["hash"]:
            return {"ok": False, "broken_at": r["seq"], "reason": "hash mismatch",
                    "entries": len(rows)}
        prev = r["hash"]

    if anchor:
        if anchor["entries"] != len(rows):
            return {"ok": False, "broken_at": last_seq, "entries": len(rows),
                    "reason": f"the log holds {len(rows)} entries but the anchor "
                              f"records {anchor['entries']}; entries have been "
                              f"removed"}
        if rows and anchor["head"] != prev:
            return {"ok": False, "broken_at": last_seq, "entries": len(rows),
                    "reason": "the chain head does not match the recorded anchor"}
    return {"ok": True, "entries": len(rows), "head": prev,
            "anchored": bool(anchor)}


class EventBus:
    """Fan-out for the workbench live view. Subscribers are bounded queues; a slow
    consumer drops events rather than stalling the control plane."""

    def __init__(self, maxsize: int = 512) -> None:
        self._subs: set[queue.Queue] = set()
        self._lock = threading.Lock()
        self._maxsize = maxsize

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=self._maxsize)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subs.discard(q)

    def publish(self, event: dict[str, Any]) -> None:
        event.setdefault("ts", db.now())
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)


bus = EventBus()
