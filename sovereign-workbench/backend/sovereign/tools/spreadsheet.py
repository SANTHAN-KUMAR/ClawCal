"""Spreadsheet work as a tool, not only a deliverable (v2 §6.4).

The problem statement names "spreadsheet work" beside file I/O and code
execution. Ingest can read a workbook and docgen can produce one, but until
this the agent could not read a named range of an attached register, add a
derived column, or find out what a formula evaluates to.

Two tools, because the policy engine prices risk per tool, statically:

    spreadsheet_read   READ_ONLY    list sheets, read a range, recalculate
    spreadsheet_edit   LOCAL_WRITE  write cells, add a sheet

Rules that make the result auditable rather than merely convenient:

* The uploaded workbook is evidence and is never modified. Edits go to a
  working copy that belongs to the session, so later turns see earlier edits.
* Formulas are written as formulas (`=B4-B5`), so an engineer can audit the
  derivation in Excel instead of trusting a pasted number.
* Every written cell carries its evidence class, as a cell comment and as a row
  in a hidden `_clawcal_evidence` sheet. A literal number that no source
  passage or recorded calculation supports is refused, exactly as the document
  generators refuse it.
* Cells read become source passages, so a number the agent takes from the
  register is Class A downstream.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import time
from pathlib import Path
from typing import Any

from .. import db
from ..config import ARTIFACT_DIR, WORKSPACE_DIR
from ..evidence import provenance
from ..policy.tool_policy import Risk
from .base import Tool, ToolContext, ToolResult
from .calculator import task_calculations
from .formula import (ExcelError, Unsupported, Workbook, col_index, col_letters,
                      references)

MAX_READ_CELLS = 1200
EVIDENCE_SHEET = "_clawcal_evidence"
_CELL = re.compile(r"^\$?([A-Za-z]{1,3})\$?(\d+)$")
_RANGE = re.compile(r"^\$?([A-Za-z]{1,3})\$?(\d+)(?::\$?([A-Za-z]{1,3})\$?(\d+))?$")
_UNIT_IN_HEADER = re.compile(r"[\(\[]\s*([^\)\]]{1,16})\s*[\)\]]\s*$")


class SheetError(ValueError):
    pass


# ------------------------------------------------------------ resolution

def _session_dir(ctx: ToolContext) -> Path:
    sid = ctx.session_id or ctx.task_id
    d = WORKSPACE_DIR / "sessions" / sid
    d.mkdir(parents=True, exist_ok=True)
    return d


def resolve(ref: str, ctx: ToolContext) -> tuple[Path, Path, str]:
    """(original, working copy, title) for a workbook reference.

    The reference may be a doc id, a title or filename fragment of an indexed
    workbook, or the name of a working copy already in this session.
    """
    ref = (ref or "").strip()
    wanted = [a for a in ctx.scratch.get("attachments", [])
              if str(a.get("title", "")).lower().endswith((".xlsx", ".xlsm"))
              or a.get("kind") == "spreadsheet"]
    row = None
    if ref:
        for r in db.query("SELECT id, title, path FROM documents "
                          "WHERE kind IN ('xlsx','xlsm') ORDER BY created_at DESC"):
            if ref.lower() in (r["id"].lower(), (r["title"] or "").lower()) or \
                    ref.lower() in (r["title"] or "").lower():
                row = r
                break
    elif wanted:
        row = db.query_one("SELECT id, title, path FROM documents WHERE id=?",
                           (wanted[0].get("doc_id"),))
    if row is None:
        wc = _session_dir(ctx) / Path(ref).name if ref else None
        if wc and wc.exists() and wc.suffix.lower() in (".xlsx", ".xlsm"):
            return wc, wc, wc.name
        known = [r["title"] for r in db.query(
            "SELECT title FROM documents WHERE kind IN ('xlsx','xlsm')")]
        raise SheetError(
            f"no workbook matches {ref!r}. Workbooks in the knowledge base: "
            f"{', '.join(known) or 'none'}. Attach an .xlsx to the session first.")
    original = Path(row["path"])
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(row["title"] or original.name).stem)
    working = _session_dir(ctx) / f"{stem}.clawcal.xlsx"
    return original, working, row["title"] or original.name


def _load(path: Path, *, formulas: bool):
    from openpyxl import load_workbook
    try:
        return load_workbook(str(path), data_only=not formulas, read_only=False)
    except Exception as exc:
        raise SheetError(f"cannot open {path.name}: {type(exc).__name__}: "
                         f"{str(exc)[:200]}. If it is a legacy .xls, convert it "
                         f"to .xlsx first.")


def _sheet(wb: Any, name: str | None) -> Any:
    if not name:
        return next(ws for ws in wb.worksheets if ws.title != EVIDENCE_SHEET)
    if name in wb.sheetnames:
        return wb[name]
    lower = {n.lower(): n for n in wb.sheetnames}
    if name.lower() in lower:
        return wb[lower[name.lower()]]
    raise SheetError(f"no sheet {name!r}; sheets are {wb.sheetnames}")


def _grid(wb_formulas: Any) -> dict[str, dict[str, Any]]:
    grid: dict[str, dict[str, Any]] = {}
    for ws in wb_formulas.worksheets:
        cells: dict[str, Any] = {}
        for row in ws.iter_rows():
            for c in row:
                if c.value is not None:
                    v = c.value
                    cells[c.coordinate] = v if not hasattr(v, "text") else str(v.text)
        grid[ws.title] = cells
    return grid


def _merged_lookup(ws: Any) -> dict[str, str]:
    """Every cell inside a merged range -> the range's top-left cell."""
    out: dict[str, str] = {}
    for rng in ws.merged_cells.ranges:
        tl = f"{col_letters(rng.min_col)}{rng.min_row}"
        for r in range(rng.min_row, rng.max_row + 1):
            for c in range(rng.min_col, rng.max_col + 1):
                out[f"{col_letters(c)}{r}"] = tl
    return out


