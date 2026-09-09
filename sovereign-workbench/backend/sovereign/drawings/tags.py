"""Tag recognition shared by the vector and raster paths.

Equipment tags on a P&ID are not plain words. Instrument balloons split the tag
across two lines ("PI" above "204"); OCR frequently loses the hyphen or splits
"V-204" into "V", "-", "204"; and the sheet is full of numbers that are not tags
at all. Recognition therefore has three stages: recombine fragments that are
geometrically one label, match a tolerant pattern, then type the tag by prefix.

Both extractors use this so the two paths cannot drift apart.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

from .model import BBox, TextTag

# Canonical form: PREFIX-NUMBER[SUFFIX]. The separator is optional because OCR
# drops thin hyphens routinely.
TAG_RE = re.compile(r"^([A-Z]{1,4})\s*[-–—_]?\s*(\d{2,4})\s*([A-Z])?$")
TAG_IN_TEXT_RE = re.compile(r"\b([A-Z]{1,4})\s*[-–—]\s*(\d{2,4})([A-Z])?\b")
LINE_PREFIXES = {"L", "LN", "PL"}

INSTRUMENT_PREFIXES = {
    "PI", "TI", "LI", "FI", "AI", "PT", "TT", "LT", "FT", "AT",
    "PG", "TG", "LG", "FG", "PC", "TC", "LC", "FC",
    "PSH", "PSL", "LSH", "LSL", "TSH", "TSL", "FSH", "FSL", "PDI", "PDT",
}
EQUIPMENT_PREFIXES = {
    "V", "D", "T", "C", "P", "E", "HX", "K", "R", "F", "GA", "TK", "S",
    "PSV", "PRV", "RV", "HV", "GV", "BV", "CV", "XV", "FV", "LV", "PV", "TV",
}


def classify_prefix(prefix: str) -> str:
    if prefix in LINE_PREFIXES:
        return "line"
    if prefix in INSTRUMENT_PREFIXES:
        return "instrument"
    if prefix in EQUIPMENT_PREFIXES:
        return "equipment"
    return "note"


def _merge_fragments(items: list[tuple[str, BBox, float]]
                     ) -> list[tuple[str, BBox, float]]:
    """Recombine label fragments that geometry says are a single tag.

    Two cases matter in practice:
      * a balloon two-liner -- alphabetic fragment directly above a numeric one,
        sharing an x-centre;
      * a hyphen-split tag -- "V", "-", "204" on one baseline.
    """
    out: list[tuple[str, BBox, float]] = []
    used: set[int] = set()

    for i, (t1, b1, c1) in enumerate(items):
        if i in used:
            continue
        a1 = t1.strip().strip(":,;.")

        # -- vertical balloon stack
        matched = False
        for j, (t2, b2, c2) in enumerate(items):
            if j == i or j in used:
                continue
            a2 = t2.strip().strip(":,;.")
            gap = b2.y0 - b1.y1
            if (a1.isalpha() and a2.isdigit() and len(a1) <= 4 and len(a2) <= 4
                    and abs(b1.cx - b2.cx) <= max(10.0, b1.w * 0.9)
                    and -2.0 < gap < max(12.0, b1.h * 1.4)):
                out.append((f"{a1}-{a2}", b1.merge(b2), min(c1, c2)))
                used.update({i, j})
                matched = True
                break
        if matched:
            continue

        # -- horizontal hyphen split on one baseline
        run = [a1]
        boxes = b1
        conf = c1
        consumed = [i]
        cursor = b1
        for _ in range(3):
            nxt = None
            for j, (t2, b2, c2) in enumerate(items):
                if j in used or j in consumed:
                    continue
                if (abs(b2.cy - cursor.cy) <= max(4.0, cursor.h * 0.6)
                        and 0 <= b2.x0 - cursor.x1 <= max(9.0, cursor.h * 1.1)):
                    if nxt is None or b2.x0 < items[nxt][1].x0:
                        nxt = j
            if nxt is None:
                break
            t2, b2, c2 = items[nxt]
            run.append(t2.strip().strip(":,;."))
            boxes = boxes.merge(b2)
            conf = min(conf, c2)
            consumed.append(nxt)
            cursor = b2
        joined = "".join(run)
        if len(consumed) > 1 and TAG_RE.match(joined.upper()):
            out.append((joined, boxes, conf))
            used.update(consumed)
            continue

        out.append((a1, b1, c1))
        used.add(i)
    return out


def recognise(items: Iterable[tuple[str, BBox, float]], *,
              id_prefix: str = "tag", min_confidence: float = 0.0
              ) -> list[TextTag]:
    """Turn positioned words into typed, canonical tags."""
    merged = _merge_fragments([(t, b, c) for t, b, c in items if t.strip()])
    tags: list[TextTag] = []
    seen: set[tuple[str, int, int]] = set()

    for text, box, conf in merged:
        if conf < min_confidence:
            continue
        clean = text.strip().strip(":,;.()").upper()
        m = TAG_RE.match(clean) or TAG_IN_TEXT_RE.search(clean)
        if not m:
            continue
        prefix, number, suffix = m.group(1), m.group(2), (m.group(3) or "")
        full = f"{prefix}-{number}{suffix}"
        key = (full, int(box.cx // 6), int(box.cy // 6))
        if key in seen:
            continue
        seen.add(key)
        tags.append(TextTag(id=f"{id_prefix}-{len(tags):03d}", text=full,
                            bbox=box, tag_type=classify_prefix(prefix),
                            confidence=conf))
    return tags
