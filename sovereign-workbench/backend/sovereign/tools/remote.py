"""The node's tools for a client-side agent loop (spec §4, §12, §13).

Attached mode moves the agent loop to the user's machine; the provenance-
critical and isolation-critical work stays here and is reached as tools:

    stage_files      copy workspace files into this session's staging area —
                     explicit copies, never a mount of anything real
    execute_remote   run a command over a fresh copy of the staged files in the
                     node's sandbox (no network, no host filesystem, wall-clock
                     deadline); changed files come back in the result
    deliver          gate the client's claims against the spans the node served
                     to this session, then write the .docx on the organisation's
                     template, every figure linked to its page and region

They are ordinary gateway tools: the policy engine decides each call, the
session's permission mode applies, and every call is a tool_calls row, a
decision and an audit entry — exactly as for the node's own harness.
"""
from __future__ import annotations

import base64
import fnmatch
import hashlib
import shutil
import time
from pathlib import Path, PurePosixPath
from typing import Any

from .. import audit, db
from ..config import DATA_DIR, WORKSPACE_DIR, settings
from ..policy.tool_policy import Risk
from .base import Tool, ToolContext, ToolResult
from .sandbox import run_code

STAGING_DIR = DATA_DIR / "staging"
MAX_FILES = 2000
RETURN_FILE_BYTES = 64_000
RETURN_FILES = 24


def _session(ctx: ToolContext) -> str:
    if ctx.session_id:
        return ctx.session_id
    row = db.query_one("SELECT conversation_id FROM tasks WHERE id=?", (ctx.task_id,))
    return (row["conversation_id"] if row else None) or ctx.task_id


def staging_dir(session_id: str) -> Path:
    safe = "".join(ch for ch in session_id if ch.isalnum() or ch in "-_")
    return STAGING_DIR / (safe or "anon")


def safe_rel(path: str) -> str:
    """A relative POSIX path confined to the staging root, or ValueError."""
    raw = str(path or "").replace("\\", "/").strip()
    if not raw or raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
        raise ValueError(f"{path!r}: staged paths must be relative")
    p = PurePosixPath(raw)
    if any(part in ("..", "") for part in p.parts) or p.parts[0] == ".":
        raise ValueError(f"{path!r} escapes the staging area")
    if len(p.parts) > 24 or len(raw) > 400:
        raise ValueError(f"{path!r} is too deep or too long")
    return p.as_posix()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _snapshot(root: Path) -> dict[str, str]:
    out = {}
    for f in root.rglob("*"):
        if f.is_file() and not f.is_symlink():
            out[f.relative_to(root).as_posix()] = _sha(f.read_bytes())
    return out


