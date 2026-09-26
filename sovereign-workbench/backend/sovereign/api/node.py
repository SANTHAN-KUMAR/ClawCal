"""The trust-domain surface of the node (sovereign-workbench-v2.md).

Everything a member device talks to, behind the node's one port:

    /api/trust/*           the domain: node identity, policy, root of trust
    /api/devices/*         enrolment, reports, manifests, log sync, roster
    /api/lease/*           renewal (signed by the device key)
    /api/admit             B1 with placement: classify, place, pin — once
    /api/bundles/*         the TUF-style bundle store
    /mcp                   the MCP tool server (retrieve · stage ·
                           execute_remote · deliver …)
    /v1/*                  OpenAI-compatible inference, pinned per plan
    /evidence/{span}       a span's page with its region outlined — what a
                           figure in a deliverable links to

Like app.py, this router reaches the authorities only through the `control`
façade.
"""
from __future__ import annotations

import base64
import html
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, Response,
                               StreamingResponse)

from .. import control, db
from ..control.identity import Forbidden, Principal
from .deps import Caller, admin, approver, caller, engineer, node_url, viewer

router = APIRouter()


def _own_device(device_id: str, p: Principal) -> dict[str, Any]:
    dev = control.devices.get(device_id)
    if dev["principal"] != p.name and not p.can("approver"):
        raise Forbidden(f"{device_id} is enrolled to {dev['principal']}")
    return dev


# ------------------------------------------------------------------- domain

@router.get("/api/trust/domain")
def trust_domain(c: Caller = Depends(caller)) -> dict[str, Any]:
    """What a device needs to know about the domain it is joining."""
    import time
    root = control.bundles.repo.trusted_root()
    return {"domain": control.trust.domain_name(),
            "node": control.leases.node_identity(),
            "tuf_root": {"version": root["version"],
                         "fingerprint": control.bundles.repo.root_fingerprint()},
            "policy": control.trust.policy(),
            "you": {"principal": c.principal.name, "grade": c.grade,
                    "clearance": control.trust.clearance(c.grade),
                    "device": c.device.to_dict() if c.device else None,
                    "console": c.console},
            "node_time": time.time()}


@router.get("/api/trust/health")
def trust_health(_p: Principal = Depends(viewer)) -> dict[str, Any]:
    return control.trust.health()


@router.get("/api/admin/trust/policy")
def get_policy(_p: Principal = Depends(admin)) -> dict[str, Any]:
    return control.trust.policy()


@router.post("/api/admin/trust/policy")
def set_policy(payload: dict[str, Any] = Body(...),
               p: Principal = Depends(admin)) -> dict[str, Any]:
    try:
        return control.trust.set_policy(payload, p)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@router.post("/api/documents/{doc_id}/data-class")
def classify_document(doc_id: str, payload: dict[str, Any] = Body(...),
                      p: Principal = Depends(approver)) -> dict[str, Any]:
    try:
        control.trust.set_data_class(doc_id, str(payload.get("data_class", "")), p)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return {"doc_id": doc_id, "data_class": payload.get("data_class")}


# ------------------------------------------------------------------ devices

@router.post("/api/devices/enrol")
def enrol(request: Request, payload: dict[str, Any] = Body(...),
          p: Principal = Depends(engineer)) -> dict[str, Any]:
    """Enrol a device: the envelope is signed by the key being enrolled. The
    response carries the node's identity and the repository's root, which the
    device pins, and — if the device is active — its first attached lease."""
    dev = control.devices.enrol(p, payload)
    lease = None
    if dev["state"] == "ACTIVE":
        try:
            lease = control.leases.issue(dev["id"], mode="attached", by=p.name)
        except control.devices.DeviceError as exc:
            lease = {"refused": str(exc)}
    return {"device": dev, "lease": lease,
            "node": control.leases.node_identity(),
            "tuf_root": control.bundles.repo.metadata("root.json"),
            "node_url": node_url(request)}


@router.get("/api/devices")
def roster(p: Principal = Depends(viewer)) -> dict[str, Any]:
    rows = control.devices.roster(p)
    by_grade: dict[str, int] = {}
    for r in rows:
        if r["state"] == "ACTIVE":
            by_grade[r.get("grade") or "?"] = by_grade.get(r.get("grade") or "?", 0) + 1
    return {"devices": rows, "by_grade": by_grade,
            "open_tamper_events": sum(len(r["tamper_events"]) for r in rows
                                      if r["state"] != "REVOKED"),
            "domain": control.trust.domain_name()}


