"""Native CAD drawing extraction: DXF, and DWG via conversion.

This is the path that should be used wherever it exists, and it is qualitatively
different from the other two.

A scan gives pixels, and everything — what a shape is, what it is called, what it
connects to — has to be inferred from them. A vector PDF gives exact geometry but
throws the *semantics* away: a pump becomes a circle and a triangle, and the
pipeline has to guess that back.

A DXF gives neither pixels nor anonymous geometry. It gives the drawing as the
engineer authored it:

* an `INSERT` is a block reference carrying the **block's name** — `GATE_VALVE`,
  `PUMP_CENTRIFUGAL`, `VESSEL_VERTICAL` — so the symbol type is read, not
  classified;
* `ATTRIB` values on that insert usually carry the **equipment tag itself**, so
  there is no OCR and therefore no S/5 confusion;
* `TEXT` and `MTEXT` are exact strings;
* `LAYER` separates process piping from instrument signals from annotation, so a
  dashed lead is identified by what the author put it on rather than by measuring
  gaps between pixels;
* `LINETYPE` says dashed or continuous explicitly.

Every failure mode documented in `docs/drawings-real-world.md` — the four-digit
tag cap, the OCR glyph confusions, the table read as a plant, the resolution
floor — is a raster problem that does not arise here.
"""
from __future__ import annotations

import math
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .model import BBox, Polyline, Symbol, TextTag
from .tags import classify_prefix, looks_like_equipment_tag, TAG_RE, TAG_IN_TEXT_RE

# Layer-name fragments that mean "this is not process equipment". Layer naming is
# a project convention rather than a standard, so these are matched loosely and
# only ever used to *demote* an entity, never to discard a tagged one.
ANNOTATION_LAYER_HINTS = (
    "TEXT", "ANNO", "DIM", "TITLE", "BORDER", "FRAME", "TBLK", "NOTE",
    "LEGEND", "REVISION", "REV", "GRID", "HATCH", "VIEWPORT", "DEFPOINTS",
)
INSTRUMENT_LAYER_HINTS = ("INST", "SIGNAL", "CONTROL", "LOOP", "ELEC", "PNEU")
PROCESS_LAYER_HINTS = ("PROC", "PIPE", "LINE", "PRIMARY", "MAIN", "FLOW")

# Block-name fragments to symbol class. Block libraries differ between projects,
# so this is a starting vocabulary that the drawing's own legend extends.
BLOCK_CLASS_HINTS: list[tuple[tuple[str, ...], str]] = [
    (("PSV", "PRV", "RELIEF", "SAFETY_VALVE", "SAFETYVALVE"), "relief_valve"),
    (("VALVE", "VLV", "GATE", "GLOBE", "BALL", "BUTTERFLY", "CHECK", "NEEDLE",
      "PLUG", "DIAPHRAGM", "BFLY"), "valve"),
    (("PUMP", "PMP", "CENTRIF", "BLOWER", "COMPRESSOR", "COMP", "FAN"), "pump"),
    (("EXCH", "HEATER", "COOLER", "CONDENS", "REBOIL", "SHELL_TUBE", "HX",
      "AIRCOOL", "CHILLER"), "heat_exchanger"),
    (("COLUMN", "TOWER", "DISTIL", "SCRUB", "STRIP", "ABSORB"), "column"),
    (("VESSEL", "DRUM", "TANK", "SEPARATOR", "ACCUM", "KOD", "RECEIVER",
      "REACTOR", "FILTER", "STRAINER"), "vessel"),
    (("INSTR", "BALLOON", "BUBBLE", "TRANSMIT", "INDICAT", "GAUGE", "SENSOR",
      "ELEMENT", "SWITCH", "CONTROLLER", "ANALYS"), "instrument"),
]

DASHED_HINTS = ("DASH", "HIDDEN", "PHANTOM", "CENTER", "DOT", "DIVIDE")