class StageFilesTool(Tool):
    name = "stage_files"
    risk = Risk.LOCAL_WRITE
    timeout_s = 120.0
    description = (
        "Copy files from your local workspace to this session's staging area on "
        "the node, so execute_remote can run against them. Send text as "
        "`content` or binary as `content_b64`. Paths are relative. Staging is an "
        "explicit copy: nothing on your machine is mounted.")
    parameters = {"type": "object", "properties": {
        "files": {"type": "array", "items": {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"},
            "content_b64": {"type": "string"}}, "required": ["path"]}},
        "delete": {"type": "array", "items": {"type": "string"},
                   "description": "staged paths to remove"},
        "reset": {"type": "boolean",
                  "description": "empty the staging area first"},
    }}

    def approval_summary(self, args: dict[str, Any]) -> str:
        names = [str(f.get("path")) for f in args.get("files") or []][:12]
        return f"stage {len(args.get('files') or [])} file(s) on the node: {names}"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        sid = _session(ctx)
        root = staging_dir(sid)
        if args.get("reset") and root.exists():
            shutil.rmtree(root)
            db.execute("DELETE FROM staged_files WHERE session_id=?", (sid,))
        root.mkdir(parents=True, exist_ok=True)
        max_file = settings.staging_max_file_mb * 1_000_000
        max_total = settings.staging_max_session_mb * 1_000_000
        written, errors = [], []
        for rel in args.get("delete") or []:
            try:
                r = safe_rel(rel)
            except ValueError as exc:
                errors.append(str(exc))
                continue
            (root / r).unlink(missing_ok=True)
            db.execute("DELETE FROM staged_files WHERE session_id=? AND path=?",
                       (sid, r))
        for f in args.get("files") or []:
            try:
                rel = safe_rel(f.get("path", ""))
                if "content_b64" in f:
                    data = base64.b64decode(str(f["content_b64"]), validate=True)
                else:
                    data = str(f.get("content", "")).encode("utf-8")
            except (ValueError, TypeError) as exc:
                errors.append(str(exc)[:200])
                continue
            if len(data) > max_file:
                errors.append(f"{rel}: {len(data)} bytes exceeds the "
                              f"{settings.staging_max_file_mb} MB per-file limit")
                continue
            total = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
            if total + len(data) > max_total:
                errors.append(f"{rel}: the session's staging area would exceed "
                              f"{settings.staging_max_session_mb} MB")
                continue
            if sum(1 for _ in root.rglob("*")) >= MAX_FILES:
                errors.append(f"{rel}: more than {MAX_FILES} staged files")
                continue
            dest = root / rel
            if not dest.resolve().is_relative_to(root.resolve()):
                errors.append(f"{rel} escapes the staging area")
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            _upsert_staged(sid, rel, data, ctx)
            written.append({"path": rel, "bytes": len(data), "sha256": _sha(data)})
        listing = db.rows_to_dicts(db.query(
            "SELECT path, bytes, sha256 FROM staged_files WHERE session_id=? "
            "ORDER BY path", (sid,)))
        ctx.note("staging", f"Staged {len(written)} file(s)",
                 f"{len(listing)} file(s) in the session's staging area",
                 {"written": written, "errors": errors})
        res = ToolResult(bool(written) or not errors,
                         content={"staged": written, "errors": errors,
                                  "staging_area": listing},
                         display=f"{len(written)} staged, {len(errors)} refused",
                         error="; ".join(errors)[:500] if errors and not written else "")
        if errors and written:
            res.outcome = "DEGRADED"
            res.outcome_reason = f"{len(errors)} file(s) refused: {errors[0][:160]}"
        return res


def _device(ctx: ToolContext) -> str | None:
    row = db.query_one("SELECT device_id FROM tasks WHERE id=?", (ctx.task_id,))
    return row["device_id"] if row else None


def _upsert_staged(sid: str, rel: str, data: bytes, ctx: ToolContext) -> None:
    db.execute(
        "INSERT INTO staged_files (session_id, path, sha256, bytes, staged_by, "
        "device_id, staged_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(session_id, path) "
        "DO UPDATE SET sha256=excluded.sha256, bytes=excluded.bytes, "
        "staged_by=excluded.staged_by, device_id=excluded.device_id, "
        "staged_at=excluded.staged_at",
        (sid, rel, _sha(data), len(data), ctx.principal, _device(ctx), time.time()))


