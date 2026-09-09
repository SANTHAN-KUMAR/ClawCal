"""Evidence and provenance engine.

The rule this enforces is the one that separates an instrument from a chat
assistant: *an important value must be sourced, deterministically derived from
sourced inputs, or explicitly marked as unsupported.*

Four evidence classes:

    A  SOURCE          directly present in a verified document region
    B  DERIVED         computed by the calculator from Class A/B inputs
    C  INTERPRETATION  a judgement made from evidence, labelled as such
    D  UNSUPPORTED     not establishable -- must be refused, never guessed

The check runs over generated text *before* it reaches a deliverable, so an
unsupported number cannot end up in a signed document.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .. import db

# Numbers that are structurally not claims: clause references, dates, document
# numbers, page pointers, enumerations. Matching these as "unsupported facts"
# would make the checker cry wolf until an operator switched it off.
_IGNORE_CONTEXT = re.compile(
    r"(clause|section|para(graph)?|rev(ision)?|page|p\.|item|step|note|table|"
    r"figure|annex|appendix|sop-|ir-|psv/|no\.|ref|dwg|drawing)\s*[:\-]?\s*$",
    re.I)
_DATE_LIKE = re.compile(
    r"\b\d{1,2}\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{4}\b|"
    r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b", re.I)
_IDENT_LIKE = re.compile(r"\b[A-Z]{1,4}[-/]\d{2,6}(?:[-/][A-Z0-9]+)*\b")

# A "significant" number: a decimal or integer, optionally with a unit.
_NUMBER = re.compile(
    r"(?<![\w.\-/])(\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d*\.\d+|\d+)\s*"
    r"(mm|cm|m|bar\s*g|bar|kpa|mpa|psi|deg\s*c|°c|c|years?|yrs?|months?|%|"
    r"mm/yr|mm/year|kg|tonnes?|hours?)?(?!\w)(?!\.\d)",
    re.I)

UNITS_REQUIRING_PROOF = {
    "mm", "cm", "m", "bar", "bar g", "kpa", "mpa", "psi", "deg c", "°c",
    "year", "years", "yr", "yrs", "mm/yr", "mm/year", "%", "kg", "tonne",
    "tonnes", "hour", "hours", "month", "months",
}

CLASS_NAMES = {"A": "SOURCE", "B": "DERIVED", "C": "INTERPRETATION",
               "D": "UNSUPPORTED"}


@dataclass
class NumberMention:
    raw: str
    value: float
    unit: str
    start: int
    end: int
    context: str

    @property
    def normalised(self) -> str:
        return f"{self.value:g}"


@dataclass
class Verdict:
    mention: NumberMention
    ev_class: str
    rationale: str
    evidence_ids: list[str] = field(default_factory=list)
    calc_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.mention.raw, "unit": self.mention.unit,
                "context": self.mention.context, "class": self.ev_class,
                "class_name": CLASS_NAMES[self.ev_class],
                "rationale": self.rationale, "evidence_ids": self.evidence_ids,
                "calc_id": self.calc_id, "start": self.mention.start,
                "end": self.mention.end}


@dataclass
class ProvenanceReport:
    verdicts: list[Verdict]
    ok: bool
    unsupported: list[Verdict] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        c = {"A": 0, "B": 0, "C": 0, "D": 0}
        for v in self.verdicts:
            c[v.ev_class] += 1
        return c

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "counts": self.counts,
                "verdicts": [v.to_dict() for v in self.verdicts],
                "unsupported": [v.to_dict() for v in self.unsupported]}


def extract_numbers(text: str) -> list[NumberMention]:
    out: list[NumberMention] = []
    for m in _NUMBER.finditer(text):
        raw, unit = m.group(1), (m.group(2) or "").strip().lower()
        span_before = text[max(0, m.start() - 40):m.start()]
        whole = m.group(0)

        # Skip identifiers, dates and structural references.
        if _IGNORE_CONTEXT.search(span_before):
            continue
        window = text[max(0, m.start() - 30):min(len(text), m.end() + 30)]
        if _DATE_LIKE.search(window) and not unit:
            continue
        if _IDENT_LIKE.search(text[max(0, m.start() - 8):m.end() + 8]):
            continue
        # A bare integer with no unit is usually a count or a clause number.
        if not unit and "." not in raw:
            continue
        try:
            value = float(raw.replace(",", ""))
        except ValueError:
            continue
        if unit and unit.replace(" ", " ").strip() not in UNITS_REQUIRING_PROOF \
                and unit not in ("bar g",):
            # Unknown unit -- still treat as a claim, better to over-check.
            pass
        out.append(NumberMention(
            raw=whole.strip(), value=value, unit=unit, start=m.start(), end=m.end(),
            context=text[max(0, m.start() - 70):min(len(text), m.end() + 70)]
                     .replace("\n", " ").strip()))
    return out


def _values_in(text: str) -> set[str]:
    """All numeric values present in a body of text, normalised for comparison."""
    vals = set()
    for m in re.finditer(r"\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d*\.\d+|\d+", text):
        try:
            vals.add(f"{float(m.group(0).replace(',', '')):g}")
        except ValueError:
            continue
    return vals


@dataclass
class EvidenceContext:
    """Everything the checker is allowed to treat as established."""
    passages: list[dict[str, Any]] = field(default_factory=list)
    calculations: list[dict[str, Any]] = field(default_factory=list)

    def source_index(self) -> dict[str, list[dict[str, Any]]]:
        idx: dict[str, list[dict[str, Any]]] = {}
        for p in self.passages:
            for v in _values_in(p.get("text", "")):
                idx.setdefault(v, []).append(p)
        return idx

    def calc_index(self) -> dict[str, list[dict[str, Any]]]:
        """Results of calculations whose inputs were themselves established.

        Provenance has to propagate: a calculation over an invented input
        produces an invented result, and stamping it DERIVED would turn the
        calculator into a way of laundering a hallucinated number into a trusted
        one. Only calculations with verified inputs confer Class B.
        """
        idx: dict[str, list[dict[str, Any]]] = {}
        for c in self.calculations:
            steps = c.get("steps") or {}
            if isinstance(steps, dict) and steps.get("inputs_verified") is False:
                continue
            try:
                v = f"{float(c.get('result')):g}"
            except (TypeError, ValueError):
                continue
            idx.setdefault(v, []).append(c)
        return idx

    def tainted_results(self) -> dict[str, list[str]]:
        """Results of calculations that ran on unestablished inputs."""
        out: dict[str, list[str]] = {}
        for c in self.calculations:
            steps = c.get("steps") or {}
            if not isinstance(steps, dict) or steps.get("inputs_verified") is not False:
                continue
            try:
                v = f"{float(c.get('result')):g}"
            except (TypeError, ValueError):
                continue
            out.setdefault(v, []).extend(steps.get("unverified_inputs") or [])
        return out


# Hedged language marks a sentence as interpretation rather than assertion.
_HEDGE = re.compile(
    r"\b(may|might|could|appears?|suggests?|indicat\w+|likely|approximately|"
    r"about|estimated|consider\w*|recommend\w*|should|in the assessor'?s? view|"
    r"is judged|would)\b", re.I)


def classify_text(text: str, ctx: EvidenceContext, *,
                  task_id: str | None = None,
                  persist: bool = True) -> ProvenanceReport:
    """Classify every significant number in `text` against the evidence context."""
    src = ctx.source_index()
    calc = ctx.calc_index()
    tainted = ctx.tainted_results()
    verdicts: list[Verdict] = []

    for mention in extract_numbers(text):
        key = mention.normalised
        sentence = _sentence_around(text, mention.start)

        if key in calc:
            c = calc[key][0]
            verdicts.append(Verdict(
                mention, "B",
                f"matches calculation {c.get('id')} ({c.get('expression')} = "
                f"{c.get('result')})", calc_id=c.get("id")))
            continue

        if key in tainted and key not in calc:
            verdicts.append(Verdict(
                mention, "D",
                f"produced by a calculation whose inputs were not established by "
                f"any source: {', '.join(sorted(set(tainted[key])))[:160]}"))
            continue

        if key in src:
            hits = src[key][:3]
            cites = "; ".join(f"{h.get('doc_title')} p.{h.get('page_no')}"
                              for h in hits)
            verdicts.append(Verdict(
                mention, "A", f"present in retrieved source: {cites}",
                evidence_ids=[h.get("chunk_id", "") for h in hits]))
            continue

        if _HEDGE.search(sentence):
            verdicts.append(Verdict(
                mention, "C",
                "appears in a hedged statement, so it is recorded as an "
                "interpretation rather than a source fact"))
            continue

        verdicts.append(Verdict(
            mention, "D",
            "not present in any retrieved source passage and not produced by a "
            "recorded calculation"))

    unsupported = [v for v in verdicts if v.ev_class == "D"]
    report = ProvenanceReport(verdicts=verdicts, ok=not unsupported,
                              unsupported=unsupported)

    if persist and task_id:
        for v in verdicts:
            db.insert("claims", {
                "id": db.new_id("claim"), "task_id": task_id, "entity": None,
                "attribute": None, "value": v.mention.raw, "unit": v.mention.unit,
                "statement": v.mention.context[:600], "ev_class": v.ev_class,
                "evidence_ids": db.jdump(v.evidence_ids), "calc_id": v.calc_id,
                "confidence": {"A": 0.95, "B": 0.99, "C": 0.6, "D": 0.0}[v.ev_class],
                "rationale": v.rationale, "created_at": db.now()})
    return report


def _sentence_around(text: str, pos: int) -> str:
    start = max(text.rfind(".", 0, pos), text.rfind("\n", 0, pos)) + 1
    end = min([x for x in (text.find(".", pos), text.find("\n", pos)) if x != -1]
              or [len(text)])
    return text[start:end + 1].strip()


def annotate(text: str, report: ProvenanceReport) -> str:
    """Inline class markers, for the trace view and for document footnotes."""
    out, last = [], 0
    for v in sorted(report.verdicts, key=lambda v: v.mention.start):
        out.append(text[last:v.mention.end])
        # The class letter (A/B/C/D), matching the documented scheme and the
        # evidence table in a generated deliverable. Using the class *name's*
        # initial instead would print S/D/I/U and quietly diverge from every
        # other surface that reports provenance.
        out.append(f" [{v.ev_class}]")
        last = v.mention.end
    out.append(text[last:])
    return "".join(out)


def redact_unsupported(text: str, report: ProvenanceReport) -> str:
    """Replace unsupported values with an explicit refusal.

    This is the enforcement point: rather than deleting the sentence and hiding
    the gap, the gap is made loud.
    """
    if report.ok:
        return text
    out, last = [], 0
    for v in sorted(report.unsupported, key=lambda v: v.mention.start):
        out.append(text[last:v.mention.start])
        out.append("[CANNOT DETERMINE - not supported by available evidence]")
        last = v.mention.end
    out.append(text[last:])
    return "".join(out)


def record_claim(task_id: str, *, entity: str, attribute: str, value: str,
                 unit: str = "", statement: str = "", ev_class: str = "A",
                 evidence_ids: Iterable[str] = (), calc_id: str | None = None,
                 rationale: str = "") -> str:
    """Record a structured Entity -> Attribute -> Value -> Document -> Page claim."""
    cid = db.new_id("claim")
    db.insert("claims", {
        "id": cid, "task_id": task_id, "entity": entity, "attribute": attribute,
        "value": value, "unit": unit, "statement": statement or
        f"{entity} {attribute} = {value} {unit}".strip(),
        "ev_class": ev_class, "evidence_ids": db.jdump(list(evidence_ids)),
        "calc_id": calc_id,
        "confidence": {"A": 0.95, "B": 0.99, "C": 0.6, "D": 0.0}.get(ev_class, 0.5),
        "rationale": rationale, "created_at": db.now()})
    return cid


def store_passages(task_id: str, passages: list[dict[str, Any]]) -> list[str]:
    """Persist retrieved passages as addressable evidence rows."""
    ids = []
    for p in passages:
        eid = db.new_id("ev")
        db.insert("evidence", {
            "id": eid, "task_id": task_id, "doc_id": p.get("doc_id"),
            "page_no": p.get("page_no"), "region": db.jdump(p.get("region")),
            "snippet": (p.get("text") or "")[:2000], "score": p.get("score", 0.0),
            "kind": "text", "created_at": db.now()})
        ids.append(eid)
    return ids
