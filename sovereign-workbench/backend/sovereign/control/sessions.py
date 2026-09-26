"""Sessions: a conversation with a working set and a permission mode.

A session is what the operator experiences as "a task I delegated": the turns
of one conversation, the documents they attached to it, the artefacts it
produced, the approvals it asked for, and the permission mode it runs under.
Its id is the `conversation_id` of every task in it, so conversations created
before v2 become sessions the first time they are touched.

The working set is authoritative (§6.4). "The attached document" resolves to
the documents in *this session* and to nothing else; with an empty working set
the agent is told so and must ask, rather than choosing a plausible document
from the index.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .. import db
from ..policy.tool_policy import normalise_mode, policy
from . import decisions
from .identity import Principal

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
CAD_EXT = {".dxf", ".dwg"}


class SessionError(ValueError):
    pass


def _kind(doc: dict[str, Any]) -> str:
    """The attachment kind the router reads, derived from the stored file.

    The client used to say "document" for everything, so a photographed
    nameplate was never routed to a vision-capable model.
    """
    ext = Path(doc.get("path") or doc.get("title") or "").suffix.lower()
    if doc.get("doc_class") == "drawing" or ext in CAD_EXT:
        return "drawing"
    if ext in IMAGE_EXT:
        return "image"
    if ext == ".xlsx" or ext == ".xlsm":
        return "spreadsheet"
    return "document"


def create(principal: Principal, *, title: str = "", mode: str | None = None,
           session_id: str | None = None) -> dict[str, Any]:
    principal.require("engineer", "starting a session")
    sid = session_id or db.new_id("sess")
    mode = normalise_mode(mode) if mode else policy.mode
    now = time.time()
    db.insert("sessions", {
        "id": sid, "title": (title or "New session")[:200], "owner": principal.name,
        "department": principal.department, "permission_mode": mode,
        "state": "OPEN", "created_at": now, "updated_at": now})
    decisions.record("tool_policy", "MODE_SET",
                     f"session started in '{mode}' mode",
                     subject_kind="session", subject_id=sid, session_id=sid,
                     principal=principal.name, basis={"mode": mode})
    return get(sid)


def ensure(session_id: str, principal: Principal, *, title: str = "",
           mode: str | None = None) -> dict[str, Any]:
    """The session, creating it if this is a pre-v2 conversation id."""
    row = db.query_one("SELECT * FROM sessions WHERE id=?", (session_id,))
    if row:
        return get(session_id)
    first = db.query_one("SELECT owner, title FROM tasks WHERE conversation_id=? "
                         "ORDER BY created_at LIMIT 1", (session_id,))
    if first and first["owner"] != principal.name \
            and not principal.can("admin"):
        raise SessionError(f"session {session_id} belongs to {first['owner']}")
    return create(principal, title=title or (first["title"] if first else ""),
                  mode=mode, session_id=session_id)


def _row(session_id: str) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM sessions WHERE id=?", (session_id,))
    if not row:
        raise SessionError(f"no session {session_id!r}")
    return dict(row)


def check_access(session_id: str, principal: Principal, *, write: bool = False) -> None:
    """Owners and admins may act on a session; approvers may read any.

    Confidential work is scoped to the person who delegated it. An approver
    needs to see what they are approving, so they can read, not drive.
    """
    s = _row(session_id)
    if s["owner"] == principal.name or principal.can("admin"):
        return
    if not write and principal.can("approver"):
        return
    raise SessionError(f"{principal.name} may not "
                       f"{'act on' if write else 'read'} session {session_id}, "
                       f"which belongs to {s['owner']}")


def get(session_id: str) -> dict[str, Any]:
    s = _row(session_id)
    s["working_set"] = working_set(session_id)
    s["tasks"] = db.rows_to_dicts(db.query(
        "SELECT id, title, state, state_reason, task_type, selected_model, "
        "priority, created_at, finished_at FROM tasks WHERE conversation_id=? "
        "ORDER BY created_at", (session_id,)))
    return s


def list_for(principal: Principal, limit: int = 50) -> list[dict[str, Any]]:
    if principal.can("admin"):
        rows = db.query("SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?",
                        (limit,))
    else:
        rows = db.query("SELECT * FROM sessions WHERE owner=? "
                        "ORDER BY updated_at DESC LIMIT ?", (principal.name, limit))
    return db.rows_to_dicts(rows)


def working_set(session_id: str) -> dict[str, Any]:
    docs = db.rows_to_dicts(db.query(
        "SELECT sd.doc_id, sd.title, sd.added_by, sd.added_at, d.kind AS ext, "
        "d.doc_class, d.pages, d.status, d.path FROM session_documents sd "
        "LEFT JOIN documents d ON d.id = sd.doc_id WHERE sd.session_id=? "
        "ORDER BY sd.added_at", (session_id,)))
    for d in docs:
        d["kind"] = _kind(d)
        d.pop("path", None)
    artefacts = db.rows_to_dicts(db.query(
        "SELECT a.id, a.name, a.kind, a.bytes, a.sha256, a.task_id, a.created_at "
        "FROM artifacts a JOIN tasks t ON t.id = a.task_id "
        "WHERE t.conversation_id=? ORDER BY a.created_at", (session_id,)))
    approvals = db.rows_to_dicts(db.query(
        "SELECT a.id, a.tool, a.summary, a.state, a.decided_by, a.created_at, "
        "a.decided_at, a.task_id FROM approvals a JOIN tasks t ON t.id = a.task_id "
        "WHERE t.conversation_id=? ORDER BY a.created_at", (session_id,)))
    return {"documents": docs, "artefacts": artefacts, "approvals": approvals}


def attach(session_id: str, doc_id: str, principal: Principal) -> dict[str, Any]:
    check_access(session_id, principal, write=True)
    doc = db.query_one("SELECT id, title, kind, pages, status, doc_class, path "
                       "FROM documents WHERE id=?", (doc_id,))
    if not doc:
        raise SessionError(f"no indexed document {doc_id!r}")
    db.execute(
        "INSERT OR IGNORE INTO session_documents (session_id, doc_id, title, kind, "
        "pages, added_by, added_at) VALUES (?,?,?,?,?,?,?)",
        (session_id, doc_id, doc["title"], _kind(dict(doc)), doc["pages"],
         principal.name, time.time()))
    touch(session_id)
    return working_set(session_id)


def detach(session_id: str, doc_id: str, principal: Principal) -> dict[str, Any]:
    check_access(session_id, principal, write=True)
    db.execute("DELETE FROM session_documents WHERE session_id=? AND doc_id=?",
               (session_id, doc_id))
    touch(session_id)
    return working_set(session_id)


def attachments(session_id: str) -> list[dict[str, Any]]:
    """The working set's documents in the shape the router and harness read."""
    out = []
    for d in working_set(session_id)["documents"]:
        out.append({"doc_id": d["doc_id"], "title": d["title"],
                    "pages": d["pages"] or 1, "kind": d["kind"],
                    "status": d["status"]})
    return out


