"""Shared data model for engineering-drawing understanding.

The confidence vocabulary is the important part. The survey document is blunt
that a vision model will describe a P&ID fluently and get the connectivity wrong,
and that in this domain a confident wrong answer is worse than a refusal. So every
edge carries a status:

    CONFIRMED   both endpoints attach to a symbol within tolerance, along a
                continuous run of exact vector geometry
    PROBABLE    geometry is raster-derived, or a small gap was bridged, or an
                endpoint attached only after tolerance was relaxed
    UNRESOLVED  a real line exists but at least one end does not attach to
                anything -- reported as a dangling connection, never guessed

Downstream, only CONFIRMED edges may be stated as fact. PROBABLE edges are
reported as interpretation. UNRESOLVED edges are reported as gaps requiring a
human, which is what an engineer would actually want to be told.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

CONFIRMED, PROBABLE, UNRESOLVED = "CONFIRMED", "PROBABLE", "UNRESOLVED"

# Legend for this deployment. Adding a symbol class is a table entry, not code.
SYMBOL_LEGEND: dict[str, dict[str, Any]] = {
    "vessel": {"label": "Vessel / Drum", "tag_prefixes": ["V", "D", "T"]},
    "column": {"label": "Column / Tower", "tag_prefixes": ["C", "T"]},
    "pump": {"label": "Pump", "tag_prefixes": ["P", "GA"]},
    "heat_exchanger": {"label": "Heat Exchanger", "tag_prefixes": ["E", "HX"]},
    "valve": {"label": "Manual Valve", "tag_prefixes": ["HV", "V", "GV", "BV"]},
    "relief_valve": {"label": "Pressure Relief Valve", "tag_prefixes": ["PSV", "PRV", "RV"]},
    "instrument": {"label": "Instrument", "tag_prefixes":
                   ["PI", "TI", "LI", "FI", "PT", "TT", "LT", "FT", "PG", "TG",
                    "LG", "FG", "PC", "TC", "LC", "FC", "PSH", "PSL", "LSH", "LSL"]},
    "unknown": {"label": "Unclassified symbol", "tag_prefixes": []},
}

INSTRUMENT_PREFIXES = set(SYMBOL_LEGEND["instrument"]["tag_prefixes"])


@dataclass
class BBox:
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def w(self) -> float:
        return self.x1 - self.x0

    @property
    def h(self) -> float:
        return self.y1 - self.y0

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def area(self) -> float:
        return max(0.0, self.w) * max(0.0, self.h)

    def expand(self, m: float) -> "BBox":
        return BBox(self.x0 - m, self.y0 - m, self.x1 + m, self.y1 + m)

    def contains(self, x: float, y: float, margin: float = 0.0) -> bool:
        return (self.x0 - margin <= x <= self.x1 + margin
                and self.y0 - margin <= y <= self.y1 + margin)

    def distance_to(self, x: float, y: float) -> float:
        dx = max(self.x0 - x, 0.0, x - self.x1)
        dy = max(self.y0 - y, 0.0, y - self.y1)
        return math.hypot(dx, dy)

    def iou(self, other: "BBox") -> float:
        ix0, iy0 = max(self.x0, other.x0), max(self.y0, other.y0)
        ix1, iy1 = min(self.x1, other.x1), min(self.y1, other.y1)
        inter = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
        union = self.area + other.area - inter
        return inter / union if union > 0 else 0.0

    def merge(self, other: "BBox") -> "BBox":
        return BBox(min(self.x0, other.x0), min(self.y0, other.y0),
                    max(self.x1, other.x1), max(self.y1, other.y1))

    def as_list(self) -> list[float]:
        return [round(self.x0, 2), round(self.y0, 2),
                round(self.x1, 2), round(self.y1, 2)]


@dataclass
class Symbol:
    id: str
    sym_class: str
    bbox: BBox
    confidence: float
    method: str
    tag: str = ""
    primitives: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return self.tag or f"{SYMBOL_LEGEND.get(self.sym_class, {}).get('label', '?')} " \
                           f"@({self.bbox.cx:.0f},{self.bbox.cy:.0f})"

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "class": self.sym_class,
                "class_label": SYMBOL_LEGEND.get(self.sym_class, {}).get("label", "?"),
                "bbox": self.bbox.as_list(), "confidence": round(self.confidence, 3),
                "method": self.method, "tag": self.tag, "label": self.label,
                "primitives": self.primitives}


@dataclass
class TextTag:
    id: str
    text: str
    bbox: BBox
    tag_type: str = "unknown"        # equipment | instrument | line | note
    confidence: float = 1.0
    symbol_id: str = ""
    assoc_distance: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "text": self.text, "bbox": self.bbox.as_list(),
                "type": self.tag_type, "confidence": round(self.confidence, 3),
                "symbol_id": self.symbol_id,
                "assoc_distance": round(self.assoc_distance, 1)}


@dataclass
class Polyline:
    id: str
    points: list[tuple[float, float]]
    dashed: bool = False
    width: float = 1.0

    @property
    def length(self) -> float:
        return sum(math.dist(self.points[i], self.points[i + 1])
                   for i in range(len(self.points) - 1))

    @property
    def start(self) -> tuple[float, float]:
        return self.points[0]

    @property
    def end(self) -> tuple[float, float]:
        return self.points[-1]

    def bbox(self) -> BBox:
        xs = [p[0] for p in self.points]
        ys = [p[1] for p in self.points]
        return BBox(min(xs), min(ys), max(xs), max(ys))


@dataclass
class Edge:
    id: str
    src: str
    dst: str
    line_type: str                   # process | instrument_signal
    polyline: list[tuple[float, float]]
    confidence: float
    status: str
    rationale: str
    line_tag: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "src": self.src, "dst": self.dst,
                "line_type": self.line_type, "line_tag": self.line_tag,
                "polyline": [[round(x, 1), round(y, 1)] for x, y in self.polyline],
                "confidence": round(self.confidence, 3), "status": self.status,
                "rationale": self.rationale}


@dataclass
class DrawingAnalysis:
    drawing_id: str
    title: str
    source_kind: str
    width: float
    height: float
    image_path: str
    symbols: list[Symbol] = field(default_factory=list)
    tags: list[TextTag] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    vlm_reading: str = ""

    def symbol(self, sid: str) -> Symbol | None:
        return next((s for s in self.symbols if s.id == sid), None)

    def name_of(self, sid: str) -> str:
        s = self.symbol(sid)
        return s.label if s else sid

    def summary(self) -> dict[str, Any]:
        by_class: dict[str, int] = {}
        for s in self.symbols:
            by_class[s.sym_class] = by_class.get(s.sym_class, 0) + 1
        by_status: dict[str, int] = {}
        for e in self.edges:
            by_status[e.status] = by_status.get(e.status, 0) + 1
        tagged = sum(1 for s in self.symbols if s.tag)
        return {
            "source_kind": self.source_kind,
            "symbols": len(self.symbols),
            "symbols_by_class": by_class,
            "symbols_tagged": tagged,
            "tags": len(self.tags),
            "edges": len(self.edges),
            "edges_by_status": by_status,
            "confirmed_connectivity": by_status.get(CONFIRMED, 0),
            "unresolved_connectivity": by_status.get(UNRESOLVED, 0),
            "warnings": self.warnings,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "drawing_id": self.drawing_id, "title": self.title,
            "source_kind": self.source_kind, "width": self.width,
            "height": self.height, "image_path": self.image_path,
            "symbols": [s.to_dict() for s in self.symbols],
            "tags": [t.to_dict() for t in self.tags],
            "edges": [e.to_dict() for e in self.edges],
            "notes": self.notes, "warnings": self.warnings,
            "vlm_reading": self.vlm_reading,
            "summary": self.summary(),
        }

    def connectivity_report(self) -> str:
        """Human-readable connectivity, honest about what is not known."""
        lines: list[str] = []
        conf = [e for e in self.edges if e.status == CONFIRMED]
        prob = [e for e in self.edges if e.status == PROBABLE]  # noqa: E501
        unres = [e for e in self.edges if e.status == UNRESOLVED]

        lines.append(f"Drawing: {self.title} ({self.source_kind} source)")
        lines.append(f"Equipment and instruments identified: {len(self.symbols)} "
                     f"({sum(1 for s in self.symbols if s.tag)} carry a tag)")
        for s in sorted(self.symbols, key=lambda s: (s.sym_class, s.label)):
            lines.append(f"  - {s.label}  [{s.sym_class}, confidence "
                         f"{s.confidence:.2f}]")

        proc = [e for e in conf if e.line_type == "process"]
        signals = [e for e in conf if e.line_type == "instrument_signal"]

        lines.append("")
        lines.append(f"CONFIRMED process connections ({len(proc)}):")
        for e in proc:
            tag = f" via {e.line_tag}" if e.line_tag else ""
            lines.append(f"  - {self.name_of(e.src)} -> {self.name_of(e.dst)}{tag}")
        if not proc:
            lines.append("  none")

        # Instrument leads are not process flow. Reporting them together would
        # imply a pipe where the drawing shows only a signal.
        if signals:
            lines.append("")
            lines.append(f"Instrument signal leads ({len(signals)}) - measurement "
                         f"connections, not process flow:")
            for e in signals:
                lines.append(f"  - {self.name_of(e.src)} monitors "
                             f"{self.name_of(e.dst)}")

        if prob:
            lines.append("")
            lines.append(f"PROBABLE connections ({len(prob)}) - interpretation, "
                         f"not established fact:")
            for e in prob:
                lines.append(f"  - {self.name_of(e.src)} -> {self.name_of(e.dst)} "
                             f"({e.rationale})")
        if unres:
            lines.append("")
            lines.append(f"UNRESOLVED ({len(unres)}) - a line exists but its "
                         f"endpoints could not both be attached. "
                         f"CANNOT DETERMINE without human review:")
            for e in unres:
                lines.append(f"  - {e.rationale}")
        if self.warnings:
            lines.append("")
            lines.append("Warnings:")
            for w in self.warnings:
                lines.append(f"  - {w}")
        return "\n".join(lines)
