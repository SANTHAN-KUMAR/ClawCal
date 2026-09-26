"""FastAPI application: the workbench's only external surface.

Every route is a read of, or a command to, the control plane, and every route
knows who is asking: a principal resolved by `control.identity`, with a role
checked before anything happens. The API reaches the seven authorities only
through the `control` façade — never `runtime`, `router`, `policy` or
`evidence` directly (held by tests/test_control_plane.py).

Deployment shape: one process, one worker. The scheduler, the residency
manager and the event bus live in this process; a second uvicorn worker would
be a second scheduler admitting work against the same GPU. Scale-out is behind
a reverse proxy to one worker per GPU host, not workers per host.
"""
from __future__ import annotations

import json
import mimetypes
import os
import queue
import re
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               PlainTextResponse, StreamingResponse)
from fastapi.staticfiles import StaticFiles

from .. import audit, control, db, hardware
from ..config import (ARTIFACT_DIR, CORPUS_DIR, DATA_DIR, FRONTEND_DIR, UPLOAD_DIR,
                      settings)
from ..control.identity import AuthError, Forbidden, Principal
from ..control.devices import DeviceError
from ..control.sessions import SessionError
from ..drawings import analyze as drawing_analyze
from ..gateway import gateway
from ..knowledge import ingest, retrieve
from ..tools import register_all
from ..workflows import TOOLSETS
from ..workflows import register as register_runners

MAX_UPLOAD_MB = int(os.environ.get("SOVEREIGN_MAX_UPLOAD_MB", "200"))
# OCR and the VLM are CPU/GPU heavy. More concurrent ingests than this and an
# upload burst starves the agents that are already running.
_INGEST_SLOTS = threading.BoundedSemaphore(
    int(os.environ.get("SOVEREIGN_INGEST_CONCURRENCY", "2")))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _startup()
    try:
        yield
    finally:
        _shutdown()


app = FastAPI(title="ClawCal — Sovereign On-Premise AI Workbench", version="2.0.0",
              docs_url="/api/docs", openapi_url="/api/openapi.json",
              lifespan=lifespan)

# The trust-domain surface: devices, leases, admission with placement, the
# bundle store, the MCP tool server and the OpenAI-compatible /v1.
from .node import router as _node_router  # noqa: E402

app.include_router(_node_router)

_origins = [o.strip() for o in os.environ.get("SOVEREIGN_CORS_ORIGINS", "").split(",")
            if o.strip()]
app.add_middleware(CORSMiddleware,
                   allow_origin_regex=r"^http://(127\.0\.0\.1|localhost)(:\d+)?$",
                   allow_origins=_origins, allow_methods=["*"],
                   allow_headers=["*"])


@app.middleware("http")
async def no_cache(request: Request, call_next):
    """Never let a browser serve a stale workbench.

    A cached `app.js` means an operator keeps running last week's client against
    this week's control plane — observed in use after a fix, when the page still
    ran the old build and kept dropping attached files.
    """
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    # Defence in depth for a page that renders confidential documents.
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


# ------------------------------------------------------------------ errors

@app.exception_handler(AuthError)
async def _auth_error(_req: Request, exc: AuthError):
    return JSONResponse({"detail": str(exc), "auth_mode": control.identity.auth_mode()},
                        status_code=401, headers={"WWW-Authenticate": "Bearer"})


@app.exception_handler(Forbidden)
async def _forbidden(_req: Request, exc: Forbidden):
    return JSONResponse({"detail": str(exc)}, status_code=403)


@app.exception_handler(DeviceError)
async def _device_error(_req: Request, exc: DeviceError):
    msg = str(exc)
    code = 404 if msg.startswith("no device") else 409 if (
        "quarantined" in msg or "revoked" in msg or "REVOKED" in msg) else 400
    return JSONResponse({"detail": msg}, status_code=code)


@app.exception_handler(SessionError)
async def _session_error(_req: Request, exc: SessionError):
    code = 404 if str(exc).startswith("no ") else 403 if "may not" in str(exc) else 400
    return JSONResponse({"detail": str(exc)}, status_code=code)


@app.exception_handler(RuntimeError)
async def _runtime_error(_req: Request, exc: RuntimeError):
    # Matched by name so the API layer stays on the façade's side of the
    # boundary: it never imports the runtime package.
    if type(exc).__name__ == "QueueFull":
        return JSONResponse({"detail": str(exc)}, status_code=429,
                            headers={"Retry-After": "30"})
    raise exc


# ------------------------------------------------------------------ identity

# `principal`, the role dependencies and the device-aware `caller` live in
# api/deps.py, shared with the trust-domain router (api/node.py).
from .deps import admin, approver, engineer, principal, role, viewer  # noqa: E402,F401


def _task_row(task_id: str) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not row:
        raise HTTPException(404, "no such task")
    return dict(row)


def _can_see_task(task: dict[str, Any], p: Principal) -> None:
    if task["owner"] == p.name or p.can("approver"):
        return
    raise Forbidden(f"{p.name} may not see task {task['id']}, which belongs to "
                    f"{task['owner']}")