@router.get("/api/devices/{device_id}")
def device(device_id: str, p: Principal = Depends(viewer)) -> dict[str, Any]:
    _own_device(device_id, p)
    d = control.devices.describe(device_id)
    d["lease"] = control.leases.current(device_id)
    d["anchor"] = control.anchors.anchor(device_id)
    d["tamper_events"] = db.rows_to_dicts(db.query(
        "SELECT * FROM tamper_events WHERE device_id=? ORDER BY ts DESC",
        (device_id,)))
    return d


@router.post("/api/devices/{device_id}/report")
def device_report(device_id: str, payload: dict[str, Any] = Body(...),
                  p: Principal = Depends(engineer)) -> dict[str, Any]:
    _own_device(device_id, p)
    return control.devices.report_state(device_id, payload)


@router.post("/api/devices/{device_id}/approve")
def approve_device(device_id: str, p: Principal = Depends(admin)) -> dict[str, Any]:
    return control.devices.approve(device_id, p)


@router.post("/api/devices/{device_id}/managed")
def device_managed(device_id: str, payload: dict[str, Any] = Body(...),
                   p: Principal = Depends(admin)) -> dict[str, Any]:
    return control.devices.set_managed(
        device_id, bool(payload.get("managed", True)), p,
        mdm_attested=bool(payload.get("mdm_attested")),
        note=str(payload.get("note", ""))[:400])


@router.post("/api/devices/{device_id}/attestation/verify")
def verify_attestation(device_id: str, p: Principal = Depends(admin)) -> dict[str, Any]:
    return control.devices.verify_attestation(device_id, p)


@router.post("/api/devices/{device_id}/attest/begin")
def attest_begin(device_id: str, payload: dict[str, Any] = Body(...),
                 p: Principal = Depends(engineer)) -> dict[str, Any]:
    _own_device(device_id, p)
    return control.attestation.begin(device_id, payload)


@router.post("/api/devices/{device_id}/attest/finish")
def attest_finish(device_id: str, payload: dict[str, Any] = Body(...),
                  p: Principal = Depends(engineer)) -> dict[str, Any]:
    _own_device(device_id, p)
    return control.attestation.finish(device_id, payload)


@router.post("/api/devices/{device_id}/attest/reset-baseline")
def attest_reset(device_id: str, payload: dict[str, Any] = Body(...),
                 p: Principal = Depends(admin)) -> dict[str, Any]:
    return control.attestation.reset_baseline(device_id, p,
                                              str(payload.get("reason", "")))


@router.post("/api/admin/tpm-roots")
def tpm_root(payload: dict[str, Any] = Body(...),
             p: Principal = Depends(admin)) -> dict[str, Any]:
    return control.attestation.add_root(str(payload.get("pem", "")),
                                        str(payload.get("name", "root")), p)


@router.post("/api/devices/{device_id}/revoke")
def revoke_device(device_id: str, payload: dict[str, Any] = Body(default={}),
                  p: Principal = Depends(admin)) -> dict[str, Any]:
    return control.devices.revoke(device_id, p, str(payload.get("reason", ""))[:400])


@router.post("/api/devices/{device_id}/manifest")
def device_manifest(device_id: str, request: Request,
                    payload: dict[str, Any] = Body(default={}),
                    p: Principal = Depends(engineer)) -> dict[str, Any]:
    _own_device(device_id, p)
    mode = str(payload.get("mode") or "attached")
    if mode not in ("attached", "detached"):
        raise HTTPException(400, "mode must be attached or detached")
    try:
        return control.bundles.issue_manifest(
            device_id, mode=mode, principal=p, model_id=payload.get("model"),
            node_url=node_url(request).split("://", 1)[-1])
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/api/devices/{device_id}/log")
def device_log_sync(device_id: str, payload: dict[str, Any] = Body(...),
                    p: Principal = Depends(engineer)) -> Any:
    _own_device(device_id, p)
    out = control.anchors.sync(device_id, payload)
    if not out.get("ok"):
        return JSONResponse(out, status_code=409)
    return out


@router.get("/api/devices/{device_id}/log")
def device_log(device_id: str, limit: int = 200,
               p: Principal = Depends(viewer)) -> dict[str, Any]:
    _own_device(device_id, p)
    return {"anchor": control.anchors.anchor(device_id),
            "entries": control.anchors.log_for(device_id, min(limit, 2000))}


