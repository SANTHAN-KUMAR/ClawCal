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


def extract(image_path: str | Path, *, scale: float = 1.0) -> dict[str, Any]:
    """Extract symbols, lines and tags from a raster drawing."""
    img = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise ValueError(f"could not read image {image_path}")
    h, w = img.shape
    binary = _binarise(img)
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
    for c in sorted(contours, key=cv2.contourArea, reverse=True):
        x, y, cw, ch = cv2.boundingRect(c)
        if cw * ch < MIN_BLOB_AREA or cw * ch > w * h * MAX_BLOB_FRACTION:
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
    ]

    return {"symbols": symbols, "lines": polylines,
            "tags": _tags_from_image(image_path, scale),
            "page_box": BBox(0, 0, w / scale, h / scale),
            "debug": {"contours": len(contours), "hough_segments": len(segs),
                      "merged_segments": len(merged)}}


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
    return _recognise_tags(items, id_prefix="tag")
