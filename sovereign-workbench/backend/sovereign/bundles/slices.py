"""Instrument slices: the gate, off-site, for chosen documents (spec §14).

By default the corpus never leaves the node. An organisation may deliberately
export a *slice* — selected documents, already ingested, with their span ids —
to one detached device, so that the provenance gate works off-site for those
documents alone:

* only to grades the policy allows (`slice_export_grades`, A and B by default),
  and only documents the device's grade is cleared for;
* the slice is a SQLite database (spans + a full-text index), encrypted with a
  fresh data key; the key is wrapped to the device's key (`sealed`), so the file
  is ciphertext anywhere else;
* its signed manifest carries an expiry equal to the device's lease grace: the
  client refuses to open it after that, and sealing wipes the wrapped key;
* the export *is* serving: every span is recorded as served to the slice's
  session, so the gate's rule is unchanged off-site, and on rejoin the node can
  write the .docx from the same claims (`POST /api/slices/{id}/deliver`).

Revoking the device marks its slices revoked; a revoked slice is never served
again. What a device already holds is protected by its lease, not recalled — the
honest limit, stated in docs/trust-domain.md.
"""
from __future__ import annotations

import base64
import hashlib
import os
import sqlite3
import time
from typing import Any

from .. import audit, db, sealed, signing
from ..config import DATA_DIR

SLICE_DIR = DATA_DIR / "slices"


class SliceError(ValueError):
    pass


def _spans_for(doc_id: str) -> list[dict[str, Any]]:
    """Every span the node would serve from this document: its chunks, and the
    values its pattern extractor reads (the same ids `extract_values` returns)."""
    doc = db.query_one("SELECT title FROM documents WHERE id=?", (doc_id,))
    out = [{"span_id": r["id"], "doc_id": doc_id, "doc_title": doc["title"],
            "page_no": r["page_no"], "region": r["region"], "text": r["text"]}
           for r in db.query("SELECT id, page_no, region, text FROM chunks "
                             "WHERE doc_id=? ORDER BY page_no, ordinal", (doc_id,))]
    try:
        from ..knowledge import extract
        fields = extract.extract_document(doc_id)["fields"]
        for name, f in fields.items():
            out.append({"span_id": f"{doc_id}:{name}", "doc_id": doc_id,
                        "doc_title": doc["title"], "page_no": f["page"],
                        "region": db.jdump(f["region"]) if f.get("region") else None,
                        "text": f["source_line"]})
    except Exception:                                          # noqa: BLE001
        pass
    return out


def _build_db(docs: list[dict[str, Any]], spans: list[dict[str, Any]]) -> bytes:
    c = sqlite3.connect(":memory:")
    c.executescript("""
        CREATE TABLE documents (id TEXT PRIMARY KEY, title TEXT, doc_class TEXT,
                                data_class TEXT, pages INTEGER);
        CREATE TABLE spans (span_id TEXT PRIMARY KEY, doc_id TEXT, doc_title TEXT,
                            page_no INTEGER, region TEXT, text TEXT);
        CREATE VIRTUAL TABLE spans_fts USING fts5(text, span_id UNINDEXED,
                                                  tokenize='porter unicode61');
    """)
    c.executemany("INSERT INTO documents VALUES (?,?,?,?,?)",
                  [(d["id"], d["title"], d["doc_class"], d["data_class"], d["pages"])
                   for d in docs])
    c.executemany("INSERT OR REPLACE INTO spans VALUES (?,?,?,?,?,?)",
                  [(s["span_id"], s["doc_id"], s["doc_title"], s["page_no"],
                    s["region"], s["text"]) for s in spans])
    c.executemany("INSERT INTO spans_fts (text, span_id) VALUES (?,?)",
                  [(s["text"], s["span_id"]) for s in spans])
    c.commit()
    data = c.serialize()
    c.close()
    return data