def _header_row(ws: Any, max_scan: int = 15) -> int | None:
    """The first row that reads as column headers above numeric data.

    Real registers put titles, notes and blank rows above the table, and often
    split a header over two merged rows. The header is the last text-heavy row
    before rows that carry numbers.
    """
    best = None
    rows = list(ws.iter_rows(min_row=1, max_row=min(ws.max_row, max_scan + 3),
                             values_only=True))
    for i, row in enumerate(rows[:max_scan], 1):
        texts = [v for v in row if isinstance(v, str) and v.strip()]
        if len(texts) < 2:
            continue
        below = rows[i:i + 3]
        numeric_below = sum(1 for r in below for v in r
                            if isinstance(v, (int, float)) and not isinstance(v, bool))
        if numeric_below >= 2:
            best = i
            break
        best = best or i
    return best


def _fmt(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.10g}"
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


# ---------------------------------------------------------------- reading

def list_sheets(path: Path) -> list[dict[str, Any]]:
    wb = _load(path, formulas=True)
    try:
        out = []
        for ws in wb.worksheets:
            if ws.title == EVIDENCE_SHEET:
                continue
            out.append({"sheet": ws.title, "dimensions": ws.dimensions,
                        "rows": ws.max_row, "columns": ws.max_column,
                        "merged_ranges": len(ws.merged_cells.ranges),
                        "header_row": _header_row(ws),
                        "state": ws.sheet_state})
        return out
    finally:
        wb.close()


