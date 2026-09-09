"""Vector-native drawing extraction.

CAD-exported P&IDs are almost always vector PDFs, and that changes the problem
completely: the connectivity is *in the file* as exact line geometry. Reading it
directly gives connectivity that can be stated as fact, rather than a plausible
guess from pixels.

The pipeline:

    page.get_drawings()  -> primitive paths
      -> classify each path as a symbol candidate or a line run
      -> merge overlapping symbol candidates into composite symbols
         (a pump is a circle plus a triangle; a drum is a rectangle plus two
          dished-head arcs)
      -> read text with geometry and match tags to symbols
      -> attach line endpoints to symbols, with tolerance, and score each edge
"""
from __future__ import annotations

import math
from typing import Any

from .model import BBox, Polyline, Symbol, TextTag

# Tolerances in PDF points on a typical A3/A4 drawing sheet.
ENDPOINT_TOLERANCE = 9.0          # endpoint within this of a symbol -> attached
ENDPOINT_TOLERANCE_RELAXED = 24.0  # attached, but only PROBABLE
JOIN_TOLERANCE = 3.0              # two line runs whose ends nearly touch
MIN_LINE_LENGTH = 14.0
MAX_SYMBOL_SPAN = 320.0           # anything larger is a border or title block


def _flatten_bezier(p0, p1, p2, p3, steps: int = 6) -> list[tuple[float, float]]:
    pts = []
    for i in range(1, steps + 1):
        t = i / steps
        mt = 1 - t
        x = (mt ** 3 * p0[0] + 3 * mt * mt * t * p1[0]
             + 3 * mt * t * t * p2[0] + t ** 3 * p3[0])
        y = (mt ** 3 * p0[1] + 3 * mt * mt * t * p1[1]
             + 3 * mt * t * t * p2[1] + t ** 3 * p3[1])
        pts.append((x, y))
    return pts


def _path_points(path: dict[str, Any]) -> tuple[list[list[tuple[float, float]]], dict[str, int]]:
    """Flatten a PyMuPDF drawing path into polylines, counting primitive kinds."""
    runs: list[list[tuple[float, float]]] = []
    counts = {"l": 0, "c": 0, "re": 0, "qu": 0}
    cur: list[tuple[float, float]] = []

    def flush() -> None:
        nonlocal cur
        if len(cur) >= 2:
            runs.append(cur)
        cur = []

    for item in path.get("items", []):
        kind = item[0]
        if kind == "l":
            p1, p2 = (item[1].x, item[1].y), (item[2].x, item[2].y)
            counts["l"] += 1
            if cur and math.dist(cur[-1], p1) <= 0.6:
                cur.append(p2)
            else:
                flush()
                cur = [p1, p2]
        elif kind == "c":
            p0 = (item[1].x, item[1].y)
            pts = _flatten_bezier(p0, (item[2].x, item[2].y),
                                  (item[3].x, item[3].y), (item[4].x, item[4].y))
            counts["c"] += 1
            if cur and math.dist(cur[-1], p0) <= 0.6:
                cur.extend(pts)
            else:
                flush()
                cur = [p0, *pts]
        elif kind == "re":
            r = item[1]
            counts["re"] += 1
            flush()
            runs.append([(r.x0, r.y0), (r.x1, r.y0), (r.x1, r.y1),
                         (r.x0, r.y1), (r.x0, r.y0)])
        elif kind == "qu":
            q = item[1]
            counts["qu"] += 1
            flush()
            runs.append([(q.ul.x, q.ul.y), (q.ur.x, q.ur.y),
                         (q.lr.x, q.lr.y), (q.ll.x, q.ll.y), (q.ul.x, q.ul.y)])
    flush()
    return runs, counts


def _bbox_of(points: list[tuple[float, float]]) -> BBox:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return BBox(min(xs), min(ys), max(xs), max(ys))


def _is_closed(points: list[tuple[float, float]], tol: float = 2.0) -> bool:
    return len(points) > 2 and math.dist(points[0], points[-1]) <= tol


def _circularity(points: list[tuple[float, float]], box: BBox) -> float:
    """1.0 for a perfect circle, lower for anything else."""
    if box.w <= 0 or box.h <= 0:
        return 0.0
    aspect = min(box.w, box.h) / max(box.w, box.h)
    r = (box.w + box.h) / 4
    cx, cy = box.cx, box.cy
    if r <= 0:
        return 0.0
    devs = [abs(math.hypot(x - cx, y - cy) - r) / r for x, y in points]
    radial = max(0.0, 1.0 - (sum(devs) / len(devs)) * 3.0)
    return aspect * radial