def _can_drive_task(task: dict[str, Any], p: Principal) -> None:
    if task["owner"] == p.name or p.can("admin"):
        return
    raise Forbidden(f"only {task['owner']} or an admin may control task {task['id']}")


# --------------------------------------------------------------------- lifecycle

def _startup() -> None:
    db.init_db()
    control.identity.ensure_owner()
    if control.identity.auth_mode() == "token":
        tok = control.identity.bootstrap_owner_token()
        if tok:
            print(f" Owner token written to {control.identity.owner_token_path()} "
                  f"(mode 0600). Keep it; it is shown only there.")
    control.admin.load_overrides()
    control.sovereignty.install_guard()
    register_all()
    register_runners(control.admission)
    gateway.sync_registry()
    control.perfmodel.revalidate_all()
    hardware.probe()
    if not hardware.sampler.is_alive():
        hardware.sampler.start()
    control.admission.start()
    audit.record("system", "workbench_started", detail={
        "host": settings.host, "port": settings.port,
        "auth": control.identity.auth_mode(),
        "default_mode": control.tool_policy.policy.mode,
        "models": [c.name for c in control.registry.all()]})


def _shutdown() -> None:
    control.admission.stop()
    hardware.sampler.stop()
    audit.record("system", "workbench_stopped")


# ------------------------------------------------------------------------ system

@app.get("/api/health")
def health() -> dict[str, Any]:
    """Unauthenticated liveness for a load balancer. Says nothing confidential."""
    backends = gateway.backend_health()
    up = any(b.get("status") in ("up", "degraded") for b in backends.values())
    return {"ok": True, "backend_up": up, "ts": time.time()}


@app.get("/api/whoami")
def whoami(p: Principal = Depends(principal)) -> dict[str, Any]:
    return {"principal": p.to_dict(), "auth_mode": control.identity.auth_mode()}


@app.get("/api/system")
def system_state(p: Principal = Depends(viewer)) -> dict[str, Any]:
    prof = hardware.cached_profile()
    from ..knowledge import embed
    return {
        "org": {"name": settings.org_name, "unit": settings.org_unit},
        "principal": p.to_dict(),
        "hardware": prof,
        "live": hardware.sampler.latest,
        "backends": gateway.backend_health(),
        "models": [c.to_dict() for c in control.registry.all(include_disabled=True)],
        "model_health": gateway.health_report(),
        "policy_mode": control.tool_policy.policy.mode,
        "permission_modes": list(control.tool_policy.MODES),
        "embeddings": embed.status(),
        "sovereignty": {
            "enforce": settings.sovereignty.enforce,
            "app_guard": control.sovereignty.guard_active(),
            "nftables": control.sovereignty.nftables_status(),
        },
        "workflows": sorted(TOOLSETS),
        "tools": register_all().catalogue(),
        "counts": {
            "documents": db.query_one("SELECT COUNT(*) n FROM documents")["n"],
            "chunks": db.query_one("SELECT COUNT(*) n FROM chunks")["n"],
            "tasks": db.query_one("SELECT COUNT(*) n FROM tasks")["n"],
            "artifacts": db.query_one("SELECT COUNT(*) n FROM artifacts")["n"],
            "audit_entries": db.query_one("SELECT COUNT(*) n FROM audit_log")["n"],
            "network_events": db.query_one(
                "SELECT COUNT(*) n FROM network_events")["n"],
        },
    }


@app.get("/api/telemetry")
def telemetry(_p: Principal = Depends(viewer)) -> dict[str, Any]:
    return {"latest": hardware.sampler.latest, "series": hardware.sampler.series()}


@app.get("/api/runtime")
def runtime_state(_p: Principal = Depends(viewer)) -> dict[str, Any]:
    snap = control.admission.snapshot()
    for q in snap.get("queued", []):
        try:
            q["wait"] = control.perfmodel.wait_estimate(q["id"])
        except Exception as exc:
            q["wait"] = {"basis": "unknown", "text": f"wait unknown ({exc})"[:120]}
    return snap


@app.post("/api/policy/mode")
def set_policy_mode(payload: dict[str, Any] = Body(...),
                    p: Principal = Depends(admin)) -> dict[str, Any]:
    """The default permission mode for new sessions (admin)."""
    try:
        mode = control.admin.set_default_mode(str(payload.get("mode", "")), p)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"policy_mode": mode}


# ------------------------------------------------------------------------ models

@app.get("/api/models")
def list_models(_p: Principal = Depends(viewer)) -> dict[str, Any]:
    return {"models": [c.to_dict() for c in control.registry.all(include_disabled=True)],
            "health": gateway.health_report(),
            "residency": control.residency.state()}