def export(device_id: str, doc_ids: list[str], principal: Any) -> dict[str, Any]:
    from ..control import decisions, devices, leases, trust
    from ..evidence import served
    principal.require("approver", "exporting documents off-site")
    dev = devices.get(device_id)
    if dev["state"] != "ACTIVE":
        raise SliceError(f"{device_id} is {dev['state']}")
    grade = dev.get("grade") or "D"
    allowed = trust.policy().get("slice_export_grades", ["A", "B"])
    if grade not in allowed:
        raise SliceError(f"grade {grade} may not carry documents off-site; the "
                         f"policy allows slices only to grades {', '.join(allowed)}")
    lease = leases.current(device_id)
    if not lease or lease["state"] != "ACTIVE":
        raise SliceError("the device holds no active lease to bind the slice to")
    clearance = trust.clearance(grade)
    docs = []
    for did in dict.fromkeys(doc_ids):
        d = db.query_one("SELECT id, title, doc_class, data_class, pages, status "
                         "FROM documents WHERE id=?", (did,))
        if not d:
            raise SliceError(f"no document {did!r}")
        if (d["data_class"] or "internal") not in clearance:
            raise SliceError(f"{d['title']} is {d['data_class']}; grade {grade} is "
                             f"not cleared for it")
        if d["status"] != "READY":
            raise SliceError(f"{d['title']} is not fully ingested")
        docs.append(dict(d))
    if not docs:
        raise SliceError("choose at least one document")
    spans = [s for d in docs for s in _spans_for(d["id"])]
    plain = _build_db(docs, spans)
    sid = db.new_id("slice")
    session = f"sess-{sid}"
    key = os.urandom(32)
    blob = sealed.seal(key, plain, sid.encode())
    wrapped = sealed.wrap_for_device(key, bytes.fromhex(dev["public_key"]),
                                     sid.encode())
    SLICE_DIR.mkdir(parents=True, exist_ok=True)
    (SLICE_DIR / f"{sid}.bin").write_bytes(blob)
    digest = hashlib.sha256(blob).hexdigest()
    body = {"schema": "workbench.slice/v1", "slice_id": sid, "device_id": device_id,
            "session_id": session, "domain": trust.domain_name(),
            "docs": [{"id": d["id"], "title": d["title"], "data_class": d["data_class"]}
                     for d in docs],
            "spans": len(spans), "sha256": digest, "bytes": len(blob),
            "created_at": round(time.time(), 3),
            "expires_at": round(lease["grace_until"], 3), "lease_id": lease["id"]}
    seed, pub = leases.node_key()
    manifest = {"slice": body, "signature": {"alg": "ed25519", "key": pub.hex(),
                "sig": signing.sign(seed, signing.canonical(body)).hex()}}
    # The export is serving: record it, so the gate's rule holds off-site and
    # the node recognises these spans when the device comes back to deliver.
    tid = db.new_id("task")
    db.insert("tasks", {"id": tid, "conversation_id": session,
                        "title": f"slice export to {device_id}", "prompt": "",
                        "owner": dev["principal"], "workflow": "slice_export",
                        "state": "COMPLETED", "device_id": device_id, "grade": grade,
                        "placement": "client", "created_at": time.time(),
                        "finished_at": time.time()})
    served.record(tid, [dict(s, chunk_id=s["span_id"],
                             region=db.jload(s["region"], None)) for s in spans],
                  via="slice_export", session_id=session, principal=dev["principal"])
    db.insert("slices", {"id": sid, "device_id": device_id, "session_id": session,
                         "docs": db.jdump([d["id"] for d in docs]),
                         "spans": len(spans), "bytes": len(blob), "sha256": digest,
                         "created_by": principal.name, "created_at": time.time(),
                         "expires_at": lease["grace_until"], "state": "ACTIVE"})
    (SLICE_DIR / f"{sid}.json").write_text(db.jdump({"manifest": manifest,
                                                     "wrapped": wrapped}))
    decisions.record("trust", "SLICE_EXPORTED",
                     f"{principal.name} exported {len(docs)} document(s), "
                     f"{len(spans)} spans, to {device_id} (grade {grade}) until "
                     f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(lease['grace_until']))}",
                     subject_kind="slice", subject_id=sid, principal=principal.name,
                     basis={"device_id": device_id, "docs": [d["id"] for d in docs],
                            "sha256": digest})
    audit.record("trust", "slice_exported", actor=principal.name,
                 detail={"slice_id": sid, "device_id": device_id, "docs": len(docs)})
    return {"slice_id": sid, "session_id": session, "docs": body["docs"],
            "spans": len(spans), "bytes": len(blob), "expires_at": body["expires_at"]}


def fetch(slice_id: str, device_id: str) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM slices WHERE id=?", (slice_id,))
    if not row or row["device_id"] != device_id:
        raise SliceError("no such slice for this device")
    if row["state"] != "ACTIVE":
        raise SliceError(f"this slice is {row['state']}")
    meta = db.jload((SLICE_DIR / f"{slice_id}.json").read_text(), {})
    blob = (SLICE_DIR / f"{slice_id}.bin").read_bytes()
    audit.record("trust", "slice_fetched", detail={"slice_id": slice_id,
                                                   "device_id": device_id})
    return {"manifest": meta["manifest"], "wrapped": meta["wrapped"],
            "data": base64.b64encode(blob).decode()}


def for_device(device_id: str) -> list[dict[str, Any]]:
    rows = db.rows_to_dicts(db.query(
        "SELECT id, session_id, docs, spans, bytes, created_by, created_at, "
        "expires_at, state FROM slices WHERE device_id=? ORDER BY created_at DESC",
        (device_id,)))
    for r in rows:
        r["docs"] = db.jload(r["docs"], [])
    return rows


def deliver(slice_id: str, principal: Any, args: dict[str, Any],
            node_url: str) -> dict[str, Any]:
    """On rejoin: the node writes the .docx from claims gated against the slice."""
    from ..tools import ToolContext, register_all
    from ..tools.remote import DeliverTool
    from ..config import WORKSPACE_DIR
    row = db.query_one("SELECT * FROM slices WHERE id=?", (slice_id,))
    if not row:
        raise SliceError("no such slice")
    dev = db.query_one("SELECT principal FROM devices WHERE id=?", (row["device_id"],))
    if dev["principal"] != principal.name and not principal.can("admin"):
        raise SliceError("this slice belongs to another principal")
    register_all()
    tid = db.new_id("task")
    db.insert("tasks", {"id": tid, "conversation_id": row["session_id"],
                        "title": f"deliver from {slice_id}", "prompt": "",
                        "owner": principal.name, "workflow": "slice_deliver",
                        "state": "COMPLETED", "device_id": row["device_id"],
                        "placement": "node", "created_at": time.time(),
                        "finished_at": time.time()})
    ws = WORKSPACE_DIR / tid
    ws.mkdir(parents=True, exist_ok=True)
    ctx = ToolContext(task_id=tid, workspace=ws, principal=principal.name,
                      session_id=row["session_id"], scratch={"node_url": node_url})
    res = DeliverTool().run(args, ctx)
    if not res.ok:
        raise SliceError(res.error)
    return {**res.content, "outcome": res.outcome, "outcome_reason": res.outcome_reason}