def read_range(path: Path, sheet: str | None, rng: str | None) -> dict[str, Any]:
    wbf = _load(path, formulas=True)
    wbv = _load(path, formulas=False)
    try:
        wsf, wsv = _sheet(wbf, sheet), _sheet(wbv, sheet)
        if rng:
            m = _RANGE.match(rng.replace(" ", ""))
            if not m:
                raise SheetError(f"range {rng!r} is not like 'A1:F20' or 'B4'")
            c0, r0 = col_index(m.group(1)), int(m.group(2))
            c1 = col_index(m.group(3)) if m.group(3) else c0
            r1 = int(m.group(4)) if m.group(4) else r0
        else:
            c0, r0 = 1, 1
            c1, r1 = min(wsf.max_column, 26), min(wsf.max_row, 60)
        c0, c1 = sorted((c0, c1))
        r0, r1 = sorted((r0, r1))
        if (c1 - c0 + 1) * (r1 - r0 + 1) > MAX_READ_CELLS:
            r1 = r0 + max(1, MAX_READ_CELLS // (c1 - c0 + 1)) - 1

        merged = _merged_lookup(wsf)
        header = _header_row(wsf)
        headers: dict[int, str] = {}
        if header:
            for c in range(c0, c1 + 1):
                parts = []
                for hr in (header - 1, header):   # two-row merged headers
                    if hr < 1:
                        continue
                    coord = f"{col_letters(c)}{hr}"
                    src = merged.get(coord, coord)
                    v = wsf[src].value
                    if isinstance(v, str) and v.strip() and v.strip() not in parts:
                        parts.append(v.strip())
                if parts:
                    headers[c] = " / ".join(parts)

        cells: list[dict[str, Any]] = []
        for r in range(r0, r1 + 1):
            for c in range(c0, c1 + 1):
                coord = f"{col_letters(c)}{r}"
                src = merged.get(coord, coord)
                fv = wsf[src].value
                vv = wsv[src].value
                if fv is None and vv is None:
                    continue
                cell: dict[str, Any] = {"cell": coord, "value": _fmt(vv if vv is not None
                                                                       else fv)}
                if isinstance(fv, str) and fv.startswith("="):
                    cell["formula"] = fv
                    if vv is None:
                        cell["value"] = ""
                        cell["note"] = "formula has no cached value; recalculate"
                if src != coord:
                    cell["merged_from"] = src
                if c in headers and r != header:
                    cell["header"] = headers[c]
                    um = _UNIT_IN_HEADER.search(headers[c])
                    if um:
                        cell["unit"] = um.group(1).strip()
                cells.append(cell)
        return {"sheet": wsf.title, "range": f"{col_letters(c0)}{r0}:"
                                             f"{col_letters(c1)}{r1}",
                "header_row": header, "headers": {col_letters(k): v
                                                  for k, v in headers.items()},
                "cells": cells, "truncated": (r1 < wsf.max_row and not rng)}
    finally:
        wbf.close()
        wbv.close()


def recalculate(path: Path, sheet: str | None = None) -> dict[str, Any]:
    wbf = _load(path, formulas=True)
    try:
        grid = _grid(wbf)
        engine = Workbook(grid)
        sheets = [sheet] if sheet else [s for s in grid if s != EVIDENCE_SHEET]
        computed, unsupported, errors = [], [], []
        for sh in sheets:
            if sh not in grid:
                raise SheetError(f"no sheet {sh!r}")
            for coord, raw in grid[sh].items():
                if not (isinstance(raw, str) and raw.startswith("=")):
                    continue
                v = engine.value(sh, coord)
                entry = {"cell": f"{sh}!{coord}", "formula": raw}
                if isinstance(v, Unsupported):
                    unsupported.append({**entry, "reason": v.reason})
                elif isinstance(v, ExcelError):
                    errors.append({**entry, "value": str(v)})
                elif isinstance(v, list):
                    unsupported.append({**entry, "reason": "array result"})
                else:
                    computed.append({**entry, "value": _fmt(v)})
        return {"computed": computed, "unsupported": unsupported, "errors": errors}
    finally:
        wbf.close()


# ------------------------------------------------------------ provenance

def _passages_for(title: str, doc_id: str | None, sheet: str,
                  cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for c in cells:
        label = c.get("header") or ""
        out.append({"chunk_id": f"{doc_id or title}:{sheet}!{c['cell']}",
                    "doc_id": doc_id, "doc_title": title, "page_no": 1,
                    "text": f"{sheet}!{c['cell']} {label}: {c['value']}".strip(),
                    "region": {"sheet": sheet, "cell": c["cell"]}, "score": 1.0})
    return out


def _classify_value(value: Any, ctx: ToolContext) -> tuple[str, str]:
    """Evidence class for one literal written into a cell."""
    if isinstance(value, str) and value.startswith("="):
        refs = references(value)
        return "B", (f"formula over {', '.join(refs[:6])}" if refs
                     else "formula with literal operands")
    if isinstance(value, bool) or value is None:
        return "C", "label"
    if isinstance(value, (int, float)) or re.fullmatch(r"\s*[-+]?\d+(\.\d+)?\s*",
                                                       str(value)):
        # Looked up in the evidence indexes directly: prose classification skips
        # bare integers as likely counts, but in a cell 16 is a pressure.
        key = f"{float(value):g}"
        ectx = provenance.EvidenceContext(
            passages=ctx.scratch.get("passages", []),
            calculations=task_calculations(ctx.task_id))
        calc = ectx.calc_index()
        if key in calc:
            c = calc[key][0]
            return "B", f"calculation {c.get('id')}: {c.get('expression')}"
        if key in ectx.tainted_results():
            return "D", "a calculation over unestablished inputs"
        src = ectx.source_index()
        if key in src:
            h = src[key][0]
            return "A", f"source: {h.get('doc_title')} p.{h.get('page_no')}"
        return "D", "unsupported"
    return "C", "text"


# ------------------------------------------------------------------ tools

class SpreadsheetReadTool(Tool):
    name = "spreadsheet_read"
    risk = Risk.READ_ONLY
    timeout_s = 120.0
    description = (
        "Work with an attached Excel workbook. action='sheets' lists its sheets "
        "with their header rows; action='read' returns the cells of a range "
        "(e.g. 'A1:F30') with their column headers, units, formulas and merged-"
        "cell origins; action='recalculate' evaluates every formula and reports "
        "any it cannot compute. Values you read here are citable source values.")
    parameters = {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["sheets", "read", "recalculate"]},
        "workbook": {"type": "string",
                     "description": "document id or title; defaults to the attached "
                                    "workbook"},
        "sheet": {"type": "string"},
        "range": {"type": "string", "description": "e.g. 'A1:F30' or 'C7'"},
        "original": {"type": "boolean",
                     "description": "read the uploaded original rather than this "
                                    "session's edited copy"},
    }, "required": ["action"]}

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            original, working, title = resolve(str(args.get("workbook") or ""), ctx)
            path = working if (working.exists() and not args.get("original")) \
                else original
            action = str(args.get("action") or "sheets")
            if action == "sheets":
                sheets = list_sheets(path)
                return ToolResult(True, content={"workbook": title,
                                                 "edited_copy": path == working,
                                                 "sheets": sheets},
                                  display=f"{len(sheets)} sheet(s) in {title}")
            if action == "read":
                data = read_range(path, args.get("sheet"), args.get("range"))
                doc = db.query_one("SELECT id FROM documents WHERE path=?",
                                   (str(original),))
                passages = _passages_for(title, doc["id"] if doc else None,
                                         data["sheet"], data["cells"])
                if passages:
                    provenance.store_passages(ctx.task_id, passages)
                    ctx.scratch.setdefault("passages", []).extend(passages)
                ctx.note("spreadsheet", f"Read {data['sheet']}!{data['range']}",
                         f"{len(data['cells'])} cells from {title}",
                         {"sheet": data["sheet"], "range": data["range"]})
                return ToolResult(True, content={"workbook": title, **data},
                                  display=f"{len(data['cells'])} cells from "
                                          f"{data['sheet']}!{data['range']}")
            if action == "recalculate":
                res = recalculate(path, args.get("sheet"))
                out = ToolResult(True, content={"workbook": title, **res},
                                 display=f"{len(res['computed'])} formulas computed, "
                                         f"{len(res['unsupported'])} unsupported")
                if res["unsupported"]:
                    out.outcome = "DEGRADED"
                    out.outcome_reason = (
                        f"{len(res['unsupported'])} formula(s) use features the "
                        f"appliance's evaluator does not support; their values are "
                        f"CANNOT DETERMINE here — open the workbook in Excel to "
                        f"compute them")
                return out
            return ToolResult(False, error=f"unknown action {action!r}")
        except SheetError as exc:
            return ToolResult(False, error=str(exc))