@app.post("/api/models/{name}/residency")
def model_residency(name: str, payload: dict[str, Any] = Body(default={}),
                    p: Principal = Depends(approver)) -> dict[str, Any]:
    card = control.registry.get(name)
    if not card:
        raise HTTPException(404, f"no model {name!r}")
    action = str(payload.get("action", "pin"))
    if action == "evict":
        return {"evicted": gateway.evict(card, reason=f"requested by {p.name}")}
    cost = gateway.pin(card, seconds=int(payload.get("seconds", 1800)))
    return {"pinned": True, "load_seconds": cost,
            "residency": control.residency.state()}


@app.post("/api/models/evict-all")
def evict_all_models(p: Principal = Depends(approver)) -> dict[str, Any]:
    """Free the GPU: unload every resident model."""
    evicted = control.residency.evict_all(reason=f"offload requested by {p.name}")
    return {"evicted": evicted, "residency": control.residency.state()}


@app.post("/api/models/sync")
def sync_models(_p: Principal = Depends(admin)) -> dict[str, Any]:
    return gateway.sync_registry()


# ------------------------------------------------------------------------- admin

@app.get("/api/admin/principals")
def admin_principals(_p: Principal = Depends(admin)) -> dict[str, Any]:
    return {"principals": control.identity.list_principals(),
            "roles": list(control.identity.ROLES)}


@app.post("/api/admin/principals")
def admin_create_principal(payload: dict[str, Any] = Body(...),
                           p: Principal = Depends(admin)) -> dict[str, Any]:
    try:
        out = control.identity.create_principal(
            str(payload.get("name", "")), role=str(payload.get("role", "engineer")),
            department=str(payload.get("department", "default")),
            display_name=str(payload.get("display_name", "")))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    audit.record("identity", "principal_created", actor=p.name,
                 detail={"name": out["name"], "role": out["role"]})
    return out          # the token is in this response and nowhere else


@app.post("/api/admin/principals/{name}")
def admin_update_principal(name: str, payload: dict[str, Any] = Body(...),
                           p: Principal = Depends(admin)) -> dict[str, Any]:
    out: dict[str, Any] = {"name": name}
    try:
        if "role" in payload:
            control.identity.set_role(name, str(payload["role"]))
            out["role"] = payload["role"]
        if "active" in payload:
            control.identity.set_active(name, bool(payload["active"]))
            out["active"] = bool(payload["active"])
        if payload.get("issue_token"):
            out["token"] = control.identity.issue_token(name)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    audit.record("identity", "principal_updated", actor=p.name,
                 detail={k: v for k, v in out.items() if k != "token"})
    return out


@app.get("/api/admin/limits")
def admin_limits(_p: Principal = Depends(admin)) -> dict[str, Any]:
    return {"limits": control.admin.limits(),
            "editable": {k: list(v) for k, v in control.admin.EDITABLE_LIMITS.items()}}


@app.post("/api/admin/limits")
def admin_set_limits(payload: dict[str, Any] = Body(...),
                     p: Principal = Depends(admin)) -> dict[str, Any]:
    try:
        return {"limits": control.admin.set_limits(payload, p)}
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/admin/models")
def admin_add_model(payload: dict[str, Any] = Body(...),
                    p: Principal = Depends(admin)) -> dict[str, Any]:
    try:
        return control.admin.add_model(payload, p)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/admin/models/{name}")
def admin_edit_model(name: str, payload: dict[str, Any] = Body(...),
                     p: Principal = Depends(admin)) -> dict[str, Any]:
    try:
        return control.admin.edit_model(name, payload, p)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/admin/egress")
def admin_egress(_p: Principal = Depends(admin)) -> dict[str, Any]:
    return {"nftables": control.sovereignty.nftables_status(),
            "app_guard": control.sovereignty.guard_active(),
            "how_to_change": "sudo ./ops/egress-policy.sh apply | persist | remove "
                             "(needs root; deliberately not exposed over HTTP)"}


# ---------------------------------------------------------------------- sessions

@app.post("/api/sessions")
def create_session(payload: dict[str, Any] = Body(default={}),
                   p: Principal = Depends(engineer)) -> dict[str, Any]:
    try:
        return control.sessions.create(p, title=str(payload.get("title") or ""),
                                       mode=payload.get("mode"))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/sessions")
def list_sessions(limit: int = Query(50, le=500),
                  p: Principal = Depends(viewer)) -> dict[str, Any]:
    return {"sessions": control.sessions.list_for(p, limit)}


@app.get("/api/sessions/{session_id}")
def get_session(session_id: str, p: Principal = Depends(viewer)) -> dict[str, Any]:
    control.sessions.check_access(session_id, p)
    return control.sessions.get(session_id)


@app.post("/api/sessions/{session_id}/mode")
def set_session_mode(session_id: str, payload: dict[str, Any] = Body(...),
                     p: Principal = Depends(engineer)) -> dict[str, Any]:
    try:
        return control.sessions.set_mode(session_id, str(payload.get("mode", "")), p)
    except ValueError as exc:
        if isinstance(exc, SessionError):
            raise
        raise HTTPException(400, str(exc))


@app.post("/api/sessions/{session_id}/documents")
def attach_document(session_id: str, payload: dict[str, Any] = Body(...),
                    p: Principal = Depends(engineer)) -> dict[str, Any]:
    control.sessions.ensure(session_id, p)
    return control.sessions.attach(session_id, str(payload.get("doc_id", "")), p)


