"""Instrument slices on the device (sovereign-workbench-v2.md §14).

A slice arrives encrypted to this device's key and bound to its lease. Off-site
it gives the harness what the node's tools give it on-site, for those documents
only:

    search    full-text search over the slice's spans, each with its span id
    gate      the node's own gate rules (`gatecore`, vendored) against the slice
    deliver   a gated report written locally now, and queued so the node renders
              the organisation's .docx from the same claims on rejoin

The slice opens only while the lease holds (the manifest carries the lease's
grace deadline); sealing the device removes the whole `org/` tree, wrapped keys
included, so what is left is ciphertext.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from . import trust
from .vendor import gatecore, sealed, tuf_verify


class SliceError(RuntimeError):
    pass


def root() -> Path:
    return trust.home() / "org" / "slices"


def fetch(api: Any, slice_id: str) -> dict[str, Any]:
    trust.enforce_lease()
    api.headers = trust.device_headers()
    out = api.get(f"/api/slices/{slice_id}")
    st = trust.load_state()
    if not tuf_verify.verify_signed_document(out["manifest"], st["node_key"], "slice"):
        raise SliceError("the slice manifest is not signed by this device's node")
    body = out["manifest"]["slice"]
    if body["device_id"] != st["device_id"]:
        raise SliceError("this slice was exported to another device")
    data = base64.b64decode(out["data"])
    if hashlib.sha256(data).hexdigest() != body["sha256"]:
        raise SliceError("the slice does not match its signed hash; discarded")
    d = root() / slice_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "data.bin").write_bytes(data)
    (d / "manifest.json").write_text(json.dumps(out["manifest"], sort_keys=True))
    (d / "key.json").write_text(json.dumps(out["wrapped"]))
    trust.log("slice_fetched", {"slice_id": slice_id, "docs": len(body["docs"]),
                                "spans": body["spans"], "sha256": body["sha256"]})
    return body


def local() -> list[dict[str, Any]]:
    out = []
    if root().exists():
        for d in sorted(root().iterdir()):
            try:
                out.append(json.loads((d / "manifest.json").read_text())["slice"])
            except (OSError, ValueError, KeyError):
                continue
    return out


def _open(slice_id: str) -> sqlite3.Connection:
    trust.enforce_lease()
    d = root() / slice_id
    try:
        man = json.loads((d / "manifest.json").read_text())
        wrapped = json.loads((d / "key.json").read_text())
    except (OSError, ValueError):
        raise SliceError(f"no slice {slice_id} on this device") from None
    st = trust.load_state()
    if not tuf_verify.verify_signed_document(man, st.get("node_key", ""), "slice"):
        raise SliceError("the stored slice manifest no longer verifies")
    body = man["slice"]
    if time.time() > float(body["expires_at"]):
        raise SliceError("this slice expired with the lease it was bound to; "
                         "re-attach to renew")
    seed, _pub = trust.device_key()
    key = sealed.unwrap_on_device(wrapped, seed, slice_id.encode())
    plain = sealed.open_(key, (d / "data.bin").read_bytes(), slice_id.encode())
    c = sqlite3.connect(":memory:")
    c.deserialize(plain)
    c.row_factory = sqlite3.Row
    return c


def _ids(slice_id: str | None) -> list[str]:
    ids = [s["slice_id"] for s in local()]
    if slice_id:
        if slice_id not in ids:
            raise SliceError(f"no slice {slice_id} on this device")
        return [slice_id]
    return ids


def documents(slice_id: str | None = None) -> list[dict[str, Any]]:
    out = []
    for sid in _ids(slice_id):
        c = _open(sid)
        out += [dict(r, slice_id=sid) for r in c.execute("SELECT * FROM documents")]
        c.close()
    return out


def search(query: str, k: int = 6, slice_id: str | None = None) -> list[dict[str, Any]]:
    """Full-text search; the same passage shape the node's retrieve returns."""
    terms = [t for t in "".join(ch if ch.isalnum() else " " for ch in query).split()
             if len(t) > 2]
    if not terms:
        return []
    match = " OR ".join(f'"{t}"' for t in terms)
    hits = []
    for sid in _ids(slice_id):
        c = _open(sid)
        rows = c.execute(
            "SELECT s.*, bm25(spans_fts) AS rank FROM spans_fts f JOIN spans s "
            "ON s.span_id = f.span_id WHERE spans_fts MATCH ? ORDER BY rank LIMIT ?",
            (match, k)).fetchall()
        hits += [dict(r, slice_id=sid) for r in rows]
        c.close()
    hits.sort(key=lambda r: r["rank"])
    hits = hits[:k]
    trust.log("slice_served", {"query": query[:200],
                               "spans": [h["span_id"] for h in hits]})
    return hits