def dwg_converter() -> str | None:
    """Find a DWG-to-DXF converter, if this appliance has one.

    DWG is proprietary and undocumented; DXF is the open interchange format that
    every CAD system exports. So DWG is handled by converting it, and when no
    converter is installed the honest answer is to say which one to install
    rather than to fail on the file type.
    """
    for name in ("dwg2dxf", "dwgread"):
        found = shutil.which(name)
        if found:
            return found
    local = Path.home() / ".sovereign" / "tools" / "bin"
    for name in ("dwg2dxf", "dwgread"):
        cand = local / name
        if cand.is_file():
            return str(cand)
    return shutil.which("ODAFileConverter")


class ConversionUnavailable(RuntimeError):
    pass


def dwg_to_dxf(path: Path) -> Path:
    """Convert a DWG to DXF in a temporary directory."""
    conv = dwg_converter()
    if not conv:
        raise ConversionUnavailable(
            "this file is AutoCAD DWG, which is a proprietary binary format. "
            "Install LibreDWG (which provides dwg2dxf) on the appliance, or "
            "export the drawing as DXF from the CAD system — DXF is the open "
            "interchange format and is what an air-gapped deployment should "
            "standardise on, because it needs no proprietary converter in the "
            "loop.")
    out_dir = Path(tempfile.mkdtemp(prefix="dwg2dxf-"))
    out = out_dir / (path.stem + ".dxf")

    if conv.endswith("dwg2dxf"):
        cmd = [conv, "-o", str(out), str(path)]
    elif conv.endswith("dwgread"):
        cmd = [conv, "-O", "DXF", "-o", str(out), str(path)]
    else:                                    # ODA File Converter
        cmd = [conv, str(path.parent), str(out_dir), "ACAD2018", "DXF", "0", "1"]

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if not out.exists():
        candidates = sorted(out_dir.glob("*.dxf"))
        if candidates:
            out = candidates[0]
    if not out.exists():
        raise RuntimeError(
            f"DWG conversion produced no DXF ({Path(conv).name} exit "
            f"{proc.returncode}): {(proc.stderr or proc.stdout)[:300]}")
    return out


def learned_legend() -> dict[str, dict[str, Any]]:
    """The block-to-meaning map learned from this project's symbol library."""
    from .. import db
    try:
        return {r["block"]: dict(r)
                for r in db.query("SELECT * FROM drawing_legend")}
    except Exception:
        return {}


def save_legend(result: dict[str, Any]) -> int:
    from .. import db
    n = 0
    for block, e in (result.get("entries") or {}).items():
        db.upsert("drawing_legend", {
            "block": block, "description": e["description"],
            "sym_class": e["sym_class"], "confidence": e["confidence"],
            "source": result.get("source", ""), "ts": db.now()}, key="block")
        n += 1
    return n


def classify_block(name: str,
                   legend: dict[str, dict[str, Any]] | None = None
                   ) -> tuple[str, float]:
    """Map a block name to a symbol class.

    The project's own legend wins: a library block called `UCCPRO065` is an
    internal code that no keyword table could ever decode, but the sheet that
    defines it captions it. Falling back to name keywords covers libraries whose
    blocks are named descriptively.
    """
    if legend is None:
        legend = learned_legend()
    hit = legend.get(name)
    if hit and hit.get("sym_class"):
        return hit["sym_class"], float(hit.get("confidence") or 0.85)

    upper = re.sub(r"[^A-Z0-9]+", "_", (name or "").upper())
    for fragments, cls in BLOCK_CLASS_HINTS:
        if any(f in upper for f in fragments):
            return cls, 0.92
    return "unknown", 0.35


def _layer_role(layer: str) -> str:
    up = (layer or "").upper()
    if any(h in up for h in ANNOTATION_LAYER_HINTS):
        return "annotation"
    if any(h in up for h in INSTRUMENT_LAYER_HINTS):
        return "instrument"
    if any(h in up for h in PROCESS_LAYER_HINTS):
        return "process"
    return "other"


def _is_dashed(entity: Any, doc: Any) -> bool:
    lt = (getattr(entity.dxf, "linetype", "") or "BYLAYER").upper()
    if lt in ("BYLAYER", "BYBLOCK"):
        try:
            lt = (doc.layers.get(entity.dxf.layer).dxf.linetype or "").upper()
        except Exception:
            lt = ""
    return any(h in lt for h in DASHED_HINTS)