@router.post("/api/tamper/{event_id}/clear")
def clear_tamper(event_id: str, payload: dict[str, Any] = Body(...),
                 p: Principal = Depends(admin)) -> dict[str, Any]:
    return control.anchors.clear(event_id, p, str(payload.get("reason", "")),
                                 rebase=bool(payload.get("rebase")))


@router.post("/api/lease/renew")
def renew_lease(payload: dict[str, Any] = Body(...),
                p: Principal = Depends(engineer)) -> dict[str, Any]:
    """Renewal needs the device key, not the old lease: an expired lease is
    exactly the case renewal exists for."""
    device_id = str(payload.get("device_id", ""))
    _own_device(device_id, p)
    return control.leases.renew(device_id, payload)


@router.get("/api/lease")
def lease_state(c: Caller = Depends(caller)) -> dict[str, Any]:
    if c.device is None:
        raise HTTPException(400, "this request carries no device lease")
    return {"device": c.device.to_dict(),
            "lease": control.leases.current(c.device.device_id)}


# ------------------------------------------------------------------ slices

@router.post("/api/devices/{device_id}/slices")
def slice_export(device_id: str, payload: dict[str, Any] = Body(...),
                 p: Principal = Depends(approver)) -> dict[str, Any]:
    try:
        return control.bundles.slices.export(device_id,
                                             list(payload.get("doc_ids") or []), p)
    except control.bundles.slices.SliceError as exc:
        raise HTTPException(409, str(exc)) from None


@router.get("/api/devices/{device_id}/slices")
def slice_list(device_id: str, p: Principal = Depends(viewer)) -> dict[str, Any]:
    _own_device(device_id, p)
    return {"slices": control.bundles.slices.for_device(device_id)}


@router.get("/api/slices/{slice_id}")
def slice_fetch(slice_id: str, c: Caller = Depends(caller)) -> dict[str, Any]:
    if c.device is None:
        raise HTTPException(403, "a slice is fetched only by the device it was "
                                 "exported to, with its lease")
    try:
        return control.bundles.slices.fetch(slice_id, c.device.device_id)
    except control.bundles.slices.SliceError as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/api/slices/{slice_id}/deliver")
def slice_deliver(slice_id: str, request: Request, payload: dict[str, Any] = Body(...),
                  p: Principal = Depends(engineer)) -> dict[str, Any]:
    try:
        return control.bundles.slices.deliver(slice_id, p, payload, node_url(request))
    except control.bundles.slices.SliceError as exc:
        raise HTTPException(409, str(exc)) from None


# ------------------------------------------------------------------- admit

@router.post("/api/admit")
def admit(payload: dict[str, Any] = Body(...),
          c: Caller = Depends(caller)) -> dict[str, Any]:
    c.principal.require("engineer", "admitting a plan")
    try:
        return control.placement.admit(
            c.principal, c.device, prompt=str(payload.get("prompt", "")),
            attachments=payload.get("attachments") or [],
            session_id=payload.get("session_id") or c.session_id,
            mode=payload.get("mode"), console=c.console)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@router.get("/api/admit/{task_id}")
def plan_progress(task_id: str, c: Caller = Depends(caller)) -> dict[str, Any]:
    row = control.placement.progress(task_id)
    if not row or (row["owner"] != c.principal.name and not c.principal.can("approver")):
        raise HTTPException(404, "no such plan")
    return row


@router.post("/api/admit/{task_id}/close")
def close_plan(task_id: str, payload: dict[str, Any] = Body(default={}),
               c: Caller = Depends(caller)) -> dict[str, Any]:
    row = db.query_one("SELECT owner, state FROM tasks WHERE id=?", (task_id,))
    if not row or (row["owner"] != c.principal.name and not c.principal.can("admin")):
        raise HTTPException(404, "no such plan")
    control.placement.close(task_id, "COMPLETED",
                            str(payload.get("reason") or "closed by the client"))
    return {"task_id": task_id, "state": "COMPLETED"}


# ----------------------------------------------------------------- bundles

@router.get("/api/bundles/metadata/{name}")
def bundle_metadata(name: str, _p: Principal = Depends(viewer)) -> dict[str, Any]:
    env = control.bundles.repo.metadata(name)
    if env is None:
        raise HTTPException(404, f"no metadata {name!r}")
    return env