def _self_crossing(points: list[tuple[float, float]]) -> bool:
    """A bowtie valve symbol is the classic self-crossing polygon."""
    segs = [(points[i], points[i + 1]) for i in range(len(points) - 1)]
    for i in range(len(segs)):
        for j in range(i + 2, len(segs)):
            if i == 0 and j == len(segs) - 1:
                continue
            if _segments_cross(segs[i], segs[j]):
                return True
    return False


def _orient(a, b, c) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _segments_cross(s1, s2) -> bool:
    a, b = s1
    c, d = s2
    d1, d2 = _orient(c, d, a), _orient(c, d, b)
    d3, d4 = _orient(a, b, c), _orient(a, b, d)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def _classify_shape(points: list[tuple[float, float]], counts: dict[str, int],
                    box: BBox) -> tuple[str, float, str]:
    """Classify one closed primitive. Returns (class, confidence, primitive)."""
    circ = _circularity(points, box)
    span = max(box.w, box.h)

    if counts["re"] and circ < 0.75:
        aspect = box.w / box.h if box.h else 99
        if aspect > 0.28 and aspect < 3.6 and box.h > box.w * 1.6:
            return "column", 0.72, "rectangle-tall"
        return "vessel", 0.70, "rectangle"

    if circ >= 0.80:
        if span <= 38:
            return "instrument", 0.74, "circle-small"
        if span <= 60:
            return "pump", 0.60, "circle-medium"
        return "heat_exchanger", 0.58, "circle-large"

    if _self_crossing(points):
        return "valve", 0.78, "bowtie"

    if 3 <= len(set(points)) <= 5 and _is_closed(points):
        return "pump", 0.45, "triangle"

    return "unknown", 0.30, "polygon"


def extract(page: Any) -> dict[str, Any]:
    """Extract symbol candidates, line runs and text tags from a vector page."""
    raw_shapes: list[dict[str, Any]] = []
    lines: list[Polyline] = []
    page_box = BBox(page.rect.x0, page.rect.y0, page.rect.x1, page.rect.y1)

    for pi, path in enumerate(page.get_drawings()):
        dashes = str(path.get("dashes") or "[] 0")
        dashed = dashes not in ("[] 0", "", "None")
        width = float(path.get("width") or 1.0)
        runs, counts = _path_points(path)

        for ri, pts in enumerate(runs):
            box = _bbox_of(pts)
            span = max(box.w, box.h)
            # Sheet border and title block: huge boxes hugging the page edge.
            if span > MAX_SYMBOL_SPAN and box.area > page_box.area * 0.25:
                continue
            closed = _is_closed(pts) or counts["re"] or counts["qu"]

            if closed and span <= MAX_SYMBOL_SPAN and box.area > 30:
                sym_class, conf, prim = _classify_shape(pts, counts, box)
                raw_shapes.append({"box": box, "class": sym_class,
                                   "confidence": conf, "primitive": prim,
                                   "points": pts})
            elif not closed:
                pl = Polyline(id=f"pl-{pi}-{ri}", points=pts, dashed=dashed,
                              width=width)
                if pl.length >= MIN_LINE_LENGTH:
                    lines.append(pl)

    words = [(w[4], BBox(w[0], w[1], w[2], w[3])) for w in page.get_text("words")]
    symbols = _merge_shapes(raw_shapes)
    symbols = _drop_frames(symbols, words, page_box)
    lines = _drop_internal_lines(lines, symbols)
    tags = _extract_tags(page)
    return {"symbols": symbols, "lines": lines, "tags": tags,
            "page_box": page_box}


def _drop_frames(symbols: list[Symbol], words: list[tuple[str, BBox]],
                 page_box: BBox) -> list[Symbol]:
    """Remove title blocks, legends and revision tables.

    These are drawn with exactly the same primitives as a vessel -- a rectangle --
    so geometry alone cannot tell them apart. What distinguishes them is that they
    *enclose text*: a legend box contains a dozen words, a drum contains none.
    """
    kept: list[Symbol] = []
    for s in symbols:
        enclosed = sum(1 for _t, b in words
                       if s.bbox.contains(b.cx, b.cy) and s.bbox.area > b.area * 4)
        big = s.bbox.area > page_box.area * 0.02
        if enclosed >= 5 and big:
            continue
        kept.append(s)
    for i, s in enumerate(kept):
        s.id = f"sym-{i:03d}"
    return kept