@app.delete("/api/sessions/{session_id}/documents/{doc_id}")
def detach_document(session_id: str, doc_id: str,
                    p: Principal = Depends(engineer)) -> dict[str, Any]:
    return control.sessions.detach(session_id, doc_id, p)


@app.get("/api/sessions/{session_id}/transcript")
def session_transcript(session_id: str, format: str = "json", verbose: bool = False,
                       p: Principal = Depends(viewer)):
    control.sessions.check_access(session_id, p)
    t = control.transcript.for_session(session_id)
    if format == "text":
        return PlainTextResponse(control.transcript.render_text(t, verbose=verbose))
    return t


# ------------------------------------------------------------------------- tasks

@app.post("/api/tasks")
def create_task(payload: dict[str, Any] = Body(...),
                p: Principal = Depends(engineer)) -> dict[str, Any]:
    prompt = str(payload.get("prompt", "")).strip()
    if not prompt:
        raise HTTPException(400, "prompt is required")
    scan = control.injection.scan(prompt, source="user prompt")
    try:
        out = control.sessions.submit(
            p, prompt=prompt,
            session_id=payload.get("session_id") or payload.get("conversation_id"),
            doc_ids=payload.get("doc_ids"),
            attachments=payload.get("attachments") or [],
            workflow=str(payload.get("workflow") or "general"),
            priority=payload.get("priority"), task_type=payload.get("task_type"),
            title=str(payload.get("title") or ""), mode=payload.get("mode"),
            model=payload.get("model"))
    except ValueError as exc:
        if isinstance(exc, SessionError):
            raise
        raise HTTPException(400, str(exc))
    return {**out, "injection_scan": scan.to_dict()}


@app.get("/api/tasks")
def list_tasks(limit: int = Query(60, le=400), state: str | None = None,
               p: Principal = Depends(viewer)) -> dict[str, Any]:
    sql = ("SELECT id,title,state,state_reason,priority,effective_priority,"
           "task_type,workflow,selected_model,routing_reason,created_at,"
           "started_at,finished_at,runtime_s,queue_wait_s,steps_used,owner,"
           "conversation_id,context_used_tokens,context_budget_tokens,"
           "compacted_messages,policy_mode FROM tasks")
    where, params = [], []
    if state:
        where.append("state=?")
        params.append(state)
    if not p.can("approver"):
        # Confidential work is visible to whoever delegated it.
        where.append("owner=?")
        params.append(p.name)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    return {"tasks": db.rows_to_dicts(db.query(sql, params))}


@app.get("/api/conversations/{conversation_id}")
def get_conversation(conversation_id: str,
                     p: Principal = Depends(viewer)) -> dict[str, Any]:
    """Every turn of one conversation, oldest first. Kept for the v1 client."""
    rows = db.rows_to_dicts(db.query(
        "SELECT id,title,prompt,state,selected_model,result,created_at,owner,"
        "attachments FROM tasks WHERE conversation_id=? ORDER BY created_at",
        (conversation_id,)))
    for r in rows:
        _can_see_task(r, p)
        r["result"] = db.jload(r["result"], {})
        r["attachments"] = db.jload(r["attachments"], [])
    return {"conversation_id": conversation_id, "turns": rows}


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str, lite: bool = False,
             decisions_limit: int = Query(100, ge=0, le=5000),
             p: Principal = Depends(viewer)) -> dict[str, Any]:
    """One task. `lite=1` is for polling a live run: the task, its events and
    tool calls, without evidence, claims, calculations and decisions."""
    task = _task_row(task_id)
    _can_see_task(task, p)
    task["result"] = db.jload(task["result"], {})
    task["attachments"] = db.jload(task["attachments"], [])
    task["required_caps"] = db.jload(task["required_caps"], {})
    if task["state"] in ("QUEUED", "PAUSED"):
        task["wait"] = control.perfmodel.wait_estimate(task_id)
    if lite:
        return {
            "task": task,
            "events": db.rows_to_dicts(db.query(
                "SELECT seq,kind,label,detail,payload,ts FROM task_events "
                "WHERE task_id=? ORDER BY seq", (task_id,))),
            "tool_calls": db.rows_to_dicts(db.query(
                "SELECT id,step,tool,decision,ok,duration_s,created_at,outcome,"
                "outcome_reason FROM tool_calls WHERE task_id=? ORDER BY created_at",
                (task_id,))),
            "lite": True,
        }
    all_decisions = control.decisions.for_task(task_id)
    return {
        "task": task,
        "events": db.rows_to_dicts(db.query(
            "SELECT seq,kind,label,detail,payload,ts FROM task_events "
            "WHERE task_id=? ORDER BY seq", (task_id,))),
        "tool_calls": db.rows_to_dicts(db.query(
            "SELECT id,step,tool,args,decision,reason,ok,duration_s,created_at,"
            "outcome,outcome_reason,principal FROM tool_calls WHERE task_id=? "
            "ORDER BY created_at", (task_id,))),
        "claims": db.rows_to_dicts(db.query(
            "SELECT * FROM claims WHERE task_id=? ORDER BY created_at", (task_id,))),
        "calculations": db.rows_to_dicts(db.query(
            "SELECT * FROM calculations WHERE task_id=? ORDER BY created_at",
            (task_id,))),
        "evidence": db.rows_to_dicts(db.query(
            "SELECT * FROM evidence WHERE task_id=? ORDER BY created_at",
            (task_id,))),
        "artifacts": db.rows_to_dicts(db.query(
            "SELECT * FROM artifacts WHERE task_id=? ORDER BY created_at",
            (task_id,))),
        "checkpoints": db.rows_to_dicts(db.query(
            "SELECT id,step,reason,ts FROM checkpoints WHERE task_id=? "
            "ORDER BY step", (task_id,))),
        "network_events": db.rows_to_dicts(db.query(
            "SELECT * FROM network_events WHERE task_id=? ORDER BY ts DESC",
            (task_id,))),
        "decisions": all_decisions[-decisions_limit:] if decisions_limit else [],
        "decisions_total": len(all_decisions),
    }