@router.get("/api/bundles/targets/{path:path}")
def bundle_target(path: str, p: Principal = Depends(viewer)) -> FileResponse:
    if path.startswith("devices/"):
        _own_device(path.split("/")[1], p)
    found = control.bundles.repo.target_file(path)
    if not found:
        raise HTTPException(404, f"no target {path!r}")
    f, info = found
    return FileResponse(str(f), headers={
        "X-Target-SHA256": info["hashes"]["sha256"],
        "X-Target-Length": str(info["length"])})


@router.get("/api/admin/bundles/catalogue")
def bundle_catalogue(_p: Principal = Depends(admin)) -> dict[str, Any]:
    node = control.profiler.node_profile()
    cat = control.bundles.manifest.catalogue()
    plan = control.bundles.manifest.plan(node)
    return {"catalogue": cat, "node_profile": node, "node_classes": plan["classes"],
            "targets": sorted(control.bundles.repo.targets())}


@router.post("/api/admin/bundles/weights")
def bundle_add_weights(payload: dict[str, Any] = Body(...),
                       _p: Principal = Depends(admin)) -> dict[str, Any]:
    try:
        return control.bundles.manifest.add_weights(
            str(payload["path"]), str(payload["model_id"]),
            internet_url=str(payload.get("internet_url", "")),
            registry_model=str(payload.get("registry_model", "")))
    except (KeyError, OSError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from None


@router.post("/api/admin/bundles/engine")
def bundle_add_engine(payload: dict[str, Any] = Body(...),
                      _p: Principal = Depends(admin)) -> dict[str, Any]:
    try:
        return control.bundles.manifest.add_engine(
            str(payload["path"]), engine=str(payload.get("engine", "llama.cpp")),
            platform=str(payload.get("platform", "linux")),
            variant=str(payload.get("variant", "default")),
            binary=str(payload["binary"]))
    except (KeyError, OSError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from None


@router.post("/api/admin/bundles/harness")
def bundle_add_harness(payload: dict[str, Any] = Body(...),
                       _p: Principal = Depends(admin)) -> dict[str, Any]:
    try:
        return control.bundles.manifest.add_harness(
            str(payload["path"]), platform=str(payload.get("platform", "linux")),
            version=str(payload.get("version", "clawcal")))
    except (KeyError, OSError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from None


@router.post("/api/admin/bundles/client")
def bundle_publish_client(p: Principal = Depends(admin)) -> dict[str, Any]:
    return control.bundles.publish_client(by=p.name)


# --------------------------------------------------------------------- MCP

@router.post("/mcp")
async def mcp_post(request: Request, c: Caller = Depends(caller)) -> Response:
    from starlette.concurrency import run_in_threadpool
    from .. import toolserver
    try:
        body = json.loads(await request.body() or b"null")
    except ValueError:
        return JSONResponse({"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32700, "message": "parse error"}},
                            status_code=400)
    sid = request.headers.get("mcp-session-id")
    batch = isinstance(body, list)
    messages = body if batch else [body]
    replies, new_sid = [], None
    for m in messages:
        # Tool calls block (sandbox runs, human approvals): off the event loop.
        reply, opened = await run_in_threadpool(
            toolserver.handle, m, mcp_session=sid or new_sid,
            principal=c.principal, device=c.device,
            clawcal_session=c.session_id, node_url=c.node_url)
        new_sid = new_sid or opened
        if reply is not None:
            replies.append(reply)
    headers = {"Mcp-Session-Id": new_sid} if new_sid else {}
    if not replies:
        return Response(status_code=202, headers=headers)
    if not new_sid and sid and toolserver.sessions.get(sid) is None and any(
            (r.get("error") or {}).get("code") == -32001 and "unknown" in
            (r.get("error") or {}).get("message", "") for r in replies):
        return JSONResponse(replies if batch else replies[0], status_code=404)
    return JSONResponse(replies if batch else replies[0], headers=headers)


@router.get("/mcp")
def mcp_get(_c: Caller = Depends(caller)) -> Response:
    # This server never pushes, so it offers no server-to-client stream.
    return Response(status_code=405, headers={"Allow": "POST, DELETE"})


@router.delete("/mcp")
def mcp_delete(request: Request, c: Caller = Depends(caller)) -> Response:
    from .. import toolserver
    sid = request.headers.get("mcp-session-id") or ""
    return Response(status_code=200 if toolserver.end(sid, c.principal) else 404)


# ----------------------------------------------------------------------- /v1

@router.get("/v1/models")
def v1_models(c: Caller = Depends(caller)) -> dict[str, Any]:
    from .. import inference
    return inference.models_for(c.principal)


@router.post("/v1/chat/completions")
def v1_chat(payload: dict[str, Any] = Body(...), c: Caller = Depends(caller)) -> Any:
    from .. import inference
    c.principal.require("engineer", "running inference on the node")
    try:
        res, _model, _meta = inference.complete(payload, principal=c.principal,
                                                device=c.device,
                                                session_id=c.session_id)
    except inference.InferenceError as exc:
        return JSONResponse(exc.body(), status_code=exc.status)
    alias = str(payload.get("model") or "generalist")
    if payload.get("stream"):
        usage = bool((payload.get("stream_options") or {}).get("include_usage"))
        return StreamingResponse(inference.stream_chunks(res, alias, usage),
                                 media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store"})
    return inference.response_body(res, alias)


# ------------------------------------------------------------------ evidence

def _span_for(span_id: str, c: Caller) -> dict[str, Any]:
    s = control.served.span(span_id)
    if not s:
        raise HTTPException(404, "no such span")
    cls = s.get("data_class") or "internal"
    if cls not in control.trust.clearance(c.grade):
        raise Forbidden(f"this span is from a {cls!r} document; grade {c.grade} is "
                        f"not cleared for it")
    return s


@router.get("/api/spans/{span_id}")
def span_json(span_id: str, c: Caller = Depends(caller)) -> dict[str, Any]:
    s = _span_for(span_id, c)
    return {k: s.get(k) for k in ("span_id", "doc_id", "doc_title", "page_no",
                                  "region", "text", "data_class")}


@router.get("/evidence/{span_id}", response_class=HTMLResponse)
def evidence_page(span_id: str, c: Caller = Depends(caller)) -> HTMLResponse:
    """The page a span came from, with its region outlined. Self-contained (the
    image is inline), so a link in a .docx needs one authenticated request."""
    s = _span_for(span_id, c)
    page = db.query_one("SELECT image_path, width, height FROM pages WHERE doc_id=? "
                        "AND page_no=?", (s["doc_id"], s["page_no"])) \
        if s.get("doc_id") else None
    img, box = "", ""
    if page and page["image_path"] and Path(page["image_path"]).exists():
        data = base64.b64encode(Path(page["image_path"]).read_bytes()).decode()
        w, h = float(page["width"] or 595), float(page["height"] or 842)
        r = s.get("region") or {}
        if all(k in r for k in ("x0", "y0", "x1", "y1")):
            box = (f'<rect x="{r["x0"]}" y="{r["y0"]}" width="{r["x1"] - r["x0"]}" '
                   f'height="{r["y1"] - r["y0"]}" class="hl"/>')
        img = (f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="page '
               f'{s["page_no"]}"><image href="data:image/png;base64,{data}" '
               f'width="{w}" height="{h}"/>{box}</svg>')
    else:
        img = '<p class="none">No rendered image is held for this page.</p>'
    title = html.escape(f"{s.get('doc_title') or s.get('doc_id')} — page "
                        f"{s.get('page_no')}")
    body = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Evidence {html.escape(span_id)}</title><style>
:root{{--bg:#f6f7f9;--fg:#15202b;--mute:#5b6570;--card:#fff;--hl:#e8590c}}
@media (prefers-color-scheme:dark){{:root{{--bg:#11161c;--fg:#e6e9ec;--mute:#9aa4ae;--card:#1a2129}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}}
main{{max-width:900px;margin:0 auto;padding:20px 16px}}
h1{{font-size:18px;margin:0 0 4px}} .meta{{color:var(--mute);font-size:13px}}
blockquote{{background:var(--card);border-left:3px solid var(--hl);margin:16px 0;
padding:10px 14px;white-space:pre-wrap}}
svg{{width:100%;height:auto;background:#fff;box-shadow:0 1px 4px #0003}}
.hl{{fill:rgba(232,89,12,.14);stroke:var(--hl);stroke-width:2}}
</style></head><body><main><h1>{title}</h1>
<div class="meta">span {html.escape(span_id)} · class
{html.escape(str(s.get('data_class') or 'internal'))} · served by the trust-domain
node</div><blockquote>{html.escape((s.get('text') or '')[:1500])}</blockquote>
{img}</main></body></html>"""
    return HTMLResponse(body, headers={"Cache-Control": "no-store"})
