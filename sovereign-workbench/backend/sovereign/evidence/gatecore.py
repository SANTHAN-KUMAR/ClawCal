"""The provenance gate's rules, as pure functions (sovereign-workbench-v2.md §13).

One implementation, two places. On the node, `evidence.gate` feeds these the
spans the node served to a session. On a detached device holding an exported
instrument slice (§14), the client feeds them the slice's spans — the same
rule, the same code, vendored into the client package. Standard library only.

    every number, equipment tag and date must resolve to a span id in the
    served set, and the value must appear in that span's text; otherwise it is
    STRIPPED and reported.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

# Numbers that are structurally not claims: clause references, dates, document
# numbers, page pointers, enumerations. Matching these as "unsupported facts"
# would make the checker cry wolf until an operator switched it off.
_IGNORE_CONTEXT = re.compile(
    r"(clause|section|para(graph)?|rev(ision)?|page|p\.|item|step|note|table|"
    r"figure|annex|appendix|sop-|ir-|psv/|no\.|ref|dwg|drawing)\s*[:\-]?\s*$",
    re.I)

# A "significant" number: a decimal or integer, optionally with a unit.
_NUMBER = re.compile(
    r"(?<![\w.\-/])(\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d*\.\d+|\d+)\s*"
    r"(mm|cm|m|bar\s*g|bar|kpa|mpa|psi|deg\s*c|°c|c|years?|yrs?|months?|%|"
    r"mm/yr|mm/year|kg|tonnes?|hours?)?(?!\w)(?!\.\d)",
    re.I)

def _values_in(text: str) -> set[str]:
    """All numeric values present in a body of text, normalised for comparison."""
    vals = set()
    for m in re.finditer(r"\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d*\.\d+|\d+", text):
        try:
            vals.add(f"{float(m.group(0).replace(',', '')):g}")
        except ValueError:
            continue
    return vals


STRIPPED_MARK = "[CANNOT DETERMINE — not in a served span]"

# Equipment tags and document identifiers: V-204, PSV-1101, P-101A, IR-2026-0731.
_TAG = re.compile(r"\b[A-Z]{1,5}-\d{2,6}[A-Z]?(?:-[A-Z0-9]{1,6})*\b")
_DATE_PATTERNS = [
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), ("%Y-%m-%d",)),
    (re.compile(r"\b(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"
                r"[a-z]*\.?\s+(\d{4})\b", re.I), ("%d %b %Y",)),
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"), ("%d/%m/%Y", "%m/%d/%Y")),
    (re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b"), ("%d.%m.%Y",)),
]


@dataclass
class Value:
    raw: str
    kind: str                     # number | tag | date
    start: int
    end: int
    norm: set[str] = field(default_factory=set)


def _dates(text: str) -> list[Value]:
    out = []
    for pat, fmts in _DATE_PATTERNS:
        for m in pat.finditer(text):
            raw = m.group(0)
            norm = set()
            cleaned = re.sub(r"(?i)\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
                             r"[a-z]*\.?", lambda x: x.group(1)[:3].title(), raw)
            for f in fmts:
                try:
                    norm.add(datetime.strptime(cleaned, f).date().isoformat())
                except ValueError:
                    continue
            if norm:
                out.append(Value(raw, "date", m.start(), m.end(), norm))
    return out


def values_in(text: str) -> list[Value]:
    """Every checkable value in a claim: numbers, tags, dates."""
    vals: list[Value] = []
    dates = _dates(text)
    vals += dates
    spans = [(d.start, d.end) for d in dates]
    for m in _TAG.finditer(text):
        if any(a <= m.start() < b for a, b in spans):
            continue
        vals.append(Value(m.group(0), "tag", m.start(), m.end(), {m.group(0).upper()}))
        spans.append((m.start(), m.end()))
    # Not extract_numbers: that skips a number beside an identifier
    # so prose checking does not cry wolf, and at a gate an unchecked number
    # is a hole. Tags and dates are already claimed above; clause and page
    # references and bare counts are still not figures.
    for m in _NUMBER.finditer(text):
        if any(a <= m.start() < b for a, b in spans):
            continue
        raw, unit = m.group(1), (m.group(2) or "").strip()
        if not unit and "." not in raw:
            continue
        if _IGNORE_CONTEXT.search(text[max(0, m.start() - 40):m.start()]):
            continue
        try:
            norm = f"{float(raw.replace(',', '')):g}"
        except ValueError:
            continue
        vals.append(Value(m.group(0).strip(), "number", m.start(), m.end(), {norm}))
    return sorted(vals, key=lambda v: v.start)


def _span_values(text: str, title: str | None = None) -> dict[str, set[str]]:
    # A document's own identifier (the report number in its title) is
    # established by that document, so tags may resolve against the served
    # span's title as well as its text. Numbers and dates may not: a figure
    # must be on the page.
    return {"number": _values_in(text),
            "tag": {m.group(0).upper() for m in _TAG.finditer(
                text + "\n" + (title or ""))},
            "date": {n for v in _dates(text) for n in v.norm}}


@dataclass
class ClaimResult:
    text_in: str
    text_out: str
    kept: list[dict[str, Any]] = field(default_factory=list)
    stripped: list[dict[str, Any]] = field(default_factory=list)
    unserved_citations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate(claims: list[dict[str, Any]], idx: dict[str, dict[str, Any]],
             calc_idx: dict[str, Any] | None = None
             ) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Gate claims `{"text", "spans"}` against a served set `idx` (span_id ->
    {text, doc_id, doc_title, page_no, region}) and verified calculations
    `calc_idx` (normalised value -> calculation). Returns (claims, counts)."""
    calc_idx = calc_idx or {}
    span_vals = {sid: _span_values(s["text"], s.get("doc_title"))
                 for sid, s in idx.items()}
    results: list[ClaimResult] = []
    counts = {"kept": 0, "stripped": 0, "recited": 0, "derived": 0}

    for c in claims:
        text = str(c.get("text") or "")
        cited = [str(s) for s in (c.get("spans") or [])]
        res = ClaimResult(text_in=text, text_out=text,
                          unserved_citations=[s for s in cited if s not in idx])
        cited_ok = [s for s in cited if s in idx]
        replacements: list[tuple[int, int]] = []
        for v in values_in(text):
            hit = next((s for s in cited_ok if v.norm & span_vals[s][v.kind]), None)
            how = "cited"
            if hit is None:
                hit = next((s for s in idx if v.norm & span_vals[s][v.kind]), None)
                how = "recited"
            if hit is not None:
                s = idx[hit]
                res.kept.append({"value": v.raw, "kind": v.kind, "span_id": hit,
                                 "doc_id": s["doc_id"], "doc_title": s["doc_title"],
                                 "page_no": s["page_no"], "region": s["region"],
                                 "basis": how, "class": "A"})
                counts["kept"] += 1
                counts["recited"] += how == "recited"
                continue
            if v.kind == "number" and (v.norm & set(calc_idx)):
                calc = calc_idx[next(iter(v.norm & set(calc_idx)))]
                res.kept.append({"value": v.raw, "kind": v.kind, "span_id": None,
                                 "calc_id": calc["id"],
                                 "expression": calc.get("expression"),
                                 "basis": "derived", "class": "B"})
                counts["kept"] += 1
                counts["derived"] += 1
                continue
            why = ("its citation was never served to this session"
                   if cited and not cited_ok else
                   f"no span served to this session contains this {v.kind}")
            res.stripped.append({"value": v.raw, "kind": v.kind, "reason": why,
                                 "class": "D"})
            counts["stripped"] += 1
            replacements.append((v.start, v.end))
        for a, b in sorted(replacements, reverse=True):
            res.text_out = res.text_out[:a] + STRIPPED_MARK + res.text_out[b:]
        results.append(res)

    return [r.to_dict() for r in results], counts