@app.post("/api/tasks/{task_id}/{action}")
def task_action(task_id: str, action: str,
                payload: dict[str, Any] = Body(default={}),
                p: Principal = Depends(engineer)) -> dict[str, Any]:
    task = _task_row(task_id)
    _can_drive_task(task, p)
    reason = str(payload.get("reason") or f"{action} requested by {p.name}")
    if action == "pause":
        return {"ok": control.admission.pause(task_id, reason)}
    if action == "resume":
        return {"ok": control.admission.resume(task_id)}
    if action == "cancel":
        return {"ok": control.admission.cancel(task_id, reason)}
    raise HTTPException(400, f"unknown action {action!r}")


# --------------------------------------------------------------------- documents

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._() -]+")


@app.post("/api/upload")
async def upload(file: UploadFile, doc_class: str | None = None,
                 ingest_now: bool = True, session_id: str | None = None,
                 p: Principal = Depends(engineer)) -> dict[str, Any]:
    """Store and index a document.

    The body is streamed to disk with a size ceiling rather than read into
    memory, and ingestion — OCR, the vision model, embedding — runs in a worker
    thread. It used to run on the event loop, which froze every other request
    and the live stream for as long as a scan took to read.
    """
    name = _SAFE_NAME.sub("_", Path(file.filename or "file").name)[:180] or "file"
    dest = UPLOAD_DIR / f"{int(time.time() * 1000)}-{name}"
    limit = MAX_UPLOAD_MB * 1024 * 1024
    written = 0
    with dest.open("wb") as fh:
        while True:
            chunk = await file.read(1 << 20)
            if not chunk:
                break
            written += len(chunk)
            if written > limit:
                fh.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(413, f"file exceeds the {MAX_UPLOAD_MB} MB "
                                         f"upload limit (SOVEREIGN_MAX_UPLOAD_MB)")
            fh.write(chunk)
    if written == 0:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, "the uploaded file is empty")
    audit.record("knowledge", "uploaded", actor=p.name,
                 detail={"name": name, "bytes": written})
    if not ingest_now:
        return {"path": str(dest), "ingested": False}

    def work() -> dict[str, Any]:
        with _INGEST_SLOTS:
            return ingest.ingest_file(dest, title=file.filename, doc_class=doc_class,
                                      copy=False)
    try:
        result = await run_in_threadpool(work)
    except Exception as exc:
        raise HTTPException(400, f"ingestion failed: {exc}")
    if session_id and result.get("doc_id"):
        control.sessions.ensure(session_id, p)
        result["working_set"] = control.sessions.attach(session_id, result["doc_id"], p)
    return {"ingested": True, **result}


@app.get("/api/documents")
def list_documents(_p: Principal = Depends(viewer)) -> dict[str, Any]:
    rows = db.rows_to_dicts(db.query(
        "SELECT id,title,kind,doc_class,sha256,pages,bytes,status,status_detail,"
        "meta,created_at FROM documents ORDER BY created_at DESC"))
    for r in rows:
        r["meta"] = db.jload(r["meta"], {})
    return {"documents": rows}


@app.get("/api/documents/{doc_id}/pages")
def document_pages(doc_id: str, _p: Principal = Depends(viewer)) -> dict[str, Any]:
    rows = db.rows_to_dicts(db.query(
        "SELECT page_no,width,height,extractor,ocr_conf,image_path,text "
        "FROM pages WHERE doc_id=? ORDER BY page_no", (doc_id,)))
    for r in rows:
        r["has_image"] = bool(r["image_path"] and Path(r["image_path"]).exists())
        r.pop("image_path", None)
    return {"doc_id": doc_id, "pages": rows}


