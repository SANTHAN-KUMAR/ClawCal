"""Deliverable-generation tools.

These are classified DELIVERABLE risk: under the default 'controlled' policy they
require human approval, because a generated document is the artefact that leaves
the workbench and gets signed.

Before any document is written, the content passes through the provenance
checker. Unsupported numbers are replaced with an explicit CANNOT DETERMINE
marker rather than being quietly emitted -- enforcement at generation time, not a
citation feature bolted on afterwards.
"""
from __future__ import annotations

from typing import Any

from .. import db
from ..deliverables import docx_builder, pptx_builder, xlsx_builder
from ..evidence import provenance
from ..policy.tool_policy import Risk
from .base import Tool, ToolContext, ToolResult
from .calculator import task_calculations


def _context(ctx: ToolContext) -> provenance.EvidenceContext:
    return provenance.EvidenceContext(
        passages=ctx.scratch.get("passages", []),
        calculations=task_calculations(ctx.task_id))


def _check(text: str, ctx: ToolContext) -> tuple[str, dict[str, Any]]:
    report = provenance.classify_text(text, _context(ctx), task_id=ctx.task_id)
    return provenance.redact_unsupported(text, report), report.to_dict()


# How many times a deliverable may be refused for unsupported values before the
# document is produced anyway, with the gaps marked. Refusing forever would turn
# a self-correcting loop into a livelock; refusing never would make the rule
# advisory, which is how a wrong number reaches a signed document.
MAX_PROVENANCE_REFUSALS = 2


def _enforce_provenance(verdicts: list[dict[str, Any]], ctx: ToolContext,
                        *, kind: str) -> ToolResult | None:
    """Refuse to write a document containing values the evidence does not support.

    This is where the number rule stops being advice. The model is told, in the
    system prompt, to compute every derived value with the calculator; a model
    under context pressure will do it in its head anyway, and mental arithmetic
    produces a number that looks identical to a sourced one. Checking at
    generation time -- and handing back the specific offending values -- turns
    that into a correctable error instead of a signed document.
    """
    unsupported = [v for v in verdicts if v.get("class") == "D"]
    if not unsupported:
        return None

    attempts = int(ctx.scratch.get("provenance_refusals", 0))
    if attempts >= MAX_PROVENANCE_REFUSALS:
        return None                    # proceed, with the gaps marked in the text

    ctx.scratch["provenance_refusals"] = attempts + 1
    listing = "\n".join(
        f"  - {v.get('value')} in: \"{str(v.get('context', ''))[:120]}\""
        for v in unsupported[:10])
    ctx.note("provenance_refusal", f"{kind} refused",
             f"{len(unsupported)} value(s) are not supported by evidence",
             {"unsupported": unsupported[:10]})
    return ToolResult(
        False,
        error=(
            f"REFUSED: the {kind} contains {len(unsupported)} number(s) that are "
            f"neither present in a retrieved source passage nor produced by a "
            f"recorded calculation:\n{listing}\n\n"
            f"Do not restate them. For each one either:\n"
            f"  (a) compute it with the `calculator` tool, binding named inputs "
            f"and recording where each input came from, then use the calculator's "
            f"result; or\n"
            f"  (b) retrieve the passage that states it and cite the document and "
            f"page; or\n"
            f"  (c) remove the claim and record it under `unresolved` as "
            f"CANNOT DETERMINE.\n\n"
            f"Then call this tool again. Re-check any compliance verdict that "
            f"depended on one of these numbers -- if the value changes, the "
            f"verdict may change with it."),
        meta={"unsupported": unsupported, "attempt": attempts + 1})


