"""Workspace file tools.

Agents get a per-task workspace and nothing else. Every path is resolved and
checked against the workspace root *after* symlink resolution, so `../` and
symlink escapes both fail closed.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import db
from ..config import WORKSPACE_DIR
from ..policy.tool_policy import Risk
from .base import Tool, ToolContext, ToolResult

MAX_READ_BYTES = 200_000
MAX_WRITE_BYTES = 4_000_000


class WorkspaceEscape(ValueError):
    pass


# What to tell a caller that reached for the host filesystem. Confinement is
# deliberate, but a refusal that does not say where the documents actually are
# leaves the agent with nowhere to go -- observed in the field, an agent asked
# for /home/user/Downloads, got back "workspace is empty", and concluded the
# file did not exist.
_ELSEWHERE = (
    "Agents cannot read the host filesystem; only this task's own workspace. "
    "Documents the organisation has indexed are reached with `list_documents`, "
    "`search_knowledge` and `read_document_page` instead of by path. If the file "
    "you want is not indexed, ask the operator to upload it through the "
    "workbench."
)


def resolve(workspace: Path, rel: str) -> Path:
    """Resolve a caller-supplied path inside the workspace, or refuse.

    An absolute path is refused explicitly rather than being quietly reinterpreted
    as workspace-relative: silently turning /home/user/Downloads into
    <workspace>/home/user/Downloads produces a confusing empty result instead of
    an answerable error.
    """
    root = workspace.resolve()
    root.mkdir(parents=True, exist_ok=True)

    raw = str(rel)
    if raw.startswith("/") or raw.startswith("~"):
        raise WorkspaceEscape(
            f"{raw!r} is an absolute host path. {_ELSEWHERE}")

    candidate = (root / raw).resolve()
    if candidate != root and root not in candidate.parents:
        raise WorkspaceEscape(
            f"path {raw!r} resolves outside the task workspace and was refused. "
            f"{_ELSEWHERE}")
    return candidate


def _workspace(ctx: ToolContext) -> Path:
    return Path(ctx.workspace) if ctx.workspace else (WORKSPACE_DIR / ctx.task_id)


class ListFilesTool(Tool):
    name = "list_files"
    risk = Risk.READ_ONLY
    description = ("List files in this task's workspace. Returns relative paths "
                   "with sizes.")
    parameters = {"type": "object", "properties": {
        "subdir": {"type": "string", "description": "optional subdirectory"}}}

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        ws = _workspace(ctx)
        try:
            base = resolve(ws, args.get("subdir") or ".")
        except WorkspaceEscape as exc:
            return ToolResult(False, error=str(exc))
        if not base.exists():
            return ToolResult(
                False,
                error=f"no such directory {args.get('subdir') or '.'!r} in this "
                      f"task's workspace. {_ELSEWHERE}")
        entries = [
            {"path": str(p.relative_to(ws)), "bytes": p.stat().st_size,
             "kind": "dir" if p.is_dir() else "file"}
            for p in sorted(base.rglob("*")) if p.name != "_sandbox_init.py"
        ][:400]
        if not entries:
            # An empty workspace is a legitimate state, but on its own it tells
            # the agent nothing about where the documents are.
            docs = db.query("SELECT title FROM documents WHERE status='READY' "
                            "ORDER BY created_at DESC LIMIT 12")
            listing = ", ".join(d["title"] for d in docs) or "none"
            return ToolResult(
                True,
                content=(f"This task's workspace is empty -- nothing has been "
                         f"written to it yet. {_ELSEWHERE}\n\n"
                         f"Indexed documents: {listing}"),
                display="workspace is empty; indexed documents listed instead")
        return ToolResult(True, content=entries,
                          display=f"{len(entries)} entries in the workspace")


class ReadFileTool(Tool):
    name = "read_file"
    risk = Risk.READ_ONLY
    description = ("Read a text file from this task's workspace. Cannot read "
                   "anywhere else on the host.")
    parameters = {"type": "object", "properties": {
        "path": {"type": "string", "description": "path relative to the workspace"},
        "max_bytes": {"type": "integer"}}, "required": ["path"]}

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        ws = _workspace(ctx)
        try:
            p = resolve(ws, str(args.get("path", "")))
        except WorkspaceEscape as exc:
            return ToolResult(False, error=str(exc))
        if not p.is_file():
            return ToolResult(
                False,
                error=f"no such file in this task's workspace: "
                      f"{args.get('path')!r}. {_ELSEWHERE}")
        limit = min(int(args.get("max_bytes") or MAX_READ_BYTES), MAX_READ_BYTES)
        data = p.read_bytes()[:limit]
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return ToolResult(False, error="file is not UTF-8 text")
        ctx.note("file_read", f"Read {p.name}", f"{len(data)} bytes")
        return ToolResult(True, content=text,
                          display=f"read {p.name} ({len(data)} bytes)")


class WriteFileTool(Tool):
    name = "write_file"
    risk = Risk.LOCAL_WRITE
    description = ("Write a text file into this task's workspace. Use it to save "
                   "code, notes or intermediate results.")
    parameters = {"type": "object", "properties": {
        "path": {"type": "string", "description": "path relative to the workspace"},
        "content": {"type": "string"},
        "append": {"type": "boolean"}}, "required": ["path", "content"]}

    def approval_summary(self, args: dict[str, Any]) -> str:
        return (f"write {len(str(args.get('content', '')))} bytes to workspace "
                f"file {args.get('path')!r}")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        ws = _workspace(ctx)
        try:
            p = resolve(ws, str(args.get("path", "")))
        except WorkspaceEscape as exc:
            return ToolResult(False, error=str(exc))
        content = str(args.get("content", ""))
        if len(content.encode()) > MAX_WRITE_BYTES:
            return ToolResult(False, error=f"content exceeds the "
                                           f"{MAX_WRITE_BYTES} byte write limit")
        p.parent.mkdir(parents=True, exist_ok=True)
        if args.get("append") and p.exists():
            p.write_text(p.read_text(errors="replace") + content)
        else:
            p.write_text(content)
        ctx.note("file_write", f"Wrote {p.name}", f"{len(content)} chars")
        return ToolResult(True, content={"path": str(p.relative_to(ws)),
                                         "bytes": len(content)},
                          display=f"wrote {p.relative_to(ws)}")
