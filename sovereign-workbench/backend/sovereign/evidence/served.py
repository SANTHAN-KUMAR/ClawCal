"""The node's own record of what it served (sovereign-workbench-v2.md §13).

When the agent loop ran on the node, the passages it had retrieved lived in its
own memory and the provenance check could read them there. With the loop on a
laptop, the node must not take the client's word for what it retrieved: a
client could claim a passage it was never given. So every span the node hands
out — a retrieved chunk, a page read in full, an extracted value — is recorded
here against the session, the task, the principal and the device, and the gate
checks claims against this record and nothing else.

The span id is the load-bearing object: minted once at ingestion (the chunk
id), recorded here when served, verified at the gate, carried into the
deliverable as a link back to the page and region it came from.
"""
from __future__ import annotations

import time
from typing import Any

from .. import db


def _task_meta(task_id: str | None) -> dict[str, Any]:
    if not task_id:
        return {}
    row = db.query_one("SELECT conversation_id, owner, device_id FROM tasks "
                       "WHERE id=?", (task_id,))
    return dict(row) if row else {}


def record(task_id: str | None, passages: list[dict[str, Any]], *, via: str,
           session_id: str | None = None, principal: str | None = None) -> int:
    """Record spans served to a task (and so to its session). Returns count."""
    if not passages:
        return 0
    meta = _task_meta(task_id)
    sid = session_id or meta.get("conversation_id")
    now = time.time()
    rows = []
    for p in passages:
        sid_ = str(p.get("chunk_id") or p.get("span_id") or "")
        text = str(p.get("text") or "")
        if not sid_ or not text:
            continue
        region = p.get("region")
        rows.append((sid, task_id, meta.get("device_id"),
                     principal or meta.get("owner"), sid_, p.get("doc_id"),
                     p.get("doc_title"), p.get("page_no"),
                     db.jdump(region) if region not in (None, "") else None,
                     text[:8000], via, now))
    if rows:
        db.executemany(
            "INSERT INTO served_spans (session_id, task_id, device_id, principal, "
            "span_id, doc_id, doc_title, page_no, region, text, via, served_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


def index(*, session_id: str | None = None,
          task_id: str | None = None) -> dict[str, dict[str, Any]]:
    """span_id -> the span as served. Session-wide when the session is known:
    a figure retrieved two turns ago was still served to this session."""
    if session_id:
        rows = db.query("SELECT * FROM served_spans WHERE session_id=? "
                        "ORDER BY id", (session_id,))
    elif task_id:
        rows = db.query("SELECT * FROM served_spans WHERE task_id=? ORDER BY id",
                        (task_id,))
    else:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        d = dict(r)
        d["region"] = db.jload(d["region"], None)
        out[d["span_id"]] = d
    return out


def passages(*, session_id: str | None = None,
             task_id: str | None = None) -> list[dict[str, Any]]:
    """The served set, in the passage shape the evidence engine reads."""
    return [{"chunk_id": s["span_id"], "doc_id": s["doc_id"],
             "doc_title": s["doc_title"], "page_no": s["page_no"],
             "text": s["text"], "region": s["region"]}
            for s in index(session_id=session_id, task_id=task_id).values()]


def span(span_id: str) -> dict[str, Any] | None:
    """The span as the node minted or served it: a chunk, or a served record."""
    row = db.query_one(
        "SELECT c.id AS span_id, c.doc_id, d.title AS doc_title, c.page_no, "
        "c.region, c.text, d.data_class FROM chunks c JOIN documents d "
        "ON d.id = c.doc_id WHERE c.id=?", (span_id,))
    if not row:
        row = db.query_one(
            "SELECT s.span_id, s.doc_id, s.doc_title, s.page_no, s.region, s.text, "
            "d.data_class FROM served_spans s LEFT JOIN documents d "
            "ON d.id = s.doc_id WHERE s.span_id=? ORDER BY s.id DESC LIMIT 1",
            (span_id,))
    if not row:
        return None
    d = dict(row)
    d["region"] = db.jload(d["region"], None)
    return d