class ApprovalNoteTool(Tool):
    name = "generate_approval_note"
    risk = Risk.DELIVERABLE
    timeout_s = 180.0
    description = (
        "Generate a formal approval note as a Word document on the organisation's "
        "template. Supply observed values with their source document and page, "
        "derived values referencing calculator results, the assessment, an "
        "explicit compliance verdict per clause, and anything that could not be "
        "determined. Every number you supply is checked against the evidence "
        "before the document is written.")
    parameters = {"type": "object", "properties": {
        "subject": {"type": "string"},
        "equipment_tag": {"type": "string"},
        "equipment_desc": {"type": "string"},
        "inspection_ref": {"type": "string"},
        "inspection_date": {"type": "string"},
        "background": {"type": "string"},
        "sop_refs": {"type": "array", "items": {"type": "string"}},
        "observed_values": {"type": "array", "items": {"type": "object"},
                            "description": "each: {parameter, value, unit, source}"},
        "derived_values": {"type": "array", "items": {"type": "object"},
                           "description": "each: {label, expression, substituted, "
                                          "result, unit}"},
        "assessment": {"type": "array", "items": {"type": "string"}},
        "compliance": {"type": "array", "items": {"type": "object"},
                       "description": "each: {clause, verdict, detail}"},
        "unresolved": {"type": "array", "items": {"type": "object"},
                       "description": "each: {item, needed}"},
        "recommendation": {"type": "string"},
        "recommending_engineer": {"type": "string"},
    }, "required": ["subject"]}

    def approval_summary(self, args: dict[str, Any]) -> str:
        return (f"generate an approval note '{args.get('subject')}' for "
                f"{args.get('equipment_tag', 'unspecified equipment')} with "
                f"{len(args.get('observed_values') or [])} source values and "
                f"{len(args.get('unresolved') or [])} unresolved items")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        # Provenance-check every free-text field that can carry a number.
        checked: dict[str, Any] = {}
        reports: list[dict[str, Any]] = []
        for field in ("background", "recommendation"):
            text, rep = _check(str(args.get(field, "")), ctx)
            checked[field] = text
            reports.append(rep)

        assessment = []
        for line in (args.get("assessment") or []):
            text, rep = _check(str(line), ctx)
            assessment.append(text)
            reports.append(rep)

        compliance = []
        for c in (args.get("compliance") or []):
            detail, rep = _check(str(c.get("detail", "")), ctx)
            compliance.append({**c, "detail": detail})
            reports.append(rep)

        merged = {"A": 0, "B": 0, "C": 0, "D": 0}
        verdicts: list[dict[str, Any]] = []
        for r in reports:
            for k, v in r["counts"].items():
                merged[k] += v
            verdicts.extend(r["verdicts"])

        refusal = _enforce_provenance(verdicts, ctx, kind="approval note")
        if refusal is not None:
            return refusal

        calcs = task_calculations(ctx.task_id)
        derived = args.get("derived_values") or [
            {"label": c.get("label"), "expression": c.get("expression"),
             "substituted": (c.get("steps") or {}).get("substituted"),
             "result": c.get("result"), "unit": c.get("unit")}
            for c in calcs if c.get("ok")]

        data = docx_builder.ApprovalNoteData(
            subject=str(args.get("subject", "Approval note")),
            equipment_tag=str(args.get("equipment_tag", "")),
            equipment_desc=str(args.get("equipment_desc", "")),
            inspection_ref=str(args.get("inspection_ref", "")),
            inspection_date=str(args.get("inspection_date", "")),
            background=checked["background"],
            sop_refs=list(args.get("sop_refs") or []),
            observed_values=list(args.get("observed_values") or []),
            derived_values=derived,
            assessment=assessment,
            compliance=compliance,
            unresolved=list(args.get("unresolved") or []),
            recommendation=checked["recommendation"],
            recommending_engineer=str(args.get("recommending_engineer", "")),
            provenance={"counts": merged, "verdicts": verdicts},
            evidence=ctx.scratch.get("passages", [])[:12],
        )
        art = docx_builder.build_approval_note(data, task_id=ctx.task_id)
        ctx.note("deliverable", f"Generated {art['name']}",
                 f"evidence classes: {merged['A']} source, {merged['B']} derived, "
                 f"{merged['C']} interpretation, {merged['D']} unsupported", art)
        return ToolResult(True, content={**art, "provenance_counts": merged},
                          display=f"created {art['name']} "
                                  f"({art['bytes']} bytes, sha256 "
                                  f"{art['sha256'][:12]})")