class SpreadsheetEditTool(Tool):
    name = "spreadsheet_edit"
    risk = Risk.LOCAL_WRITE
    timeout_s = 120.0
    description = (
        "Edit this session's working copy of an attached workbook (the uploaded "
        "original is never changed). action='write' sets cells: "
        "{\"cells\": {\"E1\": \"Margin (bar)\", \"E2\": \"=C2-D2\"}}. Write derived "
        "values as FORMULAS so the engineer can audit them. A literal number "
        "must come from a source you read or a calculator result, or it is "
        "refused. action='add_sheet' adds a sheet, optionally with a header row.")
    parameters = {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["write", "add_sheet"]},
        "workbook": {"type": "string"},
        "sheet": {"type": "string"},
        "cells": {"type": "object",
                  "description": "cell -> value or formula, e.g. {\"E2\": \"=C2-D2\"}"},
        "headers": {"type": "array", "items": {"type": "string"},
                    "description": "for add_sheet: the header row"},
    }, "required": ["action"]}

    def approval_summary(self, args: dict[str, Any]) -> str:
        cells = args.get("cells") or {}
        return (f"edit workbook {args.get('workbook') or '(attached)'}: "
                f"{args.get('action')} on {args.get('sheet') or 'first sheet'}, "
                f"{len(cells)} cell(s): " + ", ".join(
                    f"{k}={v}" for k, v in list(cells.items())[:8]))

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        from openpyxl.comments import Comment

        try:
            original, working, title = resolve(str(args.get("workbook") or ""), ctx)
        except SheetError as exc:
            return ToolResult(False, error=str(exc))
        if not working.exists():
            shutil.copyfile(original, working)
        try:
            wb = _load(working, formulas=True)
        except SheetError as exc:
            return ToolResult(False, error=str(exc))
        action = str(args.get("action") or "write")
        try:
            if action == "add_sheet":
                name = str(args.get("sheet") or "").strip()[:31]
                if not name or name in wb.sheetnames or name == EVIDENCE_SHEET:
                    return ToolResult(False, error=f"sheet name {name!r} is empty, "
                                                   f"reserved or already exists")
                ws = wb.create_sheet(name)
                for i, h in enumerate(args.get("headers") or [], 1):
                    ws.cell(row=1, column=i, value=str(h))
                self._save(wb, working, ctx, title)
                return ToolResult(True, content={"added_sheet": name,
                                                 "working_copy": working.name},
                                  display=f"added sheet {name}")

            if action != "write":
                return ToolResult(False, error=f"unknown action {action!r}")
            cells = args.get("cells") or {}
            if not isinstance(cells, dict) or not cells:
                return ToolResult(False, error="'cells' must be an object like "
                                               "{\"E2\": \"=C2-D2\"}")
            try:
                ws = _sheet(wb, args.get("sheet"))
            except SheetError as exc:
                return ToolResult(False, error=str(exc))

            plan, refused = [], []
            for coord, value in cells.items():
                coord = str(coord).replace("$", "").upper()
                if not _CELL.match(coord):
                    refused.append({"cell": coord, "why": "not a cell reference"})
                    continue
                if isinstance(value, str) and value.strip().startswith("="):
                    value = value.strip()
                elif isinstance(value, str) and re.fullmatch(
                        r"\s*[-+]?\d+(\.\d+)?\s*", value):
                    value = float(value)
                cls, why = _classify_value(value, ctx)
                if cls == "D":
                    refused.append({"cell": coord, "value": value,
                                    "why": "no retrieved source passage or recorded "
                                           "calculation supports this number"})
                    continue
                plan.append((coord, value, cls, why))

            if refused and not plan:
                return ToolResult(
                    False, error=(
                        "REFUSED: nothing was written. " + "; ".join(
                            f"{r['cell']}: {r['why']}" for r in refused) +
                        ". Write a formula over the source cells instead, or "
                        "compute the value with the calculator first."),
                    meta={"refused": refused})

            ev = wb[EVIDENCE_SHEET] if EVIDENCE_SHEET in wb.sheetnames else None
            if ev is None:
                ev = wb.create_sheet(EVIDENCE_SHEET)
                ev.append(["sheet", "cell", "value", "class", "basis", "task",
                           "principal", "written_at"])
                ev.sheet_state = "hidden"
            names = {"A": "SOURCE", "B": "DERIVED", "C": "INTERPRETATION"}
            for coord, value, cls, why in plan:
                cell = ws[coord]
                cell.value = value
                cell.comment = Comment(f"ClawCal: Class {cls} {names.get(cls, '')}"
                                       f" — {why}"[:300], "ClawCal")
                ev.append([ws.title, coord, _fmt(value), cls, why, ctx.task_id,
                           ctx.principal, time.strftime("%Y-%m-%d %H:%M:%S")])
            art = self._save(wb, working, ctx, title)
        finally:
            wb.close()

        res = recalculate(working, ws.title)
        values = {r["cell"].split("!")[1]: r["value"] for r in res["computed"]}
        written = [{"cell": c, "value": _fmt(v), "class": cls,
                    **({"computes_to": values[c]} if c in values else {})}
                   for c, v, cls, _ in plan]
        ctx.note("spreadsheet", f"Wrote {len(plan)} cell(s) to {working.name}",
                 ", ".join(f"{w['cell']}={w['value']}" for w in written[:8]),
                 {"written": written, "refused": refused})
        out = ToolResult(True, content={"working_copy": working.name,
                                        "written": written, "refused": refused,
                                        "artifact_id": art},
                         display=f"wrote {len(plan)} cell(s)"
                                 + (f", refused {len(refused)}" if refused else ""))
        if refused:
            out.outcome = "CANNOT_DETERMINE"
            out.outcome_reason = (f"{len(refused)} cell(s) refused as unsupported: "
                                  + ", ".join(r["cell"] for r in refused[:6]))
        elif any(cls == "C" for _, v, cls, _ in plan if not isinstance(v, str)):
            out.outcome = "INTERPRETED"
        return out

    def _save(self, wb: Any, working: Path, ctx: ToolContext, title: str) -> str:
        from ..audit import head
        wb.properties.keywords = f"clawcal audit-head {head()}"
        tmp = working.with_suffix(".tmp.xlsx")
        wb.save(str(tmp))
        tmp.replace(working)
        data = working.read_bytes()
        dest = ARTIFACT_DIR / f"{ctx.session_id or ctx.task_id}-{working.name}"
        dest.write_bytes(data)
        db.execute("DELETE FROM artifacts WHERE task_id=? AND path=?",
                   (ctx.task_id, str(dest)))
        aid = db.new_id("art")
        db.insert("artifacts", {
            "id": aid, "task_id": ctx.task_id, "name": working.name,
            "kind": "workbook", "path": str(dest), "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "meta": db.jdump({"derived_from": title, "working_copy": str(working)}),
            "created_at": time.time()})
        return aid