def _insert_bbox(insert: Any) -> BBox | None:
    """Bounding box of a block reference, from the block's own geometry."""
    try:
        blk = insert.doc.blocks.get(insert.dxf.name)
    except Exception:
        blk = None
    if blk is None:
        # A converted DWG can reference a block whose definition did not survive
        # the conversion. The insertion point is still real, so keep the symbol
        # and let the tag or an attached line say what it is.
        return None
    xs: list[float] = []
    ys: list[float] = []
    for e in blk:
        try:
            t = e.dxftype()
            if t == "LINE":
                xs += [e.dxf.start.x, e.dxf.end.x]
                ys += [e.dxf.start.y, e.dxf.end.y]
            elif t in ("CIRCLE", "ARC"):
                r = e.dxf.radius
                xs += [e.dxf.center.x - r, e.dxf.center.x + r]
                ys += [e.dxf.center.y - r, e.dxf.center.y + r]
            elif t == "LWPOLYLINE":
                for p in e.get_points("xy"):
                    xs.append(p[0])
                    ys.append(p[1])
            elif t in ("TEXT", "MTEXT"):
                xs.append(e.dxf.insert.x)
                ys.append(e.dxf.insert.y)
        except Exception:
            continue
    if not xs:
        return None
    sx = getattr(insert.dxf, "xscale", 1.0) or 1.0
    sy = getattr(insert.dxf, "yscale", 1.0) or 1.0
    ox, oy = insert.dxf.insert.x, insert.dxf.insert.y
    rot = math.radians(getattr(insert.dxf, "rotation", 0.0) or 0.0)
    cos_r, sin_r = math.cos(rot), math.sin(rot)

    corners = []
    for x, y in ((min(xs), min(ys)), (max(xs), min(ys)),
                 (max(xs), max(ys)), (min(xs), max(ys))):
        px, py = x * sx, y * sy
        corners.append((ox + px * cos_r - py * sin_r,
                        oy + px * sin_r + py * cos_r))
    return BBox(min(c[0] for c in corners), min(c[1] for c in corners),
                max(c[0] for c in corners), max(c[1] for c in corners))


