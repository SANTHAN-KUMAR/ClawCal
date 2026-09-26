"""Artefacts the security officer files: the sovereignty report and the audit export.

Both are signed with the appliance's Ed25519 key (`sovereign.signing`) and both
verify offline, with no access to the appliance:

    clawcal verify <file>         # or: python3 -m sovereign.control.report verify <file>

The sovereignty report is a PDF plus a `.sig.json` sidecar. The sidecar holds
the SHA-256 of the PDF's exact bytes, the structured findings the PDF was
rendered from, the appliance's public key and a signature over all of it.
Changing a byte of the PDF, or of the findings, fails verification.

The audit export is JSONL: a header, every audit row, every decision row, and a
final signature line over everything before it. The verifier recomputes both
hash chains from the rows alone, so a verified export proves the chain was
intact when it was exported, not merely that the file was not edited since.
"""
from __future__ import annotations

import hashlib
import json
import platform
import sys
import time
from pathlib import Path
from typing import Any

from .. import audit, db
from ..config import ARTIFACT_DIR, DATA_DIR, settings
from .. import signing

KEYS_DIR = DATA_DIR / "keys"


def _canonical(obj: Any) -> bytes:
    return signing.canonical(obj)


def _key() -> tuple[bytes, bytes]:
    return signing.appliance_key(KEYS_DIR)


def public_key_info() -> dict[str, str]:
    _, pub = _key()
    return {"public_key": pub.hex(), "fingerprint": signing.fingerprint(pub),
            "algorithm": "Ed25519"}


def _register(path: Path, task_id: str | None, kind: str, meta: dict[str, Any]) -> str:
    data = path.read_bytes()
    aid = db.new_id("art")
    db.insert("artifacts", {
        "id": aid, "task_id": task_id, "name": path.name, "kind": kind,
        "path": str(path), "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(), "meta": db.jdump(meta),
        "created_at": time.time()})
    return aid


# ------------------------------------------------------------ audit export

def export_audit(principal: str) -> Path:
    seed, pub = _key()
    ts = time.strftime("%Y%m%d-%H%M%S")
    path = ARTIFACT_DIR / f"audit-export-{ts}.jsonl"
    h = hashlib.sha256()
    lines: list[bytes] = []

    def emit(obj: dict[str, Any]) -> None:
        line = _canonical(obj) + b"\n"
        h.update(line)
        lines.append(line)

    emit({"type": "header", "format": "clawcal-audit-export/1",
          "appliance": platform.node(), "org": settings.org_name,
          "exported_by": principal, "exported_at": time.time(),
          "public_key": pub.hex(), "fingerprint": signing.fingerprint(pub)})
    for r in db.query("SELECT * FROM audit_log ORDER BY seq"):
        emit({"type": "audit", **dict(r)})
    for r in db.query("SELECT * FROM decisions ORDER BY seq"):
        emit({"type": "decision", **dict(r)})
    anchor = db.query_one("SELECT * FROM audit_anchor WHERE id=1")
    emit({"type": "anchor", **(dict(anchor) if anchor else {})})
    digest = h.hexdigest()
    sig = signing.sign(seed, digest.encode())
    lines.append(_canonical({"type": "signature", "sha256": digest,
                             "signature": sig.hex()}) + b"\n")
    path.write_bytes(b"".join(lines))
    audit.record("audit", "exported", actor=principal,
                 detail={"file": path.name, "sha256": digest})
    _register(path, None, "audit_export", {"sha256": digest,
                                           "fingerprint": signing.fingerprint(pub)})
    return path


def verify_audit_export(path: Path, trusted_key: str | None = None) -> dict[str, Any]:
    """Offline verification. Needs only this file and, ideally, the known key."""
    from ..audit import _digest as audit_digest
    from .decisions import _digest as dec_digest, GENESIS

    raw = path.read_bytes().splitlines(keepends=True)
    if not raw:
        return {"ok": False, "reason": "empty file"}
    sigrec = json.loads(raw[-1])
    body = b"".join(raw[:-1])
    digest = hashlib.sha256(body).hexdigest()
    header = json.loads(raw[0])
    pub = bytes.fromhex(header["public_key"])
    if trusted_key and trusted_key.lower() != pub.hex():
        return {"ok": False, "reason": "signed by a key other than the trusted one"}
    if sigrec.get("type") != "signature" or sigrec.get("sha256") != digest:
        return {"ok": False, "reason": "the file was modified after it was signed"}
    if not signing.verify(pub, digest.encode(), bytes.fromhex(sigrec["signature"])):
        return {"ok": False, "reason": "signature does not verify"}

    prev_a, prev_d = "0" * 64, GENESIS
    n_a = n_d = 0
    anchor = {}
    for line in raw[1:-1]:
        r = json.loads(line)
        if r["type"] == "audit":
            if r["prev_hash"] != prev_a or audit_digest(
                    prev_a, r["ts"], r["actor"] or "", r["task_id"] or "",
                    r["category"], r["action"], r["outcome"] or "",
                    r["detail"] or "") != r["hash"]:
                return {"ok": False, "reason": f"audit chain broken at {r['seq']}"}
            prev_a = r["hash"]
            n_a += 1
        elif r["type"] == "decision":
            if r["prev_hash"] != prev_d or dec_digest(
                    prev_d, r["ts"], r["authority"], r["subject_kind"] or "",
                    r["subject_id"] or "", r["task_id"] or "",
                    r["session_id"] or "", r["outcome"], r["reason"] or "",
                    r["basis"] or "", r["principal"] or "") != r["hash"]:
                return {"ok": False, "reason": f"decision chain broken at {r['seq']}"}
            prev_d = r["hash"]
            n_d += 1
        elif r["type"] == "anchor":
            anchor = r
    if anchor and (anchor.get("head") != prev_a or anchor.get("entries") != n_a):
        return {"ok": False, "reason": "the export does not match its own anchor: "
                                       "entries were removed before export"}
    return {"ok": True, "audit_entries": n_a, "decisions": n_d,
            "fingerprint": signing.fingerprint(pub),
            "exported_by": header.get("exported_by"),
            "exported_at": header.get("exported_at")}