@app.get("/api/documents/{doc_id}/page/{page_no}/image")
def page_image(doc_id: str, page_no: int,
               _p: Principal = Depends(viewer)) -> FileResponse:
    row = db.query_one("SELECT image_path FROM pages WHERE doc_id=? AND page_no=?",
                       (doc_id, page_no))
    if not row or not row["image_path"] or not Path(row["image_path"]).exists():
        raise HTTPException(404, "no rendered image for this page")
    return FileResponse(row["image_path"])


@app.post("/api/search")
def search(payload: dict[str, Any] = Body(...),
           _p: Principal = Depends(viewer)) -> dict[str, Any]:
    q = str(payload.get("query", "")).strip()
    if not q:
        raise HTTPException(400, "query is required")
    return retrieve.evidence_package(
        q, k=max(1, min(int(payload.get("k", 8)), 50)),
        doc_classes=payload.get("doc_classes"),
        doc_ids=payload.get("doc_ids"),
        rerank=bool(payload.get("rerank")))


# ---------------------------------------------------------------------- drawings

@app.get("/api/drawings")
def list_drawings(_p: Principal = Depends(viewer)) -> dict[str, Any]:
    rows = db.rows_to_dicts(db.query(
        "SELECT id,title,source_kind,width,height,status,summary,created_at "
        "FROM drawings ORDER BY created_at DESC"))
    for r in rows:
        r["summary"] = db.jload(r["summary"], {})
    return {"drawings": rows}


@app.get("/api/drawings/{drawing_id}")
def get_drawing(drawing_id: str, _p: Principal = Depends(viewer)) -> dict[str, Any]:
    d = drawing_analyze.load(drawing_id)
    if not d:
        raise HTTPException(404, "no such drawing")
    d.pop("image_path", None)
    return d


@app.get("/api/drawings/{drawing_id}/image")
def drawing_image(drawing_id: str, _p: Principal = Depends(viewer)) -> FileResponse:
    row = db.query_one("SELECT image_path FROM drawings WHERE id=?", (drawing_id,))
    if not row or not row["image_path"] or not Path(row["image_path"]).exists():
        raise HTTPException(404, "no image")
    return FileResponse(row["image_path"])


def _allowed_path(raw: str) -> Path:
    """A drawing path is resolved inside the corpus or the appliance's data only.

    This route used to open any path the caller named — a way to make the
    appliance read, and render into the evidence store, any file its account
    could see.
    """
    p = Path(raw)
    if not p.is_absolute():
        p = (CORPUS_DIR.parent / p)
    p = p.resolve()
    for root in (CORPUS_DIR.resolve(), UPLOAD_DIR.resolve(), DATA_DIR.resolve()):
        try:
            p.relative_to(root)
            return p
        except ValueError:
            continue
    raise HTTPException(403, "drawings are analysed from the corpus or from uploaded "
                             "documents only; upload the file first")


@app.post("/api/drawings/analyse")
async def analyse_drawing(payload: dict[str, Any] = Body(...),
                          _p: Principal = Depends(engineer)) -> dict[str, Any]:
    path = _allowed_path(str(payload.get("path", "")))
    if not path.is_file():
        raise HTTPException(400, f"no such file: {path.name}")
    use_vlm = bool(payload.get("use_vlm", True))

    def work() -> dict[str, Any]:
        suf = path.suffix.lower()
        if suf in (".dxf", ".dwg"):
            a = drawing_analyze.analyse_cad(path, title=path.stem)
        elif suf == ".pdf":
            a = drawing_analyze.analyse_pdf(path, title=path.stem, use_vlm=use_vlm)
        else:
            a = drawing_analyze.analyse_image(path, title=path.stem, use_vlm=use_vlm)
        return a.to_dict()
    return await run_in_threadpool(work)


# ------------------------------------------------------------- evidence & audit

@app.get("/api/artifacts")
def list_artifacts(p: Principal = Depends(viewer)) -> dict[str, Any]:
    sql = ("SELECT a.* FROM artifacts a LEFT JOIN tasks t ON t.id = a.task_id")
    params: list[Any] = []
    if not p.can("approver"):
        sql += " WHERE t.owner=? OR t.owner IS NULL"
        params.append(p.name)
    rows = db.rows_to_dicts(db.query(sql + " ORDER BY a.created_at DESC LIMIT 200",
                                     params))
    for r in rows:
        r["meta"] = db.jload(r["meta"], {})
        r["exists"] = Path(r["path"]).exists()
        r.pop("path", None)
    return {"artifacts": rows}


@app.get("/api/artifacts/{artifact_id}/download")
def download_artifact(artifact_id: str, p: Principal = Depends(viewer)) -> FileResponse:
    row = db.query_one("SELECT * FROM artifacts WHERE id=?", (artifact_id,))
    if not row or not Path(row["path"]).exists():
        raise HTTPException(404, "artifact not found")
    if row["task_id"]:
        t = db.query_one("SELECT id, owner FROM tasks WHERE id=?", (row["task_id"],))
        if t:
            _can_see_task(dict(t), p)
    audit.record("artifact", "downloaded", task_id=row["task_id"], actor=p.name,
                 detail={"artifact": row["name"]})
    mime = mimetypes.guess_type(row["name"])[0] or "application/octet-stream"
    return FileResponse(row["path"], filename=row["name"], media_type=mime)


