"""FastAPI application: the workbench's only external surface.

Bound to loopback by default. Every route is a read of, or a command to, the
control plane -- there is no path here that reaches a model or a tool without
going through the scheduler and the policy engine first.
"""
from __future__ import annotations

import json
import mimetypes
import queue
import time
from pathlib import Path
from typing import Any

from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles

from .. import audit, db, hardware
from ..config import ARTIFACT_DIR, FRONTEND_DIR, UPLOAD_DIR, settings
from ..drawings import analyze as drawing_analyze
from ..evidence import provenance
from ..gateway import gateway
from ..gateway.registry import registry
from ..knowledge import ingest, retrieve
from ..policy import egress, injection
from ..policy.tool_policy import policy
from ..policy import tool_policy
from ..runtime import residency, scheduler
from ..tools import register_all
from ..tools.sandbox import egress_probe
from ..workflows import TOOLSETS
from ..workflows import register as register_runners

@asynccontextmanager
async def lifespan(_app: FastAPI):
    _startup()
    try:
        yield
    finally:
        _shutdown()


app = FastAPI(title="Sovereign On-Premise AI Workbench", version="1.0.0",
              docs_url="/api/docs", openapi_url="/api/openapi.json",
              lifespan=lifespan)

app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:*",
                                                  "http://localhost:*"],
                   allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def no_cache(request: Request, call_next):
    """Never let a browser serve a stale workbench.

    The UI ships inside the appliance and changes when the appliance is updated.
    A cached `app.js` means an operator keeps running last week's client against
    this week's control plane, and the symptom is silent and baffling: a button
    that no longer does what its label says. Observed in use after a fix — the
    page still ran the old build and kept dropping attached files.
    """
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


# --------------------------------------------------------------------- lifecycle

def _startup() -> None:
    db.init_db()
    egress.install_guard()
    register_all()
    register_runners(scheduler)
    gateway.sync_registry()
    hardware.probe()
    if not hardware.sampler.is_alive():
        hardware.sampler.start()
    scheduler.start()
    audit.record("system", "workbench_started", detail={
        "host": settings.host, "port": settings.port,
        "policy_mode": policy.mode,
        "models": [c.name for c in registry.all()]})


def _shutdown() -> None:
    scheduler.stop()
    hardware.sampler.stop()
    audit.record("system", "workbench_stopped")


# ------------------------------------------------------------------------ system