# ------------------------------------------------ drafts into claims

# "(span_id=doc-1:x)", "[span_ids: a, b]", "(span_id: `doc-1:x`)" — models
# quote and backtick ids freely; all of these are citations.
_Q = "[`'\"]?"
_INLINE_SPAN = re.compile(
    r"\s*[\(\[]?\s*span[_ ]?ids?\s*[=:]\s*" + _Q
    + r"([\w\-.:/]+(?:" + _Q + r"\s*,\s*" + _Q + r"[\w\-.:/]+)*)" + _Q
    + r"\s*[\)\]]?", re.I)
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(])")


def _claim(item: Any) -> dict[str, Any] | None:
    """One claim from whatever a model sent: a {text, spans} object or a bare
    sentence. Citations written inline — "(span_id=doc-1:nominal_thickness)" —
    are lifted into `spans` and removed from the rendered text."""
    if isinstance(item, dict):
        text = str(item.get("text") or item.get("claim") or item.get("content") or "")
        spans = [str(x) for x in (item.get("spans") or item.get("span_ids") or [])]
    else:
        text, spans = str(item or ""), []
    for m in _INLINE_SPAN.finditer(text):
        spans += [x.strip(" `'\"") for x in m.group(1).split(",")
                  if x.strip(" `'\"")]
    text = re.sub(r"\s+([.,;])", r"\1", _INLINE_SPAN.sub("", text)).strip()
    return {"text": text, "spans": list(dict.fromkeys(spans))} if text else None


def normalise_sections(raw: Any) -> list[dict[str, Any]]:
    """Sections in the shape the gate reads, from the shapes models produce:
    heading|title|name, and claims (objects or strings) or a prose
    content|body|text that is split into sentences, one claim each."""
    out = []
    for i, s in enumerate(raw if isinstance(raw, list) else []):
        if not isinstance(s, dict):
            continue
        heading = str(s.get("heading") or s.get("title") or s.get("name")
                      or f"Section {i + 1}")
        items = s.get("claims")
        if not isinstance(items, list):
            prose = s.get("content") or s.get("body") or s.get("text") or ""
            items = [p for p in _SENTENCE.split(str(prose)) if p.strip()] \
                if isinstance(prose, str) else list(prose or [])
        claims = [c for c in (_claim(x) for x in items) if c]
        if claims:
            out.append({"heading": heading, "claims": claims})
    return out
