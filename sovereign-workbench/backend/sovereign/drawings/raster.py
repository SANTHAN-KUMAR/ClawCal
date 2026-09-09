"""Raster drawing extraction, for scans and photographs of drawings.

When there is no vector geometry the connectivity has to be recovered from
pixels, and the result is fundamentally less trustworthy. Everything this module
produces is therefore capped at PROBABLE by `graph.build_edges`.

Approach (classical CV, deterministic and inspectable -- a detector trained on a
handful of synthetic drawings would generalise worse and explain nothing):

  * adaptive binarisation, then morphological opening with long horizontal and
    vertical kernels to separate *lines* from *symbols*;
  * the line layer is vectorised with a probabilistic Hough transform and
    collinear segments are merged;
  * the symbol layer is segmented with connected components, and each blob is
    classified by shape descriptors (extent, circularity, solidity, vertex count)
    using the same legend as the vector path;
  * tags come from Tesseract with word geometry.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .model import BBox, Polyline, Symbol, TextTag
from .tags import plant_tag_lexicon
from .tags import recognise as _recognise_tags

MIN_BLOB_AREA = 220
MAX_BLOB_FRACTION = 0.18       # bigger than this is the border or title block
HOUGH_THRESHOLD = 45
HOUGH_MIN_LEN = 26
HOUGH_MAX_GAP = 6
COLLINEAR_ANGLE_TOL = 4.0      # degrees
COLLINEAR_OFFSET_TOL = 3.5     # pixels


def _binarise(gray: np.ndarray) -> np.ndarray:
    # Drawings are line art on paper: adaptive thresholding survives the uneven
    # illumination a flatbed or phone camera introduces.
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    binary = cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                   cv2.THRESH_BINARY_INV, 25, 9)
    return cv2.morphologyEx(binary, cv2.MORPH_CLOSE,
                            cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2)))


def _line_layer(binary: np.ndarray) -> np.ndarray:
    h, w = binary.shape
    hk = max(18, w // 45)
    vk = max(18, h // 45)
    horiz = cv2.morphologyEx(
        binary, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (hk, 1)))
    vert = cv2.morphologyEx(
        binary, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (1, vk)))
    return cv2.bitwise_or(horiz, vert)


def _merge_collinear(segments: list[tuple[float, float, float, float]]
                     ) -> list[tuple[float, float, float, float]]:
    """Hough returns a segment per dash of a single line; weld them back."""
    def angle(s: tuple[float, float, float, float]) -> float:
        return math.degrees(math.atan2(s[3] - s[1], s[2] - s[0])) % 180.0

    buckets: dict[int, list[tuple]] = {}
    for s in segments:
        buckets.setdefault(int(angle(s) // COLLINEAR_ANGLE_TOL), []).append(s)

    out: list[tuple[float, float, float, float]] = []
    for _, group in buckets.items():
        used = [False] * len(group)
        for i, s in enumerate(group):
            if used[i]:
                continue
            pts = [(s[0], s[1]), (s[2], s[3])]
            used[i] = True
            a = math.radians(angle(s))
            nx_, ny_ = -math.sin(a), math.cos(a)
            offset = nx_ * s[0] + ny_ * s[1]
            for j, t in enumerate(group):
                if used[j]:
                    continue
                off_t = nx_ * t[0] + ny_ * t[1]
                if abs(off_t - offset) <= COLLINEAR_OFFSET_TOL:
                    pts.extend([(t[0], t[1]), (t[2], t[3])])
                    used[j] = True
            # Project onto the line direction and take the extremes.
            dx, dy = math.cos(a), math.sin(a)
            proj = sorted(pts, key=lambda p: p[0] * dx + p[1] * dy)
            out.append((proj[0][0], proj[0][1], proj[-1][0], proj[-1][1]))
    return out


def _classify_blob(contour: np.ndarray, box: BBox) -> tuple[str, float, str]:
    area = cv2.contourArea(contour)
    peri = cv2.arcLength(contour, True)
    if peri <= 0 or area <= 0:
        return "unknown", 0.2, "degenerate"
    circularity = 4 * math.pi * area / (peri * peri)
    hull = cv2.convexHull(contour)
    hull_area = cv2.contourArea(hull) or 1.0
    solidity = area / hull_area
    extent = area / (box.w * box.h) if box.w and box.h else 0.0
    approx = cv2.approxPolyDP(contour, 0.02 * peri, True)
    verts = len(approx)
    span = max(box.w, box.h)
    aspect = min(box.w, box.h) / max(box.w, box.h) if max(box.w, box.h) else 0

    if circularity > 0.72 and aspect > 0.75:
        if span <= 46:
            return "instrument", 0.62, "circle-small"
        if span <= 74:
            return "pump", 0.50, "circle-medium"
        return "heat_exchanger", 0.46, "circle-large"
    if verts == 4 and extent > 0.72 and solidity > 0.85:
        if box.h > box.w * 1.6:
            return "column", 0.56, "rectangle-tall"
        return "vessel", 0.58, "rectangle"
    if solidity < 0.72 and verts >= 4 and aspect > 0.4:
        # Concave, roughly square: the classic bowtie valve.
        return "valve", 0.52, "bowtie"
    if verts == 3:
        return "pump", 0.40, "triangle"
    return "unknown", 0.25, f"blob-{verts}v"


# Tesseract needs roughly this many pixels of cap height to read reliably. Below
# it, recognition degrades into the S/5, O/0, B/8 confusions that make plant tags
# unusable — and no amount of upscaling recovers information the scan never had.
MIN_LEGIBLE_TEXT_PX = 14


def assess_resolution(gray: "np.ndarray",
                      word_heights: list[float] | None = None) -> dict[str, Any]:
    """Judge whether this scan carries enough resolution to read its tags.

    This is the single most useful thing the pipeline can tell an operator about
    a real drawing. Measured on the King County effluent P&ID: at 1440x1080 the
    tag reading scored F1 0.29, and on the same drawing's native 2200x1424 it
    scored 0.54 — the algorithm did not change, only the pixels available to it.
    An A1 sheet at 300 DPI is around 10 000 px wide; anything near 2 000 px is a
    screenshot, and telling the operator to rescan is worth more than any
    further tuning.
    """
    h, w = gray.shape
    # Measured from the OCR word boxes, not from contours. A contour-based
    # estimate counts speckle and hatching as glyphs and gets the answer
    # backwards -- it rated a 1440 px render as having *taller* text than the
    # same drawing's 2200 px original. The recogniser's own idea of where the
    # words are is the honest measure.
    heights = sorted(h_ for h_ in (word_heights or []) if h_ > 0)
    median_glyph = int(heights[len(heights) // 2]) if heights else 0

    # An A1 sheet is 841 mm wide; DPI follows from the pixel width.
    est_dpi = round(w / 33.1)
    legible = median_glyph >= MIN_LEGIBLE_TEXT_PX and bool(heights)

    return {
        "width": w, "height": h,
        "median_text_height_px": median_glyph,
        "words_measured": len(heights),
        "estimated_dpi_at_a1": est_dpi,
        "text_legible": legible,
        "advice": _resolution_advice(legible, bool(heights), median_glyph,
                                     w, est_dpi),
    }


def _resolution_advice(legible: bool, any_text: bool, median_px: int,
                       width: int, est_dpi: int) -> str:
    if legible:
        return ""
    if not any_text:
        head = "no text could be located on this sheet at all"
    else:
        head = (f"text on this sheet is about {median_px} px tall, below the "
                f"~{MIN_LEGIBLE_TEXT_PX} px OCR needs")
    return (f"{head}. At {width} px wide this is roughly {est_dpi} DPI for an A1 "
            f"sheet. Rescan at 300 DPI or higher, or supply the vector PDF — tag "
            f"reading cannot be made reliable from these pixels by any amount of "
            f"processing.")


def assess_page(binary: "np.ndarray", symbols_img: "np.ndarray",
                word_count: int) -> dict[str, Any]:
    """Decide whether this page is an engineering drawing at all.

    A page of tables produces exactly what a P&ID produces: rectangles and line
    runs. Run over a table of ISA instrument letters, the pipeline reported 437
    symbols — 161 vessels and 223 valves — every one of them a table cell. That
    is not a low score, it is a confidently wrong reading of a page that
    contains no equipment whatsoever, and the honest response is to refuse the
    page rather than describe it.

    Two signals separate the cases. A table's rectangles share edges: their
    boundaries collapse onto a handful of x and y coordinates, because cells are
    ruled on a grid. A drawing's symbols do not. And a table is mostly text,
    while a drawing is mostly line work.
    """
    h, w = binary.shape
    contours, _ = cv2.findContours(binary, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    for c in contours:
        x, y, cw, ch = cv2.boundingRect(c)
        if cw * ch < MIN_BLOB_AREA or cw * ch > w * h * MAX_BLOB_FRACTION:
            continue
        peri = cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, 0.02 * peri, True)
        extent = cv2.contourArea(c) / (cw * ch) if cw * ch else 0
        boxes.append((x, y, cw, ch, len(approx) == 4 and extent > 0.7))

    rects = [b for b in boxes if b[4]]
    grid_score = 0.0
    if len(rects) >= 8:
        # How much do rectangle edges collapse onto shared coordinates?
        tol = max(3, int(min(w, h) * 0.002))
        xs, ys = [], []
        for x, y, cw, ch, _ in rects:
            xs += [x, x + cw]
            ys += [y, y + ch]

        def collapse(vals: list[int]) -> float:
            vals = sorted(vals)
            groups = 1
            for a, b in zip(vals, vals[1:]):
                if b - a > tol:
                    groups += 1
            return 1.0 - groups / max(1, len(vals))

        grid_score = (collapse(xs) + collapse(ys)) / 2

    ink = float((binary > 0).sum()) / (w * h)
    text_density = word_count / max(1.0, (w * h) / 1e6)      # words per megapixel

    regions = _tabular_regions(rects, w, h)
    covered = sum(r_.area for r_ in regions) / float(w * h)

    # Only refuse the *page* when there is essentially nothing else on it. A real
    # drawing sheet always carries a title block, a revision table and often an
    # equipment list; rejecting the sheet because it contains tables would refuse
    # every genuine drawing. The tabular regions are excluded instead, and what
    # remains is analysed.
    looks_tabular = covered > 0.72
    looks_textual = text_density > 260 and ink < 0.09

    return {
        "is_drawing": not (looks_tabular or looks_textual),
        "tabular_regions": [r_.as_list() for r_ in regions],
        "tabular_coverage": round(covered, 3),
        "rectangles": len(rects),
        "grid_alignment": round(grid_score, 3),
        "ink_fraction": round(ink, 4),
        "words_per_megapixel": round(text_density, 1),
        "reason": (f"{covered:.0%} of the page is ruled as a grid — rectangle "
                   f"edges collapsing onto shared coordinates across "
                   f"{len(rects)} rectangles — so it is a table, not equipment"
                   if looks_tabular else
                   ("the page is predominantly text "
                    f"({text_density:.0f} words per megapixel over "
                    f"{ink:.1%} ink) rather than line work"
                    if looks_textual else "line work consistent with a drawing")),
    }


def _tabular_regions(rects: list[tuple], w: int, h: int) -> list[BBox]:
    """Locate title blocks, revision tables and equipment lists.

    These are made of the same primitives as equipment, so they have to be found
    and set aside geometrically. The signal is mutual alignment: table cells
    share edges with their neighbours, equipment symbols do not. Rectangles are
    clustered by shared edges, and a cluster of six or more is a table.
    """
    if len(rects) < 6:
        return []
    tol = max(3, int(min(w, h) * 0.004))
    n = len(rects)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        a, b = find(i), find(j)
        if a != b:
            parent[b] = a

    edges = [(x, y, x + cw, y + ch) for x, y, cw, ch, _ in rects]
    for i in range(n):
        xi0, yi0, xi1, yi1 = edges[i]
        for j in range(i + 1, n):
            xj0, yj0, xj1, yj1 = edges[j]
            # Adjacent cells share a vertical or horizontal edge line.
            shares_x = min(abs(xi0 - xj0), abs(xi1 - xj1), abs(xi1 - xj0),
                           abs(xi0 - xj1)) <= tol
            shares_y = min(abs(yi0 - yj0), abs(yi1 - yj1), abs(yi1 - yj0),
                           abs(yi0 - yj1)) <= tol
            overlap_x = min(xi1, xj1) - max(xi0, xj0) > -tol
            overlap_y = min(yi1, yj1) - max(yi0, yj0) > -tol
            if (shares_x and overlap_y) or (shares_y and overlap_x):
                union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    out: list[BBox] = []
    for members in groups.values():
        if len(members) < 6:
            continue
        xs0 = min(edges[i][0] for i in members)
        ys0 = min(edges[i][1] for i in members)
        xs1 = max(edges[i][2] for i in members)
        ys1 = max(edges[i][3] for i in members)
        out.append(BBox(xs0, ys0, xs1, ys1))
    return out


def extract(image_path: str | Path, *, scale: float = 1.0) -> dict[str, Any]:
    """Extract symbols, lines and tags from a raster drawing."""
    img = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise ValueError(f"could not read image {image_path}")
    h, w = img.shape
    binary = _binarise(img)
    tags_early = _tags_from_image(image_path, scale)
    assessment = assess_page(binary, binary, len(tags_early) * 4 + _word_estimate(img))
    assessment["resolution"] = assess_resolution(img, list(_LAST_WORD_HEIGHTS))
    if not assessment["is_drawing"]:
        return {"symbols": [], "lines": [], "tags": [],
                "page_box": BBox(0, 0, w / scale, h / scale),
                "assessment": assessment, "rejected": True}

    lines_img = _line_layer(binary)
    symbols_img = cv2.subtract(binary, lines_img)
    symbols_img = cv2.morphologyEx(
        symbols_img, cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))

    # -- symbols
    # Contours come from two passes, deduplicated by overlap.
    #
    # The symbol layer (binary minus long strokes) isolates compact glyphs, but
    # a column or drum outline is *made* of long strokes, so the line filter
    # deletes it and the equipment disappears. The full binary recovers those --
    # but it must be walked with RETR_LIST rather than RETR_EXTERNAL, because the
    # outermost contour on a drawing sheet is the border, and RETR_EXTERNAL would
    # return that one shape and nothing nested inside it.
    contours_sym, _ = cv2.findContours(symbols_img, cv2.RETR_EXTERNAL,
                                       cv2.CHAIN_APPROX_SIMPLE)
    contours_all, _ = cv2.findContours(binary, cv2.RETR_LIST,
                                       cv2.CHAIN_APPROX_SIMPLE)
    contours = list(contours_sym) + list(contours_all)
    symbols: list[Symbol] = []
    boxes_seen: list[BBox] = []

    def _accept(box_px: BBox, contour: Any, sym_class: str, conf: float,
                prim: str) -> None:
        for b in boxes_seen:
            if b.iou(box_px) > 0.55:
                return
        boxes_seen.append(box_px)
        symbols.append(Symbol(
            id=f"sym-{len(symbols):03d}", sym_class=sym_class,
            bbox=BBox(box_px.x0 / scale, box_px.y0 / scale,
                      box_px.x1 / scale, box_px.y1 / scale),
            confidence=conf, method="raster-cv", primitives=[prim]))

    # Largest first, so a composite symbol claims its area before its own
    # internal strokes are considered.
    excluded = [BBox(*r_) for r_ in assessment.get("tabular_regions", [])]

    def in_table(cx: float, cy: float) -> bool:
        return any(r_.contains(cx, cy, margin=2.0) for r_ in excluded)

    for c in sorted(contours, key=cv2.contourArea, reverse=True):
        x, y, cw, ch = cv2.boundingRect(c)
        if cw * ch < MIN_BLOB_AREA or cw * ch > w * h * MAX_BLOB_FRACTION:
            continue
        if in_table(x + cw / 2, y + ch / 2):
            continue
        box_px = BBox(x, y, x + cw, y + ch)
        sym_class, conf, prim = _classify_blob(c, box_px)
        if sym_class == "unknown" and conf < 0.3:
            continue
        _accept(box_px, c, sym_class, conf, prim)

    # Instrument balloons are small circles whose outline often merges with the
    # lead line, so contour analysis misses them. Hough finds them directly.
    circles = cv2.HoughCircles(
        cv2.medianBlur(255 - binary, 3), cv2.HOUGH_GRADIENT, dp=1,
        minDist=int(max(14, min(w, h) * 0.012)), param1=110, param2=26,
        minRadius=int(max(8, min(w, h) * 0.006)),
        maxRadius=int(max(20, min(w, h) * 0.030)))
    if circles is not None:
        for cx, cy, rad in np.round(circles[0]).astype(int):
            if in_table(cx, cy):
                continue
            box_px = BBox(cx - rad, cy - rad, cx + rad, cy + rad)
            _accept(box_px, None, "instrument", 0.55, "hough-circle")

    # -- lines
    raw = cv2.HoughLinesP(lines_img, 1, np.pi / 180, HOUGH_THRESHOLD,
                          minLineLength=HOUGH_MIN_LEN, maxLineGap=HOUGH_MAX_GAP)
    segs = [tuple(map(float, s[0])) for s in raw] if raw is not None else []
    merged = _merge_collinear(segs)
    polylines = [
        Polyline(id=f"rl-{i:03d}",
                 points=[(x0 / scale, y0 / scale), (x1 / scale, y1 / scale)],
                 dashed=False, width=1.0)
        for i, (x0, y0, x1, y1) in enumerate(merged)
        if not (in_table((x0 + x1) / 2, (y0 + y1) / 2))
    ]

    return {"symbols": symbols, "lines": polylines, "tags": tags_early,
            "page_box": BBox(0, 0, w / scale, h / scale),
            "assessment": assessment, "rejected": False,
            "debug": {"contours": len(contours), "hough_segments": len(segs),
                      "merged_segments": len(merged)}}


def _word_estimate(gray: "np.ndarray") -> int:
    """Cheap word count for the page assessment, without a second OCR pass."""
    th = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                               cv2.THRESH_BINARY_INV, 25, 9)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (12, 3))
    joined = cv2.dilate(th, kernel, iterations=1)
    cnts, _ = cv2.findContours(joined, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    n = 0
    for c in cnts:
        _x, _y, cw, ch = cv2.boundingRect(c)
        if 8 <= cw <= 400 and 6 <= ch <= 40:
            n += 1
    return n


OCR_UPSCALE = 2.0


def _tags_from_image(image_path: str | Path, scale: float) -> list[TextTag]:
    """Recognise tags on a raster drawing.

    Drawing annotations are small, thin and often rotated inside balloons.
    Tesseract's recall on them is poor at native resolution, so the sheet is
    upscaled and sharpened first and the recovered geometry is divided back down.
    Two page-segmentation modes are tried because a P&ID is sparse text on a
    field of line work, which neither mode handles alone.
    """
    from ..knowledge.ocr import _tesseract_tsv

    src = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
    words: list = []
    up = OCR_UPSCALE
    if src is not None:
        big = cv2.resize(src, None, fx=up, fy=up, interpolation=cv2.INTER_CUBIC)
        big = cv2.fastNlMeansDenoising(big, None, 7, 7, 21)
        big = cv2.adaptiveThreshold(big, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                    cv2.THRESH_BINARY, 31, 11)
        tmp = Path(image_path).with_suffix(".ocr.png")
        cv2.imwrite(str(tmp), big)
        seen: set[tuple] = set()
        for psm in (11, 6):
            _t, w_, _c = _tesseract_tsv(tmp, psm=psm)
            for wd in w_:
                key = (wd.text, round(wd.x0), round(wd.y0))
                if key not in seen:
                    seen.add(key)
                    words.append(wd)
        try:
            tmp.unlink()
        except OSError:
            pass
    else:
        up = 1.0
        _text, words, _conf = _tesseract_tsv(Path(image_path), psm=11)

    scale = scale * up
    items = [(wd.text, BBox(wd.x0 / scale, wd.y0 / scale,
                            wd.x1 / scale, wd.y1 / scale), wd.conf / 100.0)
             for wd in words if wd.conf >= 35]
    tags = _recognise_tags(items, id_prefix="tag", lexicon=plant_tag_lexicon())
    # Word heights in the *source* pixels, for the resolution assessment.
    _LAST_WORD_HEIGHTS.clear()
    _LAST_WORD_HEIGHTS.extend((wd.y1 - wd.y0) / up for wd in words
                              if wd.conf >= 35 and wd.y1 > wd.y0)
    return tags


# Word geometry from the most recent tag pass. A module-level stash rather than
# a return value because the tag extractor is called from two places and both
# only want the tags.
_LAST_WORD_HEIGHTS: list[float] = []
