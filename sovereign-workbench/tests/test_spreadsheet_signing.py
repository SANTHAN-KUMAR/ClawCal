"""The spreadsheet tool, the formula evaluator, and signed artefacts."""
from __future__ import annotations

import json
import time

import pytest

from sovereign import db, signing
from sovereign.tools.base import ToolContext
from sovereign.tools.formula import Workbook


# ------------------------------------------------------------------ formulas

@pytest.mark.parametrize("formula,expected", [
    ("=1+2*3", 7.0), ("=-2^2", 4.0), ("=2^3^2", 512.0), ("=ROUND(2.5,0)", 3.0),
    ("=ROUND(-2.5,0)", -3.0), ("=SUM(A1:A3)", 6.0), ("=AVERAGE(A1:A3)", 2.0),
    ("=IF(A1>0,\"pos\",\"neg\")", "pos"), ("=LOG10(1000)", 3.0),
    ("='Other sheet'!B2*2", 20.0), ("=A1&\"x\"", "1x"), ("=50%", 0.5),
])
def test_formula_semantics_match_excel(formula, expected):
    wb = Workbook({"S": {"A1": 1, "A2": 2, "A3": 3, "X1": formula},
                   "Other sheet": {"B2": 10}})
    assert wb.value("S", "X1") == pytest.approx(expected) \
        if isinstance(expected, float) else wb.value("S", "X1") == expected


def test_unsupported_and_circular_formulas_are_not_guessed():
    wb = Workbook({"S": {"A1": "=VLOOKUP(1,B1:C3,2)", "A2": "=A3", "A3": "=A2",
                         "A4": "=1/0"}})
    assert "VLOOKUP" in str(wb.value("S", "A1"))
    assert "circular" in str(wb.value("S", "A2"))
    assert str(wb.value("S", "A4")) == "#DIV/0!"


# ------------------------------------------------------------ spreadsheet tool

@pytest.fixture()
def register_xlsx(tmp_path):
    from openpyxl import Workbook as XL
    wb = XL()
    ws = wb.active
    ws.title = "Register"
    ws["A1"] = "Plant equipment register"             # a title row, like real ones
    ws.merge_cells("B2:C2")
    ws["B2"] = "Pressure"
    ws.append([])
    ws["A3"], ws["B3"], ws["C3"] = "Tag", "Design (bar g)", "Operating (bar g)"
    ws.append(["V-204", 16, 12])
    ws.append(["V-205", 10, 7.5])
    path = tmp_path / "register.xlsx"
    wb.save(path)
    did = db.new_id("doc")
    db.insert("documents", {"id": did, "title": "register.xlsx", "kind": "xlsx",
                            "path": str(path), "status": "READY", "pages": 1,
                            "created_at": time.time()})
    return did, path


def _ctx(did):
    return ToolContext(task_id=db.new_id("task"), session_id=db.new_id("sess"),
                       scratch={"attachments": [{"doc_id": did, "kind": "spreadsheet",
                                                 "title": "register.xlsx"}]})


def test_read_finds_header_units_and_merged_origins(register_xlsx):
    from sovereign.tools.spreadsheet import SpreadsheetReadTool
    did, _ = register_xlsx
    ctx = _ctx(did)
    r = SpreadsheetReadTool().run({"action": "read", "range": "A1:C5"}, ctx)
    assert r.ok, r.error
    cells = {c["cell"]: c for c in r.content["cells"]}
    assert r.content["header_row"] == 3
    assert cells["B4"]["value"] == "16" and cells["B4"]["unit"] == "bar g"
    assert cells["C2"]["merged_from"] == "B2"
    # Values read become citable sources for the rest of the task.
    assert any("V-204" in p["text"] for p in ctx.scratch["passages"])


def test_edit_writes_formulas_refuses_invented_numbers_and_spares_the_original(
        register_xlsx):
    from sovereign.tools.spreadsheet import SpreadsheetEditTool, SpreadsheetReadTool
    from openpyxl import load_workbook
    did, original = register_xlsx
    ctx = _ctx(did)
    before = original.read_bytes()
    SpreadsheetReadTool().run({"action": "read"}, ctx)          # sources 16, 12 ...
    r = SpreadsheetEditTool().run({"action": "write", "cells": {
        "D3": "Margin (bar)", "D4": "=B4-C4", "E4": 16, "F4": 13.7}}, ctx)
    assert r.ok, r.error
    written = {w["cell"]: w for w in r.content["written"]}
    assert written["D4"]["class"] == "B" and written["D4"]["computes_to"] == "4"
    assert written["E4"]["class"] == "A"                 # 16 is on the sheet
    assert [x["cell"] for x in r.content["refused"]] == ["F4"]
    assert r.outcome == "CANNOT_DETERMINE"
    assert original.read_bytes() == before               # evidence untouched
    from sovereign.config import WORKSPACE_DIR
    wc = load_workbook(str(WORKSPACE_DIR / "sessions" / ctx.session_id /
                           "register.clawcal.xlsx"))
    assert wc["Register"]["D4"].value == "=B4-C4"
    assert "_clawcal_evidence" in wc.sheetnames


def test_only_unsupported_cells_means_nothing_is_written(register_xlsx):
    from sovereign.tools.spreadsheet import SpreadsheetEditTool
    did, _ = register_xlsx
    r = SpreadsheetEditTool().run({"action": "write", "cells": {"E9": 99.9}},
                                  _ctx(did))
    assert not r.ok and "REFUSED" in r.error


def test_generated_spreadsheet_rows_are_provenance_checked():
    from sovereign.tools.docgen import SpreadsheetTool
    ctx = ToolContext(task_id=db.new_id("task"))
    r = SpreadsheetTool().run({"title": "t", "rows": [{"tag": "V-1",
                                                       "wall": "13.7 mm"}]}, ctx)
    assert not r.ok and "REFUSED" in r.error


# ------------------------------------------------------------------ signing

def test_ed25519_matches_rfc8032_vectors():
    seed = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
    assert signing.public_key(seed).hex() == \
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
    sig = signing.sign(seed, b"")
    assert sig.hex().startswith("e5564300c360ac729086e2cc806e828a")
    assert signing.verify(signing.public_key(seed), b"", sig)
    assert not signing.verify(signing.public_key(seed), b"tampered", sig)


def test_audit_export_verifies_offline_and_detects_tampering():
    from sovereign import audit
    from sovereign.control import decisions, report
    audit.record("test", "export_me")
    decisions.record("evidence", "ESTABLISHED", "x")
    path = report.export_audit("tester")
    assert report.verify_audit_export(path)["ok"]
    lines = path.read_bytes().splitlines(keepends=True)
    rec = json.loads(lines[1])
    rec["action"] = "something else"
    lines[1] = json.dumps(rec).encode() + b"\n"
    path.write_bytes(b"".join(lines))
    assert not report.verify_audit_export(path)["ok"]