def _drop_internal_lines(lines: list[Polyline],
                         symbols: list[Symbol]) -> list[Polyline]:
    """Discard construction lines that belong to a symbol's own glyph.

    A heat-exchanger glyph is a circle with a diameter line across it; an
    instrument balloon is a circle with a horizontal divider. Those strokes are
    unclosed runs and look exactly like short process pipes. Left in, they weld
    neighbouring equipment together through the middle of a symbol -- producing,
    for example, a phantom column-to-drum connection that bypasses the exchanger
    actually sitting between them. A run wholly inside one symbol is glyph
    detail, never a connection.
    """
    kept: list[Polyline] = []
    for pl in lines:
        box = pl.bbox()
        internal = any(
            s.bbox.contains(box.x0, box.y0, margin=0.6)
            and s.bbox.contains(box.x1, box.y1, margin=0.6)
            for s in symbols)
        if not internal:
            kept.append(pl)
    return kept


# Composite rules: overlapping primitives that together make one symbol.
def _merge_shapes(shapes: list[dict[str, Any]]) -> list[Symbol]:
    """Merge overlapping primitives into composite symbols.

    A pump is drawn as a circle plus an impeller triangle; a drum is a rectangle
    plus two dished-head arcs. Left unmerged, the graph would contain phantom
    equipment, so overlap is resolved before anything is named.
    """
    used = [False] * len(shapes)
    out: list[Symbol] = []
    order = sorted(range(len(shapes)), key=lambda i: -shapes[i]["box"].area)

    for i in order:
        if used[i]:
            continue
        base = shapes[i]
        box = base["box"]
        members = [base]
        used[i] = True
        changed = True
        while changed:
            changed = False
            for j in order:
                if used[j]:
                    continue
                other = shapes[j]
                if box.iou(other["box"]) > 0.10 or _mostly_inside(other["box"], box):
                    box = box.merge(other["box"])
                    members.append(other)
                    used[j] = True
                    changed = True
        sym_class, conf = _resolve_composite(members)
        out.append(Symbol(
            id=f"sym-{len(out):03d}", sym_class=sym_class, bbox=box,
            confidence=conf, method="vector-geometry",
            primitives=sorted({m["primitive"] for m in members})))
    return out


def _mostly_inside(inner: BBox, outer: BBox, frac: float = 0.72) -> bool:
    ix0, iy0 = max(inner.x0, outer.x0), max(inner.y0, outer.y0)
    ix1, iy1 = min(inner.x1, outer.x1), min(inner.y1, outer.y1)
    overlap = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    return inner.area > 0 and overlap / inner.area >= frac


def _resolve_composite(members: list[dict[str, Any]]) -> tuple[str, float]:
    prims = {m["primitive"] for m in members}
    classes = [m["class"] for m in members]

    if {"circle-medium", "triangle"} <= prims or {"circle-large", "triangle"} <= prims:
        return "pump", 0.88
    if "bowtie" in prims:
        return "valve", 0.86
    if "rectangle" in prims and any(p.startswith("circle") for p in prims):
        return "vessel", 0.86
    if "rectangle-tall" in prims:
        return "column", 0.80
    if "rectangle" in prims:
        return "vessel", 0.74
    if prims == {"circle-small"}:
        return "instrument", 0.80
    if "circle-large" in prims or "circle-medium" in prims:
        return "heat_exchanger", 0.66

    best = max(members, key=lambda m: m["confidence"])
    return best["class"], best["confidence"] * 0.9


from .tags import TAG_IN_TEXT_RE as TAG_RE  # noqa: F401  (re-export)
from .tags import recognise as _recognise_tags


def _extract_tags(page: Any) -> list[TextTag]:
    """Read text with geometry and hand it to the shared tag recogniser."""
    items = [(w[4], BBox(w[0], w[1], w[2], w[3]), 0.97)
             for w in page.get_text("words")]
    return _recognise_tags(items, id_prefix="tag")