def set_mode(session_id: str, mode: str, principal: Principal) -> dict[str, Any]:
    check_access(session_id, principal, write=True)
    mode = normalise_mode(mode)
    old = _row(session_id)["permission_mode"]
    db.update("sessions", "id", session_id,
              {"permission_mode": mode, "updated_at": time.time()})
    decisions.record("tool_policy", "MODE_SET",
                     f"permission mode changed from '{old}' to '{mode}'",
                     subject_kind="session", subject_id=session_id,
                     session_id=session_id, principal=principal.name,
                     basis={"from": old, "to": mode})
    return get(session_id)


def touch(session_id: str) -> None:
    db.update("sessions", "id", session_id, {"updated_at": time.time()})


def submit(principal: Principal, *, prompt: str, session_id: str | None = None,
           doc_ids: list[str] | None = None,
           attachments: list[dict[str, Any]] | None = None,
           workflow: str = "general", priority: str | None = None,
           task_type: str | None = None, title: str = "",
           mode: str | None = None, model: str | None = None) -> dict[str, Any]:
    """Delegate one turn of a session. Returns ids and the task's working set.

    Documents named here join the session's working set; the task is then
    given the *whole* working set, so a follow-up turn about "the valves"
    still sees the drawing attached two turns ago.
    """
    from ..runtime.scheduler import scheduler

    principal.require("engineer", "delegating a task")
    prompt = (prompt or "").strip()
    if not prompt:
        raise SessionError("prompt is required")
    if session_id:
        ensure(session_id, principal, title=title or prompt[:90], mode=mode)
        check_access(session_id, principal, write=True)
    else:
        session_id = create(principal, title=title or prompt[:90], mode=mode)["id"]

    wanted = list(doc_ids or [])
    for a in attachments or []:
        if isinstance(a, dict) and a.get("doc_id"):
            wanted.append(str(a["doc_id"]))
    for did in dict.fromkeys(wanted):
        attach(session_id, did, principal)

    s = _row(session_id)
    tid = scheduler.submit(
        title=title or prompt[:90], prompt=prompt, workflow=workflow or "general",
        owner=principal.name, department=principal.department, priority=priority,
        attachments=attachments_for_task(session_id, attachments),
        task_type=task_type, conversation_id=session_id,
        policy_mode=s["permission_mode"], model=model)
    touch(session_id)
    return {"task_id": tid, "session_id": session_id,
            "conversation_id": session_id,
            "permission_mode": s["permission_mode"],
            "working_set": working_set(session_id)}


def attachments_for_task(session_id: str,
                         extra: list[dict[str, Any]] | None = None
                         ) -> list[dict[str, Any]]:
    """Session documents, plus non-document attachments the caller passed.

    Non-document attachments (a path to a drawing on the appliance, used by the
    drawing workflow) are kept; anything with a doc_id is taken from the
    working set, whose kind is derived from the stored file, not from the
    client's label.
    """
    out = attachments(session_id)
    for a in extra or []:
        if isinstance(a, dict) and not a.get("doc_id"):
            out.append(a)
    return out