# ------------------------------------------------------- sovereignty report

def _findings(task_id: str) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    result = db.jload(row["result"], {}) or {}
    chain = audit.verify_chain()
    from . import decisions
    return {
        "task_id": task_id,
        "run_by": row["owner"],
        "run_at": row["finished_at"] or row["created_at"],
        "appliance": platform.node(),
        "org": {"name": settings.org_name, "unit": settings.org_unit},
        "all_blocked": bool(result.get("all_blocked")),
        "findings": result.get("findings") or [],
        "nftables": result.get("nftables") or {},
        "denial_events": result.get("denial_events") or [],
        "audit_chain": {"ok": chain.get("ok"), "entries": chain.get("entries"),
                        "head": chain.get("head"), "reason": chain.get("reason")},
        "decision_chain": decisions.verify(),
        "summary": result.get("summary", ""),
    }


def _esc(s: Any) -> str:
    return (str(s if s is not None else "").replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def _render_pdf(f: dict[str, Any], key: dict[str, str], path: Path) -> None:
    import fitz

    nft = f["nftables"] or {}
    host = ("APPLIED" if nft.get("loaded") else
            "COULD NOT BE READ (needs root)" if nft.get("loaded") is None else
            "NOT APPLIED")
    if not f["all_blocked"]:
        verdict = "FAIL — at least one attempt was not refused, or a layer did not run"
    elif nft.get("loaded"):
        verdict = "PASS — every outbound attempt was refused at every layer"
    else:
        verdict = ("PASS, DEGRADED — every attempt was refused by the application "
                   "guard and the sandbox, but the host firewall layer is "
                   + ("not applied" if nft.get("loaded") is False
                      else "unverified (reading it needs root)"))
    rows = "".join(
        f"<tr><td>{_esc(x.get('layer'))}</td><td>{_esc(x.get('target'))}</td>"
        f"<td><b>{_esc(x.get('result'))}</b></td></tr>" for x in f["findings"])
    denials = "".join(
        f"<tr><td>{_esc(time.strftime('%H:%M:%S', time.localtime(d.get('ts', 0))))}"
        f"</td><td>{_esc(d.get('destination'))}</td><td>{_esc(d.get('port'))}</td>"
        f"<td>{_esc(d.get('layer'))}</td><td>{_esc(d.get('result'))}</td></tr>"
        for d in f["denial_events"][:40])
    when = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(f["run_at"]))
    html = f"""
<h1>Sovereignty self-test report</h1>
<p>{_esc(f['org']['name'])} — {_esc(f['org']['unit'])}<br/>
Appliance <b>{_esc(f['appliance'])}</b>, test {_esc(f['task_id'])}, run by
<b>{_esc(f['run_by'])}</b> at {_esc(when)}</p>
<h2>Verdict: {_esc(verdict)}</h2>
<p>Host default-deny firewall (nftables): <b>{_esc(host)}</b><br/>
Audit chain: <b>{'VERIFIED' if f['audit_chain']['ok'] else 'BROKEN'}</b> over
{_esc(f['audit_chain']['entries'])} entries, head {_esc(f['audit_chain']['head'])}<br/>
Decision record: <b>{'VERIFIED' if f['decision_chain'].get('ok') else 'BROKEN'}</b>
over {_esc(f['decision_chain'].get('entries'))} decisions</p>
<h2>Attempts</h2>
<table border="1" cellpadding="3"><tr><th>Layer</th><th>Destination</th>
<th>Result</th></tr>{rows}</table>
<h2>Denials recorded and attributed to this test</h2>
<table border="1" cellpadding="3"><tr><th>Time</th><th>Destination</th><th>Port</th>
<th>Layer</th><th>Result</th></tr>{denials or '<tr><td colspan=5>none</td></tr>'}
</table>
<h2>Signature</h2>
<p>This PDF is accompanied by <b>{_esc(path.name)}.sig.json</b>, an Ed25519
signature over the SHA-256 of this file's exact bytes and the findings above.
Appliance key fingerprint: <b>{_esc(key['fingerprint'])}</b><br/>
Verify offline with: <i>clawcal verify {_esc(path.name)}</i></p>
<p><small>{_esc(f['summary'])}</small></p>
"""
    css = ("body{font-family:sans-serif;font-size:10pt} h1{font-size:17pt}"
           "h2{font-size:12pt;margin-top:10pt} table{border-collapse:collapse}"
           "td,th{font-size:8.5pt}")
    story = fitz.Story(html=html, user_css=css)
    rect = fitz.paper_rect("a4")
    where = rect + (40, 40, -40, -40)
    tmp = path.with_suffix(".tmp.pdf")
    writer = fitz.DocumentWriter(str(tmp))
    more = True
    while more:
        dev = writer.begin_page(rect)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()
    # Metadata is written before the file is hashed and signed, so the signed
    # bytes are the bytes filed.
    doc = fitz.open(str(tmp))
    doc.set_metadata({"title": "Sovereignty self-test report",
                      "author": "ClawCal", "subject": f["task_id"],
                      "keywords": f"clawcal audit-head {f['audit_chain']['head']}"})
    doc.save(str(path), garbage=3, deflate=True)
    doc.close()
    tmp.unlink(missing_ok=True)