def _index(slice_id: str) -> dict[str, dict[str, Any]]:
    c = _open(slice_id)
    idx = {r["span_id"]: dict(r, region=json.loads(r["region"]) if r["region"]
                              else None) for r in c.execute("SELECT * FROM spans")}
    c.close()
    return idx


def deliver(title: str, sections: Any, *, slice_id: str | None = None,
            kind: str = "report", out_dir: Path | None = None) -> dict[str, Any]:
    """Gate a draft against the slice and write it now; queue the .docx."""
    ids = _ids(slice_id)
    if not ids:
        raise SliceError("no slice on this device; deliverables need the node")
    sid = ids[0]
    secs = gatecore.normalise_sections(sections)
    if not secs:
        raise SliceError("no claims found in the sections")
    flat = [c for s in secs for c in s["claims"]]
    claims, counts = gatecore.evaluate(flat, _index(sid))
    it = iter(claims)
    body = []
    for s in secs:
        body.append(f"<h2>{html.escape(s['heading'])}</h2>")
        for _ in s["claims"]:
            c = next(it)
            cites = "".join(
                f' <sup title="{html.escape(k.get("doc_title") or "")} p.'
                f'{k.get("page_no")}">[{html.escape(k["span_id"] or "calc")}]</sup>'
                for k in c["kept"])
            body.append(f"<p>{html.escape(c['text_out'])}{cites}</p>")
    stripped = [s for c in claims for s in c["stripped"]]
    body.append("<h2>Gate</h2><p>" + html.escape(
        f"{counts['kept']} value(s) kept against the slice's spans, "
        f"{counts['stripped']} stripped.") + "</p>")
    if stripped:
        body.append("<ul>" + "".join(f"<li>{html.escape(s['value'])} — "
                                     f"{html.escape(s['reason'])}</li>"
                                     for s in stripped) + "</ul>")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = (out_dir or Path.cwd()) / f"clawcal-{stamp}.html"
    out.write_text(f"<!doctype html><meta charset=utf-8><title>{html.escape(title)}"
                   f"</title><style>body{{font:15px/1.55 system-ui;max-width:760px;"
                   f"margin:32px auto;padding:0 16px;color:#0a0a0a}}sup{{color:#555}}"
                   f"</style><h1>{html.escape(title)}</h1>"
                   f"<p><em>Drafted off-site against slice {sid}; the organisation's "
                   f".docx is written by the node on rejoin.</em></p>" + "".join(body))
    outbox = root() / sid / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    (outbox / f"{stamp}.json").write_text(json.dumps(
        {"kind": kind, "title": title, "sections": secs}))
    trust.log("slice_gate", {"slice_id": sid, "counts": counts, "report": out.name})
    return {"report": str(out), "counts": counts, "stripped": stripped,
            "queued_for_node": str(outbox / f"{stamp}.json")}


def flush_outbox(api: Any) -> list[dict[str, Any]]:
    """On rejoin: the node writes each queued deliverable as a .docx."""
    done = []
    for s in local():
        box = root() / s["slice_id"] / "outbox"
        for f in sorted(box.glob("*.json")) if box.exists() else []:
            out = api.post(f"/api/slices/{s['slice_id']}/deliver",
                           json.loads(f.read_text()))
            trust.log("slice_delivered", {"slice_id": s["slice_id"],
                                          "artifact": out.get("name"),
                                          "gate": (out.get("gate") or {}).get("counts")})
            f.unlink()
            done.append(out)
    return done