def extract(path: str | Path) -> dict[str, Any]:
    """Read a DXF (or a DWG, by converting it first) into symbols, lines and tags."""
    import ezdxf

    path = Path(path)
    converted_from = None
    if path.suffix.lower() == ".dwg":
        converted_from = str(path)
        path = dwg_to_dxf(path)

    doc = ezdxf.readfile(str(path))
    msp = doc.modelspace()

    symbols: list[Symbol] = []
    lines: list[Polyline] = []
    tags: list[TextTag] = []
    text_entities: list[str] = []
    xs: list[float] = []
    ys: list[float] = []
    block_counts: dict[str, int] = {}
    legend = learned_legend()

    def note(x: float, y: float) -> None:
        xs.append(x)
        ys.append(y)

    # -- block references: the symbols, named by their author
    for ins in msp.query("INSERT"):
        name = ins.dxf.name
        block_counts[name] = block_counts.get(name, 0) + 1
        box = _insert_bbox(ins)
        if box is None:
            p = ins.dxf.insert
            box = BBox(p.x - 5, p.y - 5, p.x + 5, p.y + 5)
        note(box.x0, box.y0)
        note(box.x1, box.y1)

        cls, conf = classify_block(name, legend)
        # A block attribute usually carries the tag outright, which is the whole
        # advantage of this path: the equipment names itself.
        tag = ""
        try:
            for att in ins.attribs:
                value = (att.dxf.text or "").strip().upper()
                if looks_like_equipment_tag(_canonical(value)):
                    tag = _canonical(value)
                    break
        except Exception:
            pass

        symbols.append(Symbol(
            id=f"sym-{len(symbols):03d}", sym_class=cls, bbox=box,
            confidence=conf, method=f"dxf-block:{name}", tag=tag,
            primitives=[f"block:{name}", f"layer:{ins.dxf.layer}"]))

    # -- geometry: pipes and instrument leads
    for e in msp:
        t = e.dxftype()
        layer = getattr(e.dxf, "layer", "") or ""
        role = _layer_role(layer)
        try:
            if t == "LINE":
                pts = [(e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y)]
            elif t == "LWPOLYLINE":
                pts = [(p[0], p[1]) for p in e.get_points("xy")]
            elif t == "POLYLINE":
                pts = [(v.dxf.location.x, v.dxf.location.y) for v in e.vertices]
            else:
                continue
        except Exception:
            continue
        if len(pts) < 2 or role == "annotation":
            continue
        for x, y in pts:
            note(x, y)
        dashed = _is_dashed(e, doc) or role == "instrument"
        lines.append(Polyline(id=f"cad-{len(lines):04d}", points=pts,
                              dashed=dashed,
                              width=float(getattr(e.dxf, "lineweight", 1) or 1)))

    # -- text: exact strings, so no OCR repair and no lexicon needed
    for e in msp.query("TEXT MTEXT ATTRIB"):
        try:
            raw = (e.plain_text() if e.dxftype() == "MTEXT" else e.dxf.text) or ""
        except Exception:
            raw = getattr(e.dxf, "text", "") or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            p = e.dxf.insert
            px, py = float(p.x), float(p.y)
        except Exception:
            continue
        note(px, py)
        text_entities.append(raw)
        height = float(getattr(e.dxf, "height", 2.5) or 2.5)
        width = max(height * 0.6 * len(raw), height)
        box = BBox(px, py, px + width, py + height)

        for candidate in _tag_candidates(raw):
            if not looks_like_equipment_tag(candidate):
                continue
            tags.append(TextTag(
                id=f"tag-{len(tags):03d}", text=candidate, bbox=box,
                tag_type=classify_prefix(candidate.split("-")[0]),
                confidence=1.0))
            break

    if not xs:
        xs, ys = [0.0, 100.0], [0.0, 100.0]
    page = BBox(min(xs), min(ys), max(xs), max(ys))

    return {
        "symbols": symbols, "lines": lines, "tags": tags, "page_box": page,
        "text_entities": text_entities, "rejected": False,
        "assessment": {
            "is_drawing": True,
            "source": "cad",
            "dxf_version": doc.dxfversion,
            "converted_from_dwg": converted_from,
            "entities": len(list(msp)),
            "blocks_used": dict(sorted(block_counts.items(),
                                       key=lambda kv: -kv[1])[:20]),
            "layers": sorted({(getattr(e.dxf, "layer", "") or "") for e in msp}),
            "reason": ("read natively from CAD geometry: symbol types come from "
                       "block names and tags from text entities, so neither is "
                       "inferred from pixels"),
        },
    }


# Keyword -> symbol class, applied to the human-readable description a legend
# sheet places beside each block. Block *names* in a real library are internal
# codes -- VPINSN, UCCPRO065, INST-AR -- and mean nothing on their own; the
# description next to them is what the engineer reads.
DESCRIPTION_CLASS_HINTS: list[tuple[tuple[str, ...], str]] = [
    (("RELIEF", "SAFETY VALVE", "RUPTURE", "PSV", "PRV"), "relief_valve"),
    (("VALVE", "COCK", "DAMPER", "REGULATOR", "BLIND", "SPECTACLE"), "valve"),
    (("PUMP", "COMPRESSOR", "BLOWER", "FAN", "AGITATOR", "MIXER", "EJECTOR",
      "TURBINE", "MOTOR"), "pump"),
    (("EXCHANGER", "COOLER", "HEATER", "CONDENSER", "REBOILER", "CHILLER",
      "COIL"), "heat_exchanger"),
    (("COLUMN", "TOWER", "SCRUBBER", "STRIPPER", "ABSORBER"), "column"),
    (("VESSEL", "TANK", "DRUM", "SEPARATOR", "ACCUMULATOR", "RECEIVER",
      "REACTOR", "HOPPER", "SILO", "FILTER", "STRAINER", "CYCLONE"), "vessel"),
    (("INSTRUMENT", "INDICATOR", "TRANSMITTER", "GAUGE", "SENSOR", "ELEMENT",
      "SWITCH", "CONTROLLER", "ANALYSER", "ANALYZER", "METER", "RECORDER",
      "ORIFICE", "THERMOWELL", "SIGNAL"), "instrument"),
]