@app.get("/api/audit")
def audit_log(limit: int = Query(200, le=2000), category: str | None = None,
              _p: Principal = Depends(approver)) -> dict[str, Any]:
    sql = "SELECT * FROM audit_log"
    params: list[Any] = []
    if category:
        sql += " WHERE category=?"
        params.append(category)
    sql += " ORDER BY seq DESC LIMIT ?"
    params.append(limit)
    return {"entries": db.rows_to_dicts(db.query(sql, params)),
            "verification": _verification()}


_VERIFY_CACHE: dict[str, Any] = {}


def _verification(max_age_s: float = 10.0) -> dict[str, Any]:
    """Chain verification, cached briefly: it is O(n) over the whole log.

    The strip polls it; recomputing a year of history every few seconds per
    client is how a sovereignty indicator becomes a denial of service.
    """
    now = time.time()
    if _VERIFY_CACHE and now - _VERIFY_CACHE["ts"] < max_age_s:
        return _VERIFY_CACHE["v"]
    v = {"audit": audit.verify_chain(), "decisions": control.decisions.verify()}
    v["ok"] = bool(v["audit"].get("ok") and v["decisions"].get("ok"))
    _VERIFY_CACHE.update(ts=now, v=v)
    return v


@app.get("/api/audit/verify")
def audit_verify(_p: Principal = Depends(viewer)) -> dict[str, Any]:
    v = _verification(max_age_s=0)
    return {**v["audit"], "decisions": v["decisions"], "ok": v["ok"]}


@app.get("/api/audit/export")
def audit_export(p: Principal = Depends(approver)) -> FileResponse:
    """The whole audit chain plus decisions as JSONL, signed, verifiable offline."""
    path = control.report.export_audit(p.name)
    return FileResponse(str(path), filename=path.name,
                        media_type="application/x-ndjson")


@app.get("/api/decisions")
def list_decisions(task_id: str | None = None, session_id: str | None = None,
                   authority: str | None = None, limit: int = Query(200, le=2000),
                   p: Principal = Depends(viewer)) -> dict[str, Any]:
    if task_id:
        _can_see_task(_task_row(task_id), p)
        return {"decisions": control.decisions.for_task(task_id)}
    if session_id:
        control.sessions.check_access(session_id, p)
        return {"decisions": control.decisions.for_session(session_id)}
    p.require("approver", "listing decisions across all tasks")
    return {"decisions": control.decisions.recent(limit, authority)}


@app.get("/api/network")
def network_events(_p: Principal = Depends(viewer)) -> dict[str, Any]:
    return control.sovereignty.status()


@app.get("/api/sovereignty")
def sovereignty_strip(_p: Principal = Depends(viewer)) -> dict[str, Any]:
    """Everything the always-visible sovereignty strip shows, in one call."""
    nft = control.sovereignty.nftables_status()
    v = _verification()
    denials = db.query_one("SELECT COUNT(*) AS n FROM network_events "
                           "WHERE result IN ('DENIED','BLOCKED')")["n"]
    last = db.query_one("SELECT ts FROM network_events ORDER BY ts DESC LIMIT 1")
    backends = gateway.backend_health()
    selftest = db.query_one(
        "SELECT outcome, ts, task_id, basis FROM decisions WHERE authority="
        "'sovereignty' AND subject_kind='selftest' ORDER BY seq DESC LIMIT 1")
    return {
        "app_guard": control.sovereignty.guard_active(),
        "enforce": settings.sovereignty.enforce,
        "nftables": nft,
        "host_policy": ("applied" if nft.get("loaded") else
                        "unreadable" if nft.get("loaded") is None else "not_applied"),
        "denials": denials, "last_denial_ts": last["ts"] if last else None,
        "audit": {"ok": v["ok"], "entries": v["audit"].get("entries"),
                  "head": audit.head(), "reason": v["audit"].get("reason")
                  or v["decisions"].get("reason")},
        "backends": {k: b.get("status") for k, b in backends.items()},
        "last_selftest": dict(selftest) if selftest else None,
        # The trust domain's member devices: how many, at which grades, and
        # whether any is quarantined after a tamper event (§11 roster).
        "devices": {
            "domain": control.trust.domain_name(),
            "by_grade": {r["grade"] or "?": r["n"] for r in db.query(
                "SELECT grade, COUNT(*) AS n FROM devices WHERE state='ACTIVE' "
                "GROUP BY grade")},
            "quarantined": db.query_one("SELECT COUNT(*) AS n FROM devices "
                                        "WHERE state='QUARANTINED'")["n"],
            "open_tamper_events": db.query_one(
                "SELECT COUNT(*) AS n FROM tamper_events t JOIN devices d ON "
                "d.id = t.device_id WHERE t.state='OPEN' AND d.state != 'REVOKED'")["n"]},
        "state": ("green" if (control.sovereignty.guard_active() and v["ok"]
                              and nft.get("loaded")) else
                  "amber" if (control.sovereignty.guard_active() and v["ok"])
                  else "red"),
    }