class ExecuteRemoteTool(Tool):
    name = "execute_remote"
    risk = Risk.COMPUTE
    timeout_s = 330.0
    description = (
        "Run a shell command on the node, in its sandbox (no network, no host "
        "filesystem, a wall-clock deadline), over a fresh copy of this session's "
        "staged files mounted at /workspace. Use it to run tests or scripts "
        "against code you staged with stage_files. Files the command creates or "
        "changes come back in the result; the staging area itself is unchanged.")
    parameters = {"type": "object", "properties": {
        "command": {"type": "string",
                    "description": "shell command, run with /workspace as cwd"},
        "timeout_s": {"type": "integer",
                      "description": "wall-clock limit, default 60, max 300"},
        "return_files": {"type": "array", "items": {"type": "string"},
                         "description": "glob patterns of result files to return "
                                        "(default: every changed file)"},
    }, "required": ["command"]}

    def approval_summary(self, args: dict[str, Any]) -> str:
        return f"run in the node sandbox: {str(args.get('command', ''))[:400]}"

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        command = str(args.get("command", "")).strip()
        if not command:
            return ToolResult(False, error="command must not be empty")
        sid = _session(ctx)
        src = staging_dir(sid)
        ws = WORKSPACE_DIR / f"remote-{sid[-12:]}-{int(time.time() * 1000)}"
        if src.exists():
            shutil.copytree(src, ws, symlinks=False)
        else:
            ws.mkdir(parents=True)
        before = _snapshot(ws)
        timeout = max(5, min(int(args.get("timeout_s") or 60), 300))
        res = run_code(command, language="bash", task_id=ctx.task_id, workspace=ws,
                       timeout_s=timeout)
        after = _snapshot(ws)
        noise = {res.get("script"), "_sandbox_init.py"}
        changed = sorted(p for p, h in after.items()
                         if p not in noise and before.get(p) != h)
        patterns = [str(x) for x in args.get("return_files") or []]
        if patterns:
            changed = [p for p in changed if any(fnmatch.fnmatch(p, g)
                                                 for g in patterns)]
        files = []
        for rel in changed[:RETURN_FILES]:
            data = (ws / rel).read_bytes()
            item: dict[str, Any] = {"path": rel, "bytes": len(data),
                                    "sha256": _sha(data)}
            if len(data) <= RETURN_FILE_BYTES:
                try:
                    item["content"] = data.decode("utf-8")
                except UnicodeDecodeError:
                    item["content_b64"] = base64.b64encode(data).decode()
            else:
                item["omitted"] = f"larger than {RETURN_FILE_BYTES} bytes"
            files.append(item)
        shutil.rmtree(ws, ignore_errors=True)
        audit.record("sandbox", "execute_remote", task_id=ctx.task_id,
                     actor=ctx.principal,
                     outcome="OK" if res.get("ok") else "FAILED",
                     detail={"session": sid, "exit_code": res.get("exit_code"),
                             "engine": res.get("engine"),
                             "changed": len(changed)})
        ctx.note("sandbox", f"execute_remote exit {res.get('exit_code')}",
                 f"engine={res.get('engine')} {res.get('duration_s')}s, "
                 f"{len(changed)} changed file(s)", {k: res.get(k) for k in (
                     "exit_code", "engine", "network", "duration_s", "timed_out")})
        out = ToolResult(
            bool(res.get("ok")),
            content={"exit_code": res.get("exit_code"), "engine": res.get("engine"),
                     "network": res.get("network"), "timed_out": res.get("timed_out"),
                     "duration_s": res.get("duration_s"),
                     "stdout": res.get("stdout", ""), "stderr": res.get("stderr", ""),
                     "changed_files": files,
                     "more_changed": max(0, len(changed) - RETURN_FILES)},
            display=f"exit {res.get('exit_code')} in {res.get('duration_s')}s, "
                    f"{len(changed)} changed",
            error="" if res.get("ok") else (res.get("stderr") or "non-zero exit")[:400],
            meta={"engine": res.get("engine")})
        if res.get("engine") not in ("bwrap", "unshare"):
            out.outcome = "DEGRADED"
            out.outcome_reason = (f"sandbox engine {res.get('engine')!r}: no network "
                                  f"namespace isolation on this node")
        return out


# How model output becomes gateable claims lives with the gate's rules, so the
# node and an offline client read a draft identically.
from ..evidence.gatecore import normalise_sections  # noqa: E402