def classify_description(text: str) -> tuple[str, float]:
    up = (text or "").upper()
    for fragments, cls in DESCRIPTION_CLASS_HINTS:
        if any(f in up for f in fragments):
            return cls, 0.88
    return "unknown", 0.0


def learn_legend(path: str | Path) -> dict[str, Any]:
    """Read a symbol-library drawing as a legend.

    A P&ID symbol library is a sheet of blocks, each captioned with what it
    means -- `FRWD-10` beside "Regulator Forward", `AG-DSP` beside "Dispersing
    Agitator". That mapping is exactly what the architecture calls legend-scoped
    extraction, and a project's own library is the authoritative source for it.

    Pairing is by proximity: the nearest text below or beside a block insert is
    its caption. Where the caption names a known equipment kind, the block is
    bound to that class for every later drawing from the same project.
    """
    data = extract(path)
    entries: dict[str, dict[str, Any]] = {}

    texts = [(t, b) for t, b in _text_boxes(path)]
    for sym in data["symbols"]:
        block = next((p.split(":", 1)[1] for p in sym.primitives
                      if p.startswith("block:")), "")
        if not block:
            continue
        best: tuple[float, str] | None = None
        for raw, box in texts:
            # Captions sit below or immediately beside the glyph.
            dx = abs(box.cx - sym.bbox.cx)
            dy = sym.bbox.y0 - box.cy          # positive when text is below
            span = max(sym.bbox.w, sym.bbox.h, 1.0)
            if dx > span * 3 or not (-span * 0.6 <= dy <= span * 4):
                continue
            score = dx + abs(dy) * 0.5
            if best is None or score < best[0]:
                best = (score, raw)
        if not best:
            continue
        cls, conf = classify_description(best[1])
        if cls == "unknown":
            continue
        prev = entries.get(block)
        if prev is None or conf > prev["confidence"]:
            entries[block] = {"block": block, "description": best[1].strip(),
                              "sym_class": cls, "confidence": conf}

    return {"source": str(path), "entries": entries,
            "blocks_seen": len(data["symbols"]),
            "learned": len(entries)}


def _text_boxes(path: str | Path) -> list[tuple[str, BBox]]:
    import ezdxf
    p = Path(path)
    if p.suffix.lower() == ".dwg":
        p = dwg_to_dxf(p)
    doc = ezdxf.readfile(str(p))
    out: list[tuple[str, BBox]] = []
    for e in doc.modelspace().query("TEXT MTEXT"):
        try:
            raw = (e.plain_text() if e.dxftype() == "MTEXT" else e.dxf.text) or ""
            raw = raw.strip()
            if not raw:
                continue
            ins = e.dxf.insert
            h = float(getattr(e.dxf, "height", 2.5) or 2.5)
            out.append((raw, BBox(ins.x, ins.y, ins.x + h * 0.6 * len(raw),
                                  ins.y + h)))
        except Exception:
            continue
    return out


def _canonical(text: str) -> str:
    m = TAG_RE.match(text.strip().upper().replace(" ", ""))
    if m:
        return f"{m.group(1)}-{m.group(2)}{m.group(3)}"
    return text.strip().upper()


def _tag_candidates(raw: str) -> list[str]:
    """Tags inside a text entity. Exact strings, so only splitting is needed."""
    out: list[str] = []
    text = raw.upper()
    direct = _canonical(text)
    if TAG_RE.match(direct):
        out.append(direct)
    for m in TAG_IN_TEXT_RE.finditer(text):
        out.append(f"{m.group(1)}-{m.group(2)}{m.group(3)}")
    return out
