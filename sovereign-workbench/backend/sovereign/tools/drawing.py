"""Engineering-drawing tools exposed to the agent."""
from __future__ import annotations

from pathlib import Path
from typing import Any


from .. import db
from ..drawings import analyze, graph
from ..drawings.model import CONFIRMED, PROBABLE
from ..policy.tool_policy import Risk
from .base import Tool, ToolContext, ToolResult


def _latest_drawing(task_id: str) -> str | None:
    row = db.query_one("SELECT id FROM drawings ORDER BY created_at DESC LIMIT 1")
    return row["id"] if row else None


def _resolve_drawing_ref(ref: str) -> str | None:
    """Accept an internal id or the drawing's own number, e.g. 'PID-204-01'."""
    if not ref:
        return None
    row = db.query_one("SELECT id FROM drawings WHERE id=?", (str(ref),))
    if row:
        return row["id"]
    row = db.query_one("SELECT id FROM drawings WHERE title LIKE ? "
                       "ORDER BY created_at DESC LIMIT 1", (f"%{ref}%",))
    return row["id"] if row else None


def _resolve_path(raw: str) -> Path | None:
    """Resolve a caller-supplied drawing path.

    A model quotes the path the way it appeared in the task text, which is
    relative to the deployment root rather than to the process working
    directory. Trying the obvious bases is the difference between the tool
    working and the agent looping on a file-not-found.
    """
    from ..config import CORPUS_DIR, REPO_ROOT, UPLOAD_DIR

    p = Path(str(raw)).expanduser()
    candidates = [p] if p.is_absolute() else [
        Path.cwd() / p, REPO_ROOT / p, CORPUS_DIR / p,
        CORPUS_DIR / "drawings" / p.name, UPLOAD_DIR / p.name,
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


class AnalyseDrawingTool(Tool):
    name = "analyse_drawing"
    risk = Risk.READ_ONLY
    timeout_s = 600.0
    description = (
        "Analyse an engineering drawing (P&ID, isometric, GA) attached to this "
        "task. Returns the equipment and instrument tags found, and the "
        "connectivity between them with an explicit confidence status per "
        "connection: CONFIRMED, PROBABLE or UNRESOLVED. Never state a PROBABLE "
        "or UNRESOLVED connection as fact.")
    parameters = {"type": "object", "properties": {
        "path": {"type": "string",
                 "description": "drawing file path; defaults to the attachment"},
        "drawing_id": {"type": "string",
                       "description": "re-read an already analysed drawing"},
    }}

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        # A previously analysed drawing, if the reference resolves. If it does
        # not, fall through to `path` rather than failing: a model that supplies
        # both a valid path and a guessed id should not be blocked by the guess.
        if args.get("drawing_id"):
            did = _resolve_drawing_ref(str(args["drawing_id"]))
            if did:
                loaded = analyze.load(did)
                if loaded:
                    return ToolResult(True, content=loaded,
                                      display=f"loaded drawing {loaded['title']}")

        path = args.get("path") or ctx.scratch.get("drawing_path")
        if not path:
            attachments = ctx.scratch.get("attachments", [])
            drawing = next((a for a in attachments if a.get("kind") == "drawing"),
                           None)
            if drawing:
                path = drawing.get("path")
        if not path:
            known = db.query("SELECT title FROM drawings ORDER BY created_at DESC "
                             "LIMIT 10")
            return ToolResult(
                False,
                error="no drawing was attached to this task and no path was "
                      "supplied. Previously analysed drawings: "
                      + (", ".join(r["title"] for r in known) or "none"))

        p = _resolve_path(str(path))
        if p is None:
            return ToolResult(
                False,
                error=f"drawing file not found at {path!r}. Give a path relative "
                      f"to the deployment root, such as "
                      f"'corpus/drawings/PID-204-01.pdf', or attach the drawing "
                      f"to the task.")
        try:
            if p.suffix.lower() == ".pdf":
                a = analyze.analyse_pdf(p, title=p.stem, task_id=ctx.task_id)
            else:
                a = analyze.analyse_image(p, title=p.stem, task_id=ctx.task_id)
        except Exception as exc:
            return ToolResult(False, error=f"drawing analysis failed: {exc}")

        ctx.scratch["drawing_id"] = a.drawing_id
        ctx.note("drawing", f"Analysed {a.title}",
                 f"{len(a.symbols)} symbols, {len(a.edges)} traced lines, "
                 f"{a.summary()['confirmed_connectivity']} confirmed connections",
                 a.to_dict())
        return ToolResult(True, content=a.connectivity_report(),
                          display=f"{len(a.symbols)} symbols, "
                                  f"{a.summary()['confirmed_connectivity']} "
                                  f"confirmed connections",
                          meta={"drawing_id": a.drawing_id,
                                "summary": a.summary()})


class TraceConnectionTool(Tool):
    name = "trace_drawing_connection"
    risk = Risk.READ_ONLY
    timeout_s = 120.0
    description = ("Check whether two tags on an analysed drawing are connected, "
                   "and by what path. Returns CANNOT DETERMINE when the drawing "
                   "does not establish a connection.")
    parameters = {"type": "object", "properties": {
        "from_tag": {"type": "string"},
        "to_tag": {"type": "string"},
        "drawing_id": {"type": "string"},
    }, "required": ["from_tag", "to_tag"]}

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        did = (args.get("drawing_id") or ctx.scratch.get("drawing_id")
               or _latest_drawing(ctx.task_id))
        if not did:
            return ToolResult(False, error="no drawing has been analysed yet; "
                                           "call analyse_drawing first")
        loaded = analyze.load(str(did))
        if not loaded:
            return ToolResult(False, error=f"no analysed drawing {did!r}")

        from ..drawings.model import BBox, Edge, Symbol
        symbols = [Symbol(id=s["id"].split(":")[-1], sym_class=s["sym_class"],
                          bbox=BBox(*s["bbox"]), confidence=s["confidence"],
                          method=s["method"], tag=s["tag"] or "")
                   for s in loaded["symbols"]]
        edges = [Edge(id=e["id"].split(":")[-1], src=e["src"], dst=e["dst"],
                      line_type=e["line_type"],
                      polyline=[tuple(p) for p in e["polyline"]],
                      confidence=e["confidence"], status=e["status"],
                      rationale=e["rationale"] or "")
                 for e in loaded["edges"]]
        result = graph.trace_path(symbols, edges, str(args["from_tag"]).upper(),
                                  str(args["to_tag"]).upper())
        ctx.note("drawing", f"Traced {args['from_tag']} -> {args['to_tag']}",
                 result.get("status", ""), result)
        return ToolResult(True, content=result,
                          display=f"{args['from_tag']} -> {args['to_tag']}: "
                                  f"{result.get('status')}")
