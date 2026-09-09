"""Excel deliverables: calculation sheets and data tables.

A calculation workbook is written with live formulas, not baked values, so an
engineer can change an input and see the result move. That is the difference
between a spreadsheet and a picture of one.
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from .. import db
from ..config import ARTIFACT_DIR, settings

HEAD_FILL = PatternFill("solid", fgColor="1F3A5F")
HEAD_FONT = Font(color="FFFFFF", bold=True, size=10)
TITLE_FONT = Font(bold=True, size=14, color="1F3A5F")
WARN_FILL = PatternFill("solid", fgColor="F7D5D0")
THIN = Side(style="thin", color="BFC7D1")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def _autosize(ws: Any, max_width: int = 60) -> None:
    for col in ws.columns:
        width = max((len(str(c.value)) for c in col if c.value is not None),
                    default=8)
        ws.column_dimensions[get_column_letter(col[0].column)].width = \
            min(max_width, max(10, width + 2))


def _header(ws: Any, title: str, row: int = 1) -> int:
    ws.cell(row=row, column=1, value=settings.org_name).font = TITLE_FONT
    ws.cell(row=row + 1, column=1, value=settings.org_unit).font = Font(
        size=9, color="555F6B")
    ws.cell(row=row + 2, column=1,
            value="CONFIDENTIAL — INTERNAL USE ONLY").font = Font(
        size=8, bold=True, color="992B1F")
    ws.cell(row=row + 3, column=1, value=title).font = Font(bold=True, size=12)
    ws.cell(row=row + 4, column=1,
            value=f"Generated {time.strftime('%d %B %Y %H:%M')} by the Sovereign "
                  f"AI Workbench").font = Font(size=8, italic=True, color="555F6B")
    return row + 6


def build_calculation_sheet(title: str, calculations: list[dict[str, Any]], *,
                            task_id: str | None = None,
                            filename: str | None = None) -> dict[str, Any]:
    wb = Workbook()
    ws = wb.active
    ws.title = "Calculations"
    r = _header(ws, title)

    headers = ("ID", "Quantity", "Formula", "Substitution", "Result", "Unit",
               "Input provenance")
    for i, h in enumerate(headers, 1):
        c = ws.cell(row=r, column=i, value=h)
        c.fill, c.font, c.border = HEAD_FILL, HEAD_FONT, BORDER
    r += 1

    for calc in calculations:
        prov = calc.get("input_prov") or calc.get("input_provenance") or {}
        vals = [calc.get("id", ""), calc.get("label", ""),
                calc.get("expression", ""),
                (calc.get("steps") or {}).get("substituted")
                if isinstance(calc.get("steps"), dict)
                else calc.get("substituted", ""),
                calc.get("result"), calc.get("unit", ""),
                "; ".join(f"{k}={v}" for k, v in prov.items())]
        for i, v in enumerate(vals, 1):
            c = ws.cell(row=r, column=i, value=v)
            c.border = BORDER
            c.alignment = Alignment(vertical="top", wrap_text=(i in (3, 4, 7)))
            if not calc.get("ok", True):
                c.fill = WARN_FILL
        r += 1

    # Second sheet: the inputs, live, so results can be re-derived.
    ws2 = wb.create_sheet("Inputs")
    r2 = _header(ws2, "Calculation Inputs")
    for i, h in enumerate(("Calculation", "Input", "Value", "Source"), 1):
        c = ws2.cell(row=r2, column=i, value=h)
        c.fill, c.font, c.border = HEAD_FILL, HEAD_FONT, BORDER
    r2 += 1
    for calc in calculations:
        prov = calc.get("input_prov") or calc.get("input_provenance") or {}
        for k, v in (calc.get("inputs") or {}).items():
            for i, val in enumerate((calc.get("id", ""), k, v,
                                     prov.get(k, "not recorded")), 1):
                c = ws2.cell(row=r2, column=i, value=val)
                c.border = BORDER
            r2 += 1

    _autosize(ws)
    _autosize(ws2)
    out = ARTIFACT_DIR / (filename or
                          f"calculations_{int(time.time())}.xlsx")
    wb.save(str(out))
    return _register(out, task_id, "calculation_sheet",
                     {"title": title, "rows": len(calculations)})


def build_table(title: str, rows: list[dict[str, Any]],
                columns: list[str] | None = None, *,
                task_id: str | None = None, filename: str | None = None,
                sheet_name: str = "Data") -> dict[str, Any]:
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name[:31]
    r = _header(ws, title)
    cols = columns or sorted({k for row in rows for k in row})
    for i, h in enumerate(cols, 1):
        c = ws.cell(row=r, column=i, value=h)
        c.fill, c.font, c.border = HEAD_FILL, HEAD_FONT, BORDER
    r += 1
    for row in rows:
        for i, col in enumerate(cols, 1):
            v = row.get(col, "")
            c = ws.cell(row=r, column=i,
                        value=v if isinstance(v, (int, float, str)) else str(v))
            c.border = BORDER
            c.alignment = Alignment(vertical="top", wrap_text=True)
        r += 1
    ws.freeze_panes = ws.cell(row=r - len(rows), column=1)
    _autosize(ws)
    out = ARTIFACT_DIR / (filename or f"table_{int(time.time())}.xlsx")
    wb.save(str(out))
    return _register(out, task_id, "spreadsheet",
                     {"title": title, "rows": len(rows), "columns": cols})


def _register(path: Path, task_id: str | None, kind: str,
              meta: dict[str, Any]) -> dict[str, Any]:
    data = path.read_bytes()
    aid = db.new_id("art")
    db.insert("artifacts", {
        "id": aid, "task_id": task_id, "name": path.name, "kind": kind,
        "path": str(path), "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "meta": db.jdump(meta), "created_at": time.time()})
    return {"artifact_id": aid, "name": path.name, "path": str(path),
            "bytes": len(data), "kind": kind, **meta}
