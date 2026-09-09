"""Organisation-templated Word deliverables.

The gap this closes is the one the survey calls out: python-docx writes a valid
.docx in an afternoon, but what an organisation means by "an approval note" is
*their* approval note -- letterhead, reference numbering, clause numbering,
tables, signature block, retention footer. A syntactically valid file that does
not look like the department's own paperwork is not a deliverable.

Every approval note produced here also carries an evidence appendix in which each
significant number is listed with its class (SOURCE / DERIVED / INTERPRETATION /
CANNOT DETERMINE) and its origin. A reader can check the document against the
record without opening the workbench.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

from .. import db
from ..config import ARTIFACT_DIR, settings

ACCENT = RGBColor(0x1F, 0x3A, 0x5F)
MUTED = RGBColor(0x55, 0x5F, 0x6B)


@dataclass
class ApprovalNoteData:
    subject: str
    equipment_tag: str = ""
    equipment_desc: str = ""
    inspection_ref: str = ""
    inspection_date: str = ""
    reference_no: str = ""
    prepared_by: str = "Sovereign AI Workbench (draft)"
    recommending_engineer: str = ""
    approving_authority: str = "Head of Mechanical Inspection"
    background: str = ""
    observed_values: list[dict[str, Any]] = field(default_factory=list)
    derived_values: list[dict[str, Any]] = field(default_factory=list)
    assessment: list[str] = field(default_factory=list)
    compliance: list[dict[str, Any]] = field(default_factory=list)
    recommendation: str = ""
    unresolved: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)
    sop_refs: list[str] = field(default_factory=list)


def _shade(cell: Any, hex_colour: str) -> None:
    tc = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:fill"), hex_colour)
    tc.append(shd)


def _rule(paragraph: Any, size: int = 6, colour: str = "1F3A5F") -> None:
    p = paragraph._p.get_or_add_pPr()
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), str(size))
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), colour)
    borders.append(bottom)
    p.append(borders)


def _styles(doc: Document) -> None:
    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(10.5)
    normal.paragraph_format.space_after = Pt(6)
    normal.paragraph_format.line_spacing = 1.08


def _letterhead(doc: Document, ref: str, date_str: str) -> None:
    org = doc.add_paragraph()
    org.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = org.add_run(settings.org_name.upper())
    r.bold = True
    r.font.size = Pt(16)
    r.font.color.rgb = ACCENT

    unit = doc.add_paragraph()
    unit.alignment = WD_ALIGN_PARAGRAPH.CENTER
    ru = unit.add_run(settings.org_unit.upper())
    ru.font.size = Pt(10.5)
    ru.font.color.rgb = MUTED
    ru.bold = True

    cls = doc.add_paragraph()
    cls.alignment = WD_ALIGN_PARAGRAPH.CENTER
    rc = cls.add_run("CONFIDENTIAL — INTERNAL USE ONLY")
    rc.font.size = Pt(8.5)
    rc.bold = True
    rc.font.color.rgb = RGBColor(0x99, 0x2B, 0x1F)
    _rule(cls, 12)

    meta = doc.add_table(rows=2, cols=2)
    meta.alignment = WD_TABLE_ALIGNMENT.CENTER
    meta.autofit = True
    cells = [("Reference No.", ref), ("Date", date_str)]
    meta.cell(0, 0).text = cells[0][0]
    meta.cell(0, 1).text = cells[0][1]
    meta.cell(1, 0).text = cells[1][0]
    meta.cell(1, 1).text = cells[1][1]
    for row in meta.rows:
        row.cells[0].paragraphs[0].runs[0].bold = True
        for c in row.cells:
            for p in c.paragraphs:
                for run in p.runs:
                    run.font.size = Pt(9)
    doc.add_paragraph()


def _clause(doc: Document, number: str, text: str, *, bold_lead: str = "") -> None:
    p = doc.add_paragraph()
    p.paragraph_format.left_indent = Inches(0.55)
    p.paragraph_format.first_line_indent = Inches(-0.55)
    run = p.add_run(f"{number}\t")
    run.bold = True
    if bold_lead:
        b = p.add_run(bold_lead)
        b.bold = True
    p.add_run(text)


def _heading(doc: Document, number: str, title: str) -> None:
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(12)
    r = p.add_run(f"{number}. {title.upper()}")
    r.bold = True
    r.font.size = Pt(11)
    r.font.color.rgb = ACCENT
    _rule(p, 4, "AAB4C4")


def _value_table(doc: Document, rows: list[dict[str, Any]],
                 headers: tuple[str, ...]) -> None:
    t = doc.add_table(rows=1, cols=len(headers))
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    for i, h in enumerate(headers):
        cell = t.rows[0].cells[i]
        cell.text = h
        _shade(cell, "1F3A5F")
        for p in cell.paragraphs:
            for r in p.runs:
                r.bold = True
                r.font.size = Pt(9)
                r.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
    for row in rows:
        cells = t.add_row().cells
        for i, key in enumerate(("parameter", "value", "unit", "source")):
            if i < len(headers):
                cells[i].text = str(row.get(key, ""))
                for p in cells[i].paragraphs:
                    for r in p.runs:
                        r.font.size = Pt(9)


def _signature_block(doc: Document, data: ApprovalNoteData) -> None:
    doc.add_paragraph()
    t = doc.add_table(rows=2, cols=3)
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    labels = [("Prepared by", data.prepared_by),
              ("Recommended by", data.recommending_engineer or "____________________"),
              ("Approved by", data.approving_authority)]
    for i, (role, who) in enumerate(labels):
        c = t.cell(0, i)
        c.text = "\n\n\n____________________"
        p = t.cell(1, i).paragraphs[0]
        r = p.add_run(f"{role}\n")
        r.bold = True
        r.font.size = Pt(9)
        r2 = p.add_run(who)
        r2.font.size = Pt(9)
        r2.font.color.rgb = MUTED


def _footer(doc: Document, ref: str) -> None:
    section = doc.sections[0]
    p = section.footer.paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = p.add_run(
        f"{ref}  |  {settings.org_name}  |  CONFIDENTIAL — INTERNAL USE ONLY  |  "
        f"Retain per SOP-MECH-014 clause 7.1")
    r.font.size = Pt(7.5)
    r.font.color.rgb = MUTED


CLASS_LABEL = {"A": "SOURCE", "B": "DERIVED", "C": "INTERPRETATION",
               "D": "CANNOT DETERMINE"}


def build_approval_note(data: ApprovalNoteData, *, task_id: str | None = None,
                        filename: str | None = None) -> dict[str, Any]:
    doc = Document()
    _styles(doc)
    sec = doc.sections[0]
    sec.top_margin = Inches(0.7)
    sec.bottom_margin = Inches(0.7)
    sec.left_margin = Inches(0.85)
    sec.right_margin = Inches(0.85)

    ref = data.reference_no or f"AN/{time.strftime('%Y')}/{db.new_id('')[1:6].upper()}"
    date_str = time.strftime("%d %B %Y")
    _letterhead(doc, ref, date_str)

    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    tr = title.add_run("APPROVAL NOTE")
    tr.bold = True
    tr.font.size = Pt(13)
    tr.font.color.rgb = ACCENT

    subj = doc.add_paragraph()
    subj.alignment = WD_ALIGN_PARAGRAPH.CENTER
    sr = subj.add_run(f"Subject: {data.subject}")
    sr.bold = True
    sr.font.size = Pt(10.5)
    doc.add_paragraph()

    # 1. Equipment particulars
    _heading(doc, "1", "Equipment and Reference")
    particulars = [
        {"parameter": "Equipment Tag", "value": data.equipment_tag or "—",
         "unit": "", "source": "Equipment register"},
        {"parameter": "Description", "value": data.equipment_desc or "—",
         "unit": "", "source": data.inspection_ref or "—"},
        {"parameter": "Inspection Reference", "value": data.inspection_ref or "—",
         "unit": "", "source": data.inspection_ref or "—"},
        {"parameter": "Inspection Date", "value": data.inspection_date or "—",
         "unit": "", "source": data.inspection_ref or "—"},
    ]
    _value_table(doc, particulars, ("Parameter", "Value", "Unit", "Source"))

    # 2. Background
    _heading(doc, "2", "Background")
    _clause(doc, "2.1", data.background or
            "This note records the assessment of the referenced inspection against "
            "the applicable internal procedure.")
    if data.sop_refs:
        _clause(doc, "2.2", "Assessed against: " + "; ".join(data.sop_refs) + ".")

    # 3. Observed values (Class A)
    _heading(doc, "3", "Observed Values (Source Evidence)")
    if data.observed_values:
        _clause(doc, "3.1", "The following values are taken directly from the "
                            "inspection record. Each is traceable to a document page.")
        _value_table(doc, data.observed_values,
                     ("Parameter", "Value", "Unit", "Source (document, page)"))
    else:
        _clause(doc, "3.1", "No source values were established. "
                            "[CANNOT DETERMINE]")

    # 4. Derived values (Class B)
    _heading(doc, "4", "Derived Values (Calculated)")
    if data.derived_values:
        _clause(doc, "4.1", "The following values were computed from the source "
                            "values above. Each calculation is reproducible from "
                            "its recorded inputs.")
        t = doc.add_table(rows=1, cols=4)
        t.style = "Table Grid"
        for i, h in enumerate(("Quantity", "Formula", "Substitution", "Result")):
            cell = t.rows[0].cells[i]
            cell.text = h
            _shade(cell, "1F3A5F")
            for p in cell.paragraphs:
                for r in p.runs:
                    r.bold = True
                    r.font.size = Pt(9)
                    r.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
        for d in data.derived_values:
            cells = t.add_row().cells
            vals = [d.get("label", ""), d.get("expression", ""),
                    d.get("substituted", ""),
                    f"{d.get('result', '')} {d.get('unit', '')}".strip()]
            for i, v in enumerate(vals):
                cells[i].text = str(v)
                for p in cells[i].paragraphs:
                    for r in p.runs:
                        r.font.size = Pt(9)
    else:
        _clause(doc, "4.1", "No derived values were required.")

    # 5. Assessment
    _heading(doc, "5", "Assessment")
    if data.assessment:
        for i, line in enumerate(data.assessment, 1):
            _clause(doc, f"5.{i}", line)
    else:
        _clause(doc, "5.1", "No assessment could be formed from the available "
                            "evidence. [CANNOT DETERMINE]")

    # 6. Compliance
    _heading(doc, "6", "Compliance Statement")
    if data.compliance:
        for i, c in enumerate(data.compliance, 1):
            verdict = str(c.get("verdict", "CANNOT DETERMINE")).upper()
            _clause(doc, f"6.{i}",
                    f"{c.get('detail', '')}", bold_lead=f"{c.get('clause', '')} — "
                                                        f"{verdict}. ")
    else:
        _clause(doc, "6.1", "Compliance could not be assessed. [CANNOT DETERMINE]")

    # 7. Unresolved
    _heading(doc, "7", "Matters That Could Not Be Determined")
    if data.unresolved:
        _clause(doc, "7.1", "The following could not be established from the "
                            "evidence available. This note must not be approved "
                            "until they are resolved.")
        for i, u in enumerate(data.unresolved, 2):
            _clause(doc, f"7.{i}",
                    f"{u.get('item', '')} — required to resolve: "
                    f"{u.get('needed', 'additional source document')}.")
    else:
        _clause(doc, "7.1", "None. Every value in this note is either a source "
                            "value or a recorded calculation.")

    # 8. Recommendation
    _heading(doc, "8", "Recommendation")
    _clause(doc, "8.1", data.recommendation or
            "No recommendation is made pending resolution of clause 7.")

    _signature_block(doc, data)

    # Appendix A — evidence register
    doc.add_page_break()
    _heading(doc, "A", "Appendix A — Evidence Register")
    _clause(doc, "A.1", "Each significant value in this note is classified below. "
                        "SOURCE values appear in a cited document region; DERIVED "
                        "values are reproducible calculations; INTERPRETATION is a "
                        "judgement; CANNOT DETERMINE means the evidence does not "
                        "support the value.")
    verdicts = (data.provenance or {}).get("verdicts", [])
    if verdicts:
        t = doc.add_table(rows=1, cols=4)
        t.style = "Table Grid"
        for i, h in enumerate(("Value", "Class", "Basis", "Context")):
            cell = t.rows[0].cells[i]
            cell.text = h
            _shade(cell, "1F3A5F")
            for p in cell.paragraphs:
                for r in p.runs:
                    r.bold = True
                    r.font.size = Pt(9)
                    r.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
        for v in verdicts:
            cells = t.add_row().cells
            cells[0].text = str(v.get("value", ""))
            cells[1].text = CLASS_LABEL.get(v.get("class", "D"), "UNKNOWN")
            cells[2].text = str(v.get("rationale", ""))[:220]
            cells[3].text = str(v.get("context", ""))[:220]
            for c in cells:
                for p in c.paragraphs:
                    for r in p.runs:
                        r.font.size = Pt(8)
            if v.get("class") == "D":
                _shade(cells[1], "F7D5D0")

    if data.evidence:
        _heading(doc, "B", "Appendix B — Source Extracts")
        for i, e in enumerate(data.evidence[:12], 1):
            _clause(doc, f"B.{i}",
                    f"{e.get('doc_title', 'document')}, page {e.get('page_no', '?')}: "
                    f"“{(e.get('text') or '')[:400].strip()}”")

    _footer(doc, ref)

    out = ARTIFACT_DIR / (filename or f"approval_note_{ref.replace('/', '-')}.docx")
    doc.save(str(out))
    return _register(out, task_id, "approval_note",
                     {"reference_no": ref, "subject": data.subject,
                      "equipment_tag": data.equipment_tag,
                      "unresolved": len(data.unresolved)})


def build_report(title: str, sections: list[dict[str, Any]], *,
                 task_id: str | None = None,
                 filename: str | None = None,
                 subtitle: str = "") -> dict[str, Any]:
    """A general internal report / memo on the same letterhead."""
    doc = Document()
    _styles(doc)
    ref = f"MEMO/{time.strftime('%Y')}/{db.new_id('')[1:6].upper()}"
    _letterhead(doc, ref, time.strftime("%d %B %Y"))

    h = doc.add_paragraph()
    h.alignment = WD_ALIGN_PARAGRAPH.CENTER
    hr = h.add_run(title.upper())
    hr.bold = True
    hr.font.size = Pt(13)
    hr.font.color.rgb = ACCENT
    if subtitle:
        sp = doc.add_paragraph()
        sp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        sr = sp.add_run(subtitle)
        sr.font.size = Pt(10)
        sr.font.color.rgb = MUTED
    doc.add_paragraph()

    for i, sec in enumerate(sections, 1):
        _heading(doc, str(i), sec.get("heading", f"Section {i}"))
        body = sec.get("body", "")
        if isinstance(body, str):
            for j, para in enumerate([p for p in body.split("\n") if p.strip()], 1):
                _clause(doc, f"{i}.{j}", para.strip())
        elif isinstance(body, list):
            for j, item in enumerate(body, 1):
                _clause(doc, f"{i}.{j}", str(item))
        if sec.get("table"):
            _value_table(doc, sec["table"],
                         tuple(sec.get("headers",
                                       ("Parameter", "Value", "Unit", "Source"))))
    _footer(doc, ref)
    out = ARTIFACT_DIR / (filename or f"report_{ref.replace('/', '-')}.docx")
    doc.save(str(out))
    return _register(out, task_id, "report", {"title": title, "reference_no": ref})


def _register(path: Path, task_id: str | None, kind: str,
              meta: dict[str, Any]) -> dict[str, Any]:
    import hashlib
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    aid = db.new_id("art")
    db.insert("artifacts", {
        "id": aid, "task_id": task_id, "name": path.name, "kind": kind,
        "path": str(path), "bytes": len(data), "sha256": digest,
        "meta": db.jdump(meta), "created_at": time.time()})
    return {"artifact_id": aid, "name": path.name, "path": str(path),
            "bytes": len(data), "sha256": digest, "kind": kind, **meta}