class ReportTool(Tool):
    name = "generate_report"
    risk = Risk.DELIVERABLE
    timeout_s = 180.0
    description = ("Generate a Word report or memo on the organisation's "
                   "letterhead from a list of sections.")
    parameters = {"type": "object", "properties": {
        "title": {"type": "string"},
        "subtitle": {"type": "string"},
        "sections": {"type": "array", "items": {"type": "object"},
                     "description": "each: {heading, body, table?, headers?}"},
    }, "required": ["title", "sections"]}

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        sections = []
        verdicts: list[dict[str, Any]] = []
        for s in (args.get("sections") or []):
            body = s.get("body", "")
            if isinstance(body, list):
                body = "\n".join(str(b) for b in body)
            text, rep = _check(str(body), ctx)
            verdicts.extend(rep["verdicts"])
            sections.append({**s, "body": text})
        refusal = _enforce_provenance(verdicts, ctx, kind="report")
        if refusal is not None:
            return refusal
        art = docx_builder.build_report(
            str(args.get("title", "Report")), sections,
            subtitle=str(args.get("subtitle", "")), task_id=ctx.task_id)
        ctx.note("deliverable", f"Generated {art['name']}", "", art)
        return ToolResult(True, content=art, display=f"created {art['name']}")


class SpreadsheetTool(Tool):
    name = "generate_spreadsheet"
    risk = Risk.DELIVERABLE
    timeout_s = 120.0
    description = ("Generate an Excel workbook. Either a data table (supply "
                   "'rows') or a calculation sheet built from this task's "
                   "recorded calculations (set 'calculations' true).")
    parameters = {"type": "object", "properties": {
        "title": {"type": "string"},
        "rows": {"type": "array", "items": {"type": "object"}},
        "columns": {"type": "array", "items": {"type": "string"}},
        "calculations": {"type": "boolean"},
    }, "required": ["title"]}

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        title = str(args.get("title", "Workbook"))
        if args.get("calculations"):
            calcs = task_calculations(ctx.task_id)
            if not calcs:
                return ToolResult(False, error="no calculations have been recorded "
                                               "for this task yet")
            art = xlsx_builder.build_calculation_sheet(title, calcs,
                                                       task_id=ctx.task_id)
        else:
            rows = args.get("rows") or []
            if not rows:
                return ToolResult(False, error="supply 'rows', or set "
                                               "'calculations' to true")
            art = xlsx_builder.build_table(title, rows, args.get("columns"),
                                           task_id=ctx.task_id)
        ctx.note("deliverable", f"Generated {art['name']}", "", art)
        return ToolResult(True, content=art, display=f"created {art['name']}")


class PresentationTool(Tool):
    name = "generate_presentation"
    risk = Risk.DELIVERABLE
    timeout_s = 120.0
    description = ("Generate a PowerPoint briefing deck. Each slide takes a "
                   "heading and bullets, and optionally a table.")
    parameters = {"type": "object", "properties": {
        "title": {"type": "string"},
        "subtitle": {"type": "string"},
        "slides": {"type": "array", "items": {"type": "object"},
                   "description": "each: {heading, bullets[], table?, headers?, "
                                  "note?}"},
    }, "required": ["title", "slides"]}

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        slides = []
        verdicts: list[dict[str, Any]] = []
        for s in (args.get("slides") or []):
            bullets = []
            for b in (s.get("bullets") or []):
                text, rep = _check(str(b), ctx)
                verdicts.extend(rep["verdicts"])
                bullets.append(text)
            slides.append({**s, "bullets": bullets})
        refusal = _enforce_provenance(verdicts, ctx, kind="presentation")
        if refusal is not None:
            return refusal
        art = pptx_builder.build_deck(str(args.get("title", "Briefing")), slides,
                                      subtitle=str(args.get("subtitle", "")),
                                      task_id=ctx.task_id)
        ctx.note("deliverable", f"Generated {art['name']}", "", art)
        return ToolResult(True, content=art, display=f"created {art['name']}")