@app.post("/api/sovereignty/selftest")
def sovereignty_selftest(p: Principal = Depends(engineer)) -> dict[str, Any]:
    out = control.sessions.submit(
        p, title="Sovereignty self-test",
        prompt="Attempt outbound network access from every layer and record the "
               "result.",
        workflow="sovereignty_proof", priority="HIGH")
    return {"task_id": out["task_id"], "session_id": out["session_id"]}


@app.post("/api/sovereignty/report/{task_id}")
def sovereignty_report(task_id: str, p: Principal = Depends(engineer)) -> dict[str, Any]:
    """Produce the signed sovereignty report for a finished self-test."""
    task = _task_row(task_id)
    _can_see_task(task, p)
    if task["workflow"] != "sovereignty_proof" or task["state"] != "COMPLETED":
        raise HTTPException(400, "the report is produced from a completed "
                                 "sovereignty self-test")
    return control.report.sovereignty_report(task_id, p.name)


@app.get("/api/approvals")
def approvals(p: Principal = Depends(viewer)) -> dict[str, Any]:
    pending = control.tool_policy.pending_approvals()
    recent = db.rows_to_dicts(db.query(
        "SELECT a.*, t.owner FROM approvals a LEFT JOIN tasks t ON t.id=a.task_id "
        "ORDER BY a.created_at DESC LIMIT 50"))
    if not p.can("approver"):
        pending = [a for a in pending if _owner_of(a.get("task_id")) == p.name]
        recent = [a for a in recent if a.get("owner") == p.name]
    return {"pending": pending, "recent": recent, "can_decide": p.can("approver")}


def _owner_of(task_id: str | None) -> str | None:
    if not task_id:
        return None
    row = db.query_one("SELECT owner FROM tasks WHERE id=?", (task_id,))
    return row["owner"] if row else None


@app.post("/api/approvals/{approval_id}")
def decide(approval_id: str, payload: dict[str, Any] = Body(...),
           p: Principal = Depends(approver)) -> dict[str, Any]:
    try:
        ok = control.tool_policy.decide_approval(
            approval_id, bool(payload.get("approve")), by=p.name)
    except control.tool_policy.ApprovalRefused as exc:
        raise HTTPException(403, str(exc))
    if not ok:
        raise HTTPException(409, "approval is not pending (already decided, "
                                 "expired or cancelled)")
    return {"ok": True, "state": control.tool_policy.approval_state(approval_id),
            "decided_by": p.name}


@app.get("/api/benchmarks")
def benchmarks(_p: Principal = Depends(viewer)) -> dict[str, Any]:
    rows = db.rows_to_dicts(db.query(
        "SELECT * FROM benchmarks ORDER BY ts DESC LIMIT 50"))
    for r in rows:
        r["payload"] = db.jload(r["payload"], {})
    return {"benchmarks": rows}


# ------------------------------------------------------------------------ stream

MAX_STREAMS = int(os.environ.get("SOVEREIGN_MAX_STREAMS", "64"))


@app.get("/api/stream")
async def stream(request: Request, p: Principal = Depends(viewer)) -> StreamingResponse:
    """Server-sent events: task state, trace entries, admissions, denials.

    A principal below `approver` only receives events about its own tasks.
    The generator is async and polls its queue without blocking, so a hundred
    open browser tabs cost a hundred small coroutines, not a hundred threads.
    """
    if audit.bus.subscriber_count >= MAX_STREAMS:
        raise HTTPException(503, "too many live streams are open")
    q = audit.bus.subscribe()
    see_all = p.can("approver")
    owners: dict[str, str | None] = {}

    def visible(ev: dict[str, Any]) -> bool:
        if see_all:
            return True
        tid = ev.get("task_id")
        if not tid:
            return ev.get("type") in ("hello", "network_event", "audit")
        if tid not in owners:
            if len(owners) > 2048:
                owners.clear()
            owners[tid] = _owner_of(tid)
        return owners[tid] == p.name

    async def gen():
        import asyncio
        try:
            yield f"data: {json.dumps({'type': 'hello', 'ts': time.time()})}\n\n"
            last_ping = time.time()
            while True:
                if await request.is_disconnected():
                    break
                sent = False
                for _ in range(64):
                    try:
                        ev = q.get_nowait()
                    except queue.Empty:
                        break
                    if visible(ev):
                        yield f"data: {json.dumps(ev, default=str)}\n\n"
                        sent = True
                if not sent:
                    if time.time() - last_ping > 15:
                        last_ping = time.time()
                        yield ": keepalive\n\n"
                    await asyncio.sleep(0.25)
        finally:
            audit.bus.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ---------------------------------------------------------------------- frontend

if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
def index() -> Any:
    idx = FRONTEND_DIR / "index.html"
    if idx.exists():
        return FileResponse(str(idx))
    return HTMLResponse("<h1>ClawCal</h1><p>Frontend not built.</p>")