def sovereignty_report(task_id: str, principal: str) -> dict[str, Any]:
    seed, pub = _key()
    key = {"fingerprint": signing.fingerprint(pub), "public_key": pub.hex()}
    f = _findings(task_id)
    date = time.strftime("%Y-%m-%d", time.localtime(f["run_at"]))
    path = ARTIFACT_DIR / f"sovereignty-report-{date}-{task_id[-6:]}.pdf"
    _render_pdf(f, key, path)
    pdf_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    payload = {"format": "clawcal-sovereignty-report/1", "pdf": path.name,
               "pdf_sha256": pdf_sha, "findings": f, "signed_by": principal,
               "signed_at": time.time(), **key}
    sig = signing.sign(seed, _canonical(payload))
    sidecar = path.with_name(path.name + ".sig.json")
    sidecar.write_text(json.dumps({**payload, "signature": sig.hex()}, indent=1,
                                  default=str))
    aid = _register(path, task_id, "sovereignty_report",
                    {"sidecar": sidecar.name, "fingerprint": key["fingerprint"],
                     "all_blocked": f["all_blocked"]})
    sid = _register(sidecar, task_id, "signature", {"for": path.name})
    audit.record("sovereignty", "report_signed", task_id=task_id, actor=principal,
                 detail={"pdf": path.name, "sha256": pdf_sha,
                         "fingerprint": key["fingerprint"]})
    return {"artifact_id": aid, "signature_artifact_id": sid, "pdf": path.name,
            "pdf_sha256": pdf_sha, "fingerprint": key["fingerprint"],
            "all_blocked": f["all_blocked"],
            "host_policy_applied": bool((f["nftables"] or {}).get("loaded"))}


def verify_report(pdf: Path, sidecar: Path | None = None,
                  trusted_key: str | None = None) -> dict[str, Any]:
    sidecar = sidecar or pdf.with_name(pdf.name + ".sig.json")
    if not sidecar.exists():
        return {"ok": False, "reason": f"no signature file {sidecar.name}"}
    data = json.loads(sidecar.read_text())
    sig = bytes.fromhex(data.pop("signature"))
    pub = bytes.fromhex(data["public_key"])
    if trusted_key and trusted_key.lower() != pub.hex():
        return {"ok": False, "reason": "signed by a key other than the trusted one"}
    if hashlib.sha256(pdf.read_bytes()).hexdigest() != data["pdf_sha256"]:
        return {"ok": False, "reason": "the PDF was modified after it was signed"}
    if not signing.verify(pub, _canonical(data), sig):
        return {"ok": False, "reason": "signature does not verify"}
    return {"ok": True, "fingerprint": signing.fingerprint(pub),
            "all_blocked": data["findings"].get("all_blocked"),
            "signed_by": data.get("signed_by")}


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] != "verify":
        print("usage: python -m sovereign.control.report verify <file> [trusted-key-hex]")
        return 2
    p = Path(argv[1])
    key = argv[2] if len(argv) > 2 else None
    res = (verify_audit_export(p, key) if p.suffix == ".jsonl"
           else verify_report(p, trusted_key=key))
    print(json.dumps(res, indent=1, default=str))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
