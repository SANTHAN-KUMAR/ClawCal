"""Deterministic key-value extraction from industrial documents.

Retrieval hands a model a passage and hopes it reads the right figure off it.
For prose that is fine; for an equipment particulars block it is not. A 7B model
asked to read "Nominal Thickness : 12.0 mm" out of an OCR'd table will, often
enough to matter, produce 12.4 -- and a wrong wall thickness propagates into a
corrosion rate, a remaining life, and an approval decision.

So the values that carry engineering weight are extracted here instead, by
pattern, with the exact source line, page and region attached. The model is then
told what the document says rather than asked to read it. OCR noise is handled
explicitly: the separator between label and value is frequently mangled (":"
becomes "©", "=" or nothing at all), and labels are abbreviated inconsistently,
so matching is fuzzy on the label and strict on the value.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from .. import db

# Separators Tesseract produces for a colon on a degraded scan.
_SEP = r"[:;=©®|·•\-]{0,2}"
_NUM = r"[-+]?\d{1,6}(?:[.,]\d{1,4})?"
_UNIT = (r"(?:mm/yr|mm/year|mm|cm|m|bar\s*g|barg|bar|kpa|mpa|psi|deg\s*c|°c|"
         r"years?|yrs?|months?|%|kg|mm2|m2|hours?|hrs?)")

LINE_RE = re.compile(
    rf"^\s*(?P<label>[A-Za-z][A-Za-z0-9 ./()'&+-]{{2,48}}?)\s*{_SEP}\s*"
    rf"(?P<value>{_NUM})\s*(?P<unit>{_UNIT})?\s*$",
    re.I | re.M)

TEXT_RE = re.compile(
    rf"^\s*(?P<label>[A-Za-z][A-Za-z0-9 ./()'&+-]{{2,48}}?)\s*{_SEP}\s*"
    rf"(?P<value>[A-Za-z0-9][^\n]{{0,70}}?)\s*$",
    re.I | re.M)


def _norm(label: str) -> str:
    s = unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()
    return re.sub(r"\s+", " ", s)


# Canonical field names and the label variants seen in the field, including the
# truncations a fixed-width report template produces ("Minimum Required Thick").
FIELD_ALIASES: dict[str, list[str]] = {
    "equipment_tag": ["equipment tag", "tag", "tag no", "equipment no", "item no"],
    "description": ["description", "service", "equipment description"],
    "registration_no": ["registration no", "registration", "register no"],
    "year_of_commissioning": ["year of commissioning", "commissioned",
                              "year commissioned", "installation year"],
    "nominal_thickness": ["nominal thickness", "nominal thick", "original thickness",
                          "as built thickness", "t nom"],
    "corrosion_allowance": ["corrosion allowance", "ca", "corr allowance"],
    "minimum_required_thickness": ["minimum required thickness",
                                   "minimum required thick", "min required thickness",
                                   "minimum thickness", "t min", "retirement thickness"],
    "design_pressure": ["design pressure", "design press", "mawp",
                        "maximum allowable working pressure"],
    "design_temperature": ["design temperature", "design temp"],
    "operating_pressure": ["operating pressure", "operating press", "working pressure"],
    "operating_temperature": ["operating temperature", "operating temp"],
    "previous_thickness": ["thickness recorded", "previous thickness",
                           "last recorded thickness"],
    "previous_inspection_date": ["previous inspection date", "last inspection date"],
    "previous_inspection_ref": ["previous inspection ref", "previous inspection"],
    "governing_thickness": ["governing minimum reading", "governing reading",
                            "governing minimum", "minimum reading",
                            "governing thickness"],
    "report_no": ["report no", "report number", "ref no", "reference no"],
    "report_date": ["date", "report date", "inspection date"],
    "inspector": ["inspector", "inspected by", "surveyed by"],
    "procedure": ["procedure", "as per", "carried out under"],
    "unit": ["unit", "plant unit", "area"],
    "set_pressure": ["set pressure", "set point", "set"],
}

_ALIAS_INDEX = {_norm(a): canon
                for canon, aliases in FIELD_ALIASES.items()
                for a in aliases}


@dataclass
class Extracted:
    field: str
    label_as_written: str
    value: str
    numeric: float | None
    unit: str
    page_no: int
    line: str
    region: dict[str, Any] | None = None
    confidence: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {"field": self.field, "label": self.label_as_written,
                "value": self.value, "numeric": self.numeric, "unit": self.unit,
                "page": self.page_no, "source_line": self.line,
                "region": self.region, "confidence": round(self.confidence, 2)}


def _canonical(label: str) -> tuple[str | None, float]:
    n = _norm(label)
    if not n:
        return None, 0.0
    if n in _ALIAS_INDEX:
        return _ALIAS_INDEX[n], 1.0
    # Containment, longest alias first, so "minimum required thick" beats "thick".
    best: tuple[str, float] | None = None
    for alias, canon in sorted(_ALIAS_INDEX.items(), key=lambda kv: -len(kv[0])):
        if alias in n or n in alias:
            score = min(len(alias), len(n)) / max(len(alias), len(n))
            if score >= 0.55 and (best is None or score > best[1]):
                best = (canon, score)
    return best if best else (None, 0.0)


def extract_document(doc_id: str, *, fields: list[str] | None = None
                     ) -> dict[str, Any]:
    """Extract labelled values from every page of an indexed document."""
    from .ocr import Word, region_for_span

    pages = db.query(
        "SELECT page_no, text, words FROM pages WHERE doc_id=? ORDER BY page_no",
        (doc_id,))
    doc = db.query_one("SELECT title FROM documents WHERE id=?", (doc_id,))
    wanted = {f.lower() for f in fields} if fields else None

    found: dict[str, Extracted] = {}
    unmatched: list[dict[str, Any]] = []

    for pg in pages:
        text = pg["text"] or ""
        raw_words = db.jload(pg["words"], []) or []
        words = [Word(w["t"], *w["b"], w.get("c", 100.0)) for w in raw_words]

        for rx, numeric_only in ((LINE_RE, True), (TEXT_RE, False)):
            for m in rx.finditer(text):
                label = m.group("label").strip()
                canon, score = _canonical(label)
                raw_value = re.sub(r"^[\s:;=©®|·•-]+", "",
                                   m.group("value").strip()).strip()
                unit = (m.group("unit") or "").strip() if numeric_only else ""

                if not numeric_only and re.fullmatch(rf"{_NUM}\s*{_UNIT}?", raw_value,
                                                     re.I):
                    continue                 # already captured by the numeric pass

                num: float | None = None
                if numeric_only:
                    try:
                        num = float(raw_value.replace(",", "."))
                    except ValueError:
                        continue

                entry = Extracted(
                    field=canon or _norm(label).replace(" ", "_"),
                    label_as_written=label, value=raw_value, numeric=num, unit=unit,
                    page_no=pg["page_no"], line=m.group(0).strip(),
                    region=region_for_span(words, m.group(0).strip()[:90]),
                    confidence=score if canon else 0.5)

                if canon is None:
                    unmatched.append(entry.to_dict())
                    continue
                if wanted and canon.lower() not in wanted:
                    continue
                # A numeric reading beats a text one, and a better label match wins.
                prev = found.get(canon)
                if prev is None or (entry.numeric is not None and prev.numeric is None) \
                        or entry.confidence > prev.confidence:
                    found[canon] = entry

    return {
        "doc_id": doc_id,
        "title": doc["title"] if doc else doc_id,
        "fields": {k: v.to_dict() for k, v in sorted(found.items())},
        "unrecognised_labels": unmatched[:40],
        "field_count": len(found),
    }


def extract_thickness_survey(doc_id: str) -> dict[str, Any]:
    """Pull the thickness reading table, which is positional rather than labelled."""
    rows: list[dict[str, Any]] = []
    for pg in db.query("SELECT page_no, text FROM pages WHERE doc_id=? "
                       "ORDER BY page_no", (doc_id,)):
        in_table = False
        for line in (pg["text"] or "").splitlines():
            if re.search(r"thickness survey|reading\s*\(mm\)|location\s+reading",
                         line, re.I):
                in_table = True
                continue
            if in_table:
                if re.match(r"^\s*\d+\.\s+[A-Z]", line) or not line.strip():
                    if rows:
                        in_table = False
                    continue
                m = re.match(rf"^\s*(?P<loc>[A-Za-z][A-Za-z0-9 ]{{2,40}}?)\s+"
                             rf"(?P<val>{_NUM})\s*$", line)
                if m:
                    try:
                        rows.append({"location": m.group("loc").strip(),
                                     "reading_mm": float(m.group("val")),
                                     "page": pg["page_no"]})
                    except ValueError:
                        pass
    governing = min(rows, key=lambda r: r["reading_mm"]) if rows else None
    return {"readings": rows, "count": len(rows), "governing": governing}