@app.get("/api/system")
def system_state() -> dict[str, Any]:
    prof = hardware.cached_profile()
    return {
        "org": {"name": settings.org_name, "unit": settings.org_unit},
        "hardware": prof,
        "live": hardware.sampler.latest,
        "backends": gateway.backend_health(),
        "models": [c.to_dict() for c in registry.all(include_disabled=True)],
        "model_health": gateway.health_report(),
        "policy_mode": policy.mode,
        "embeddings": __import__("sovereign.knowledge.embed",
                                 fromlist=["status"]).status(),
        "sovereignty": {
            "enforce": settings.sovereignty.enforce,
            "app_guard": egress.guard_active(),
            "nftables": egress.nftables_status(),
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
def telemetry() -> dict[str, Any]:
    return {"latest": hardware.sampler.latest, "series": hardware.sampler.series()}


@app.get("/api/runtime")
def runtime_state() -> dict[str, Any]:
    return scheduler.snapshot()


@app.post("/api/policy/mode")
def set_policy_mode(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    mode = str(payload.get("mode", ""))
    try:
        policy.set_mode(mode)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"policy_mode": policy.mode}


# ------------------------------------------------------------------------ models

@app.get("/api/models")
def list_models() -> dict[str, Any]:
    return {"models": [c.to_dict() for c in registry.all(include_disabled=True)],
            "health": gateway.health_report(),
            "residency": residency.state()}


@app.post("/api/models/{name}/residency")
def model_residency(name: str, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    card = registry.get(name)
    if not card:
        raise HTTPException(404, f"no model {name!r}")
    action = str(payload.get("action", "pin"))
    if action == "evict":
        return {"evicted": gateway.evict(card, reason="operator request")}
    cost = gateway.pin(card, seconds=int(payload.get("seconds", 1800)))
    return {"pinned": True, "load_seconds": cost, "residency": residency.state()}


@app.post("/api/models/sync")
def sync_models() -> dict[str, Any]:
    return gateway.sync_registry()


# ------------------------------------------------------------------------- tasks

@app.post("/api/tasks")
def create_task(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    prompt = str(payload.get("prompt", "")).strip()
    if not prompt:
        raise HTTPException(400, "prompt is required")

    attachments = payload.get("attachments") or []
    scan = injection.scan(prompt, source="user prompt")
    tid = scheduler.submit(
        title=str(payload.get("title") or prompt[:90]),
        prompt=prompt,
        workflow=str(payload.get("workflow") or "general"),
        owner=str(payload.get("owner") or "operator"),
        department=str(payload.get("department") or "default"),
        priority=payload.get("priority"),
        attachments=attachments,
        task_type=payload.get("task_type"),
    )
    return {"task_id": tid, "injection_scan": scan.to_dict()}


@app.get("/api/tasks")
def list_tasks(limit: int = Query(60, le=400),
               state: str | None = None) -> dict[str, Any]:
    sql = ("SELECT id,title,state,state_reason,priority,effective_priority,"
           "task_type,workflow,selected_model,routing_reason,created_at,"
           "started_at,finished_at,runtime_s,queue_wait_s,steps_used,owner "
           "FROM tasks")
    params: list[Any] = []
    if state:
        sql += " WHERE state=?"
        params.append(state)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    return {"tasks": db.rows_to_dicts(db.query(sql, params))}


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not row:
        raise HTTPException(404, "no such task")
    task = dict(row)
    task["result"] = db.jload(task["result"], {})
    task["attachments"] = db.jload(task["attachments"], [])
    task["required_caps"] = db.jload(task["required_caps"], {})
    return {
        "task": task,
        "events": db.rows_to_dicts(db.query(
            "SELECT seq,kind,label,detail,payload,ts FROM task_events "
            "WHERE task_id=? ORDER BY seq", (task_id,))),
        "tool_calls": db.rows_to_dicts(db.query(
            "SELECT id,step,tool,args,decision,reason,ok,duration_s,created_at "
            "FROM tool_calls WHERE task_id=? ORDER BY created_at", (task_id,))),
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
    }


@app.post("/api/tasks/{task_id}/{action}")
def task_action(task_id: str, action: str,
                payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    reason = str(payload.get("reason") or f"{action} requested by operator")
    if action == "pause":
        return {"ok": scheduler.pause(task_id, reason)}
    if action == "resume":
        return {"ok": scheduler.resume(task_id)}
    if action == "cancel":
        return {"ok": scheduler.cancel(task_id, reason)}
    raise HTTPException(400, f"unknown action {action!r}")


# --------------------------------------------------------------------- documents

@app.post("/api/upload")
async def upload(file: UploadFile, doc_class: str | None = None,
                 ingest_now: bool = True) -> dict[str, Any]:
    dest = UPLOAD_DIR / f"{int(time.time() * 1000)}-{Path(file.filename or 'file').name}"
    dest.write_bytes(await file.read())
    if not ingest_now:
        return {"path": str(dest), "ingested": False}
    try:
        result = ingest.ingest_file(dest, title=file.filename, doc_class=doc_class,
                                    copy=False)
    except Exception as exc:
        raise HTTPException(400, f"ingestion failed: {exc}")
    return {"ingested": True, **result}


@app.get("/api/documents")
def list_documents() -> dict[str, Any]:
    rows = db.rows_to_dicts(db.query(
        "SELECT * FROM documents ORDER BY created_at DESC"))
    for r in rows:
        r["meta"] = db.jload(r["meta"], {})
    return {"documents": rows}


@app.get("/api/documents/{doc_id}/pages")
def document_pages(doc_id: str) -> dict[str, Any]:
    rows = db.rows_to_dicts(db.query(
        "SELECT page_no,width,height,extractor,ocr_conf,image_path,text "
        "FROM pages WHERE doc_id=? ORDER BY page_no", (doc_id,)))
    for r in rows:
        r["has_image"] = bool(r["image_path"] and Path(r["image_path"]).exists())
        r.pop("image_path", None)
    return {"doc_id": doc_id, "pages": rows}


@app.get("/api/documents/{doc_id}/page/{page_no}/image")
def page_image(doc_id: str, page_no: int) -> FileResponse:
    row = db.query_one("SELECT image_path FROM pages WHERE doc_id=? AND page_no=?",
                       (doc_id, page_no))
    if not row or not row["image_path"] or not Path(row["image_path"]).exists():
        raise HTTPException(404, "no rendered image for this page")
    return FileResponse(row["image_path"])


@app.post("/api/search")
def search(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    q = str(payload.get("query", "")).strip()
    if not q:
        raise HTTPException(400, "query is required")
    return retrieve.evidence_package(
        q, k=int(payload.get("k", 8)),
        doc_classes=payload.get("doc_classes"),
        doc_ids=payload.get("doc_ids"),
        rerank=bool(payload.get("rerank")))


# ---------------------------------------------------------------------- drawings

@app.get("/api/drawings")
def list_drawings() -> dict[str, Any]:
    rows = db.rows_to_dicts(db.query(
        "SELECT id,title,source_kind,width,height,status,summary,created_at "
        "FROM drawings ORDER BY created_at DESC"))
    for r in rows:
        r["summary"] = db.jload(r["summary"], {})
    return {"drawings": rows}


@app.get("/api/drawings/{drawing_id}")
def get_drawing(drawing_id: str) -> dict[str, Any]:
    d = drawing_analyze.load(drawing_id)
    if not d:
        raise HTTPException(404, "no such drawing")
    d.pop("image_path", None)
    return d


@app.get("/api/drawings/{drawing_id}/image")
def drawing_image(drawing_id: str) -> FileResponse:
    row = db.query_one("SELECT image_path FROM drawings WHERE id=?", (drawing_id,))
    if not row or not Path(row["image_path"]).exists():
        raise HTTPException(404, "no image")
    return FileResponse(row["image_path"])


@app.post("/api/drawings/analyse")
def analyse_drawing(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    path = Path(str(payload.get("path", "")))
    if not path.is_file():
        raise HTTPException(400, f"no such file: {path}")
    use_vlm = bool(payload.get("use_vlm", True))
    if path.suffix.lower() == ".pdf":
        a = drawing_analyze.analyse_pdf(path, title=path.stem, use_vlm=use_vlm)
    else:
        a = drawing_analyze.analyse_image(path, title=path.stem, use_vlm=use_vlm)
    return a.to_dict()


# ------------------------------------------------------------- evidence & audit

@app.get("/api/artifacts")
def list_artifacts() -> dict[str, Any]:
    rows = db.rows_to_dicts(db.query(
        "SELECT * FROM artifacts ORDER BY created_at DESC LIMIT 200"))
    for r in rows:
        r["meta"] = db.jload(r["meta"], {})
        r["exists"] = Path(r["path"]).exists()
    return {"artifacts": rows}


@app.get("/api/artifacts/{artifact_id}/download")
def download_artifact(artifact_id: str) -> FileResponse:
    row = db.query_one("SELECT * FROM artifacts WHERE id=?", (artifact_id,))
    if not row or not Path(row["path"]).exists():
        raise HTTPException(404, "artifact not found")
    audit.record("artifact", "downloaded", task_id=row["task_id"],
                 detail={"artifact": row["name"]})
    mime = mimetypes.guess_type(row["name"])[0] or "application/octet-stream"
    return FileResponse(row["path"], filename=row["name"], media_type=mime)


@app.get("/api/audit")
def audit_log(limit: int = Query(200, le=2000),
              category: str | None = None) -> dict[str, Any]:
    sql = "SELECT * FROM audit_log"
    params: list[Any] = []
    if category:
        sql += " WHERE category=?"
        params.append(category)
    sql += " ORDER BY seq DESC LIMIT ?"
    params.append(limit)
    return {"entries": db.rows_to_dicts(db.query(sql, params)),
            "verification": audit.verify_chain()}


@app.get("/api/audit/verify")
def audit_verify() -> dict[str, Any]:
    return audit.verify_chain()


@app.get("/api/network")
def network_events() -> dict[str, Any]:
    return egress.status()


@app.post("/api/sovereignty/selftest")
def sovereignty_selftest() -> dict[str, Any]:
    tid = scheduler.submit(
        title="Sovereignty self-test",
        prompt="Attempt outbound network access from every layer and record the "
               "result.",
        workflow="sovereignty_proof", priority="HIGH")
    return {"task_id": tid}


@app.get("/api/approvals")
def approvals() -> dict[str, Any]:
    return {"pending": tool_policy.pending_approvals(),
            "recent": db.rows_to_dicts(db.query(
                "SELECT * FROM approvals ORDER BY created_at DESC LIMIT 50"))}


@app.post("/api/approvals/{approval_id}")
def decide(approval_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    ok = tool_policy.decide_approval(approval_id, bool(payload.get("approve")),
                                     by=str(payload.get("by", "operator")))
    if not ok:
        raise HTTPException(400, "approval is not pending")
    return {"ok": True, "state": tool_policy.approval_state(approval_id)}


@app.get("/api/benchmarks")
def benchmarks() -> dict[str, Any]:
    rows = db.rows_to_dicts(db.query(
        "SELECT * FROM benchmarks ORDER BY ts DESC LIMIT 50"))
    for r in rows:
        r["payload"] = db.jload(r["payload"], {})
    return {"benchmarks": rows}


# ------------------------------------------------------------------------ stream

@app.get("/api/stream")
def stream(request: Request) -> StreamingResponse:
    """Server-sent events: task state, trace entries, admissions, denials."""
    q = audit.bus.subscribe()

    def gen():
        try:
            yield f"data: {json.dumps({'type': 'hello', 'ts': time.time()})}\n\n"
            last_ping = time.time()
            while True:
                try:
                    ev = q.get(timeout=2.0)
                    yield f"data: {json.dumps(ev, default=str)}\n\n"
                except queue.Empty:
                    if time.time() - last_ping > 15:
                        last_ping = time.time()
                        yield ": keepalive\n\n"
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
    return HTMLResponse("<h1>Sovereign Workbench</h1><p>Frontend not built.</p>")