class DeliverTool(Tool):
    name = "deliver"
    risk = Risk.DELIVERABLE
    timeout_s = 180.0
    description = (
        "Produce the deliverable (.docx on the organisation's template) from "
        "claims you drafted. Each claim is a sentence plus the span_ids it rests "
        "on (from retrieve / read_page / extract_values). The node's provenance "
        "gate checks every number, equipment tag and date against the spans it "
        "served to this session: a value found there is kept and linked to its "
        "page; one found nowhere is STRIPPED and reported. Figures you computed "
        "must come from the calculate tool.")
    parameters = {"type": "object", "properties": {
        "kind": {"type": "string", "enum": ["approval_note", "report"]},
        "title": {"type": "string"},
        "subtitle": {"type": "string"},
        "particulars": {"type": "object", "description":
                        "approval_note only: equipment_tag, equipment_desc, "
                        "inspection_ref, inspection_date — each gated like a claim"},
        "sections": {"type": "array", "items": {"type": "object", "properties": {
            "heading": {"type": "string"},
            "claims": {"type": "array", "description":
                       "one sentence each, with the span_ids it rests on",
                       "items": {"type": "object", "properties": {
                           "text": {"type": "string"},
                           "spans": {"type": "array", "items": {"type": "string"}}},
                           "required": ["text"]}},
            "content": {"type": "string", "description":
                        "alternative to claims: prose, one claim per sentence, "
                        "citations inline as (span_id=...)"}},
            "required": ["heading"]}},
    }, "required": ["title", "sections"]}

    def approval_summary(self, args: dict[str, Any]) -> str:
        n = sum(len(s.get("claims") or []) for s in args.get("sections") or [])
        return (f"write {args.get('kind', 'report')} “{args.get('title', '')}” "
                f"({n} claims, gated against served spans)")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        from ..deliverables import docx_builder
        from ..evidence import gate
        sid = _session(ctx)
        sections = normalise_sections(args.get("sections"))
        if not sections:
            # Never write a hollow document: say exactly what was expected.
            return ToolResult(False, error=(
                "no claims found in `sections`. Send sections as "
                "[{\"heading\": \"Assessment\", \"claims\": [{\"text\": "
                "\"Nominal thickness is 12.0 mm.\", \"spans\": "
                "[\"<span_id>\"]}]}] — or a section with a `content` string, "
                "one sentence per claim, citing (span_id=...) inline."))
        parts = args.get("particulars") or {}
        all_spans = sorted({x for s in sections for c in s["claims"]
                            for x in c["spans"]})
        flat: list[dict[str, Any]] = []
        where: list[tuple[str, int]] = []
        for si, s in enumerate(sections):
            for c in s["claims"]:
                flat.append(c)
                where.append(("section", si))
        for key in ("equipment_tag", "equipment_desc", "inspection_ref",
                    "inspection_date"):
            if parts.get(key):
                flat.append({"text": str(parts[key]), "spans": all_spans})
                where.append(("particular", key))  # type: ignore[arg-type]
        report = gate.check(flat, session_id=sid, task_id=ctx.task_id)
        base = (ctx.scratch.get("node_url") or settings.public_url
                or f"http://{settings.host}:{settings.port}")

        gated: dict[int, list[str]] = {}
        particulars: dict[str, str] = {}
        kept_rows, derived_rows, unresolved, verdicts, evidence = [], [], [], [], []
        seen_spans: set[str] = set()
        for (kind, pos), cr in zip(where, report["claims"]):
            if kind == "section":
                gated.setdefault(pos, []).append(cr["text_out"])
            else:
                particulars[pos] = "—" if cr["stripped"] else cr["text_out"]
            for k in cr["kept"]:
                verdicts.append({"value": k["value"], "class": k["class"],
                                 "rationale": (f"served span {k['span_id']} — "
                                               f"{k.get('doc_title')} p.{k.get('page_no')}"
                                               f" ({k['basis']})" if k.get("span_id")
                                               else f"calculation {k.get('calc_id')}"),
                                 "context": cr["text_in"][:200]})
                if k.get("span_id"):
                    kept_rows.append({
                        "parameter": cr["text_in"][:90], "value": k["value"],
                        "unit": k["kind"],
                        "source": f"{k.get('doc_title')}, p.{k.get('page_no')}",
                        "link": f"{base}/evidence/{k['span_id']}"})
                    if k["span_id"] not in seen_spans:
                        seen_spans.add(k["span_id"])
                else:
                    derived_rows.append({"label": cr["text_in"][:90],
                                         "result": k["value"],
                                         "expression": k.get("expression", ""),
                                         "calc_id": k.get("calc_id")})
            for s in cr["stripped"]:
                verdicts.append({"value": s["value"], "class": "D",
                                 "rationale": s["reason"],
                                 "context": cr["text_in"][:200]})
                unresolved.append({"item": f"{s['value']} (“{cr['text_in'][:80]}”)",
                                   "needed": f"a served source span — {s['reason']}"})
        from ..evidence import served
        idx = served.index(session_id=sid, task_id=ctx.task_id)
        evidence = [{"doc_title": idx[s]["doc_title"], "page_no": idx[s]["page_no"],
                     "text": idx[s]["text"]} for s in sorted(seen_spans) if s in idx]

        kind = str(args.get("kind") or "report")
        title = str(args.get("title") or "Report")[:200]
        if kind == "approval_note":
            by_heading = {str(sections[i].get("heading", "")).lower(): lines
                          for i, lines in gated.items()}

            def pick(word: str) -> list[str]:
                return [l for h, ls in by_heading.items() if word in h for l in ls]
            background = " ".join(pick("background"))
            recommendation = " ".join(pick("recommend"))
            assessment = [l for h, ls in by_heading.items()
                          if "background" not in h and "recommend" not in h
                          for l in ls]
            data = docx_builder.ApprovalNoteData(
                subject=title,
                equipment_tag=particulars.get("equipment_tag", ""),
                equipment_desc=particulars.get("equipment_desc", ""),
                inspection_ref=particulars.get("inspection_ref", ""),
                inspection_date=particulars.get("inspection_date", ""),
                prepared_by=f"ClawCal draft for {ctx.principal}",
                background=background, observed_values=kept_rows,
                derived_values=[{"label": d["label"], "result": d["result"],
                                 "unit": "", "expression": d["expression"],
                                 "calc_id": d["calc_id"]} for d in derived_rows],
                assessment=assessment, recommendation=recommendation,
                unresolved=unresolved, evidence=evidence,
                provenance={"verdicts": verdicts})
            art = docx_builder.build_approval_note(data, task_id=ctx.task_id)
        else:
            secs = [{"heading": sections[i].get("heading", f"Section {i + 1}"),
                     "body": lines} for i, lines in sorted(gated.items())]
            if kept_rows:
                secs.append({"heading": "Evidence — every figure and the page it "
                                        "came from", "table": kept_rows,
                             "headers": ("Claim", "Value", "Kind", "Source")})
            if unresolved:
                secs.append({"heading": "Stripped by the provenance gate",
                             "body": [f"{u['item']} — {u['needed']}"
                                      for u in unresolved]})
            art = docx_builder.build_report(title, secs, task_id=ctx.task_id,
                                            subtitle=str(args.get("subtitle") or ""))
        c = report["counts"]
        ctx.note("deliverable", f"Delivered {art['name']}",
                 f"{c['kept']} value(s) kept, {c['stripped']} stripped by the gate",
                 {"artifact": art, "gate": c})
        res = ToolResult(True, content={
            "artifact_id": art["artifact_id"], "name": art["name"],
            "sha256": art["sha256"], "bytes": art["bytes"],
            "download": f"/api/artifacts/{art['artifact_id']}/download",
            "gate": {"ok": report["ok"], "counts": c,
                     "served_spans": report["served_spans"],
                     "stripped": [dict(s, claim=cr["text_in"][:160])
                                  for cr in report["claims"] for s in cr["stripped"]],
                     "recited": [dict(k, claim=cr["text_in"][:160])
                                 for cr in report["claims"] for k in cr["kept"]
                                 if k["basis"] == "recited"]}},
            display=f"{art['name']}: {c['kept']} kept, {c['stripped']} stripped")
        if c["stripped"]:
            res.outcome = "CANNOT_DETERMINE"
            res.outcome_reason = (f"{c['stripped']} value(s) were not in any span "
                                  f"served to this session and were stripped; the "
                                  f"engineer must resolve them before signing")
        else:
            res.outcome = "ESTABLISHED"
        return res
