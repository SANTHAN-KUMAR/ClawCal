"""Engineering-drawing analysis orchestrator.

Chooses the vector path when the file carries real geometry and the raster path
otherwise, assembles the connectivity graph, optionally cross-checks against the
local vision model, and persists everything so the workbench can overlay symbols,
tags and traced lines on the page image.

The vision model is used as a *corroborator*, never as the source of
connectivity. It is good at reading a title block and naming equipment, and
unreliable at saying what is connected to what -- so its output is recorded
alongside the geometric result and any disagreement is surfaced, not resolved
silently in the model's favour.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .. import audit, db
from ..config import EVIDENCE_DIR
from . import graph, raster, vector
from .model import (CONFIRMED, PROBABLE, UNRESOLVED, DrawingAnalysis, Edge,
                    Symbol, TextTag)

RENDER_SCALE = 2.0        # points -> pixels for the overlay image
MIN_VECTOR_PRIMITIVES = 25


def _has_vector_geometry(page: Any) -> bool:
    try:
        drawings = page.get_drawings()
    except Exception:
        return False
    return len(drawings) >= MIN_VECTOR_PRIMITIVES


def analyse_pdf(path: str | Path, *, page_no: int = 1, title: str = "",
                drawing_id: str | None = None, use_vlm: bool = True,
                task_id: str | None = None) -> DrawingAnalysis:
    import fitz

    path = Path(path)
    did = drawing_id or db.new_id("dwg")
    doc = fitz.open(str(path))
    try:
        page = doc[page_no - 1]
        rect = page.rect
        out_dir = EVIDENCE_DIR / did
        out_dir.mkdir(parents=True, exist_ok=True)
        img_path = out_dir / f"drawing-p{page_no}.png"
        page.get_pixmap(matrix=fitz.Matrix(RENDER_SCALE, RENDER_SCALE)).save(
            str(img_path))

        if _has_vector_geometry(page):
            extracted = vector.extract(page)
            source_kind = "vector"
        else:
            extracted = raster.extract(img_path, scale=RENDER_SCALE)
            source_kind = "raster"
    finally:
        doc.close()

    return _assemble(did, title or path.stem, source_kind, rect.width, rect.height,
                     str(img_path), extracted, use_vlm=use_vlm, task_id=task_id)


def analyse_image(path: str | Path, *, title: str = "",
                  drawing_id: str | None = None, use_vlm: bool = True,
                  task_id: str | None = None) -> DrawingAnalysis:
    from PIL import Image

    path = Path(path)
    did = drawing_id or db.new_id("dwg")
    out_dir = EVIDENCE_DIR / did
    out_dir.mkdir(parents=True, exist_ok=True)
    img_path = out_dir / f"drawing{path.suffix.lower()}"
    if str(img_path) != str(path):
        img_path.write_bytes(path.read_bytes())
    with Image.open(img_path) as im:
        w, h = im.size

    extracted = raster.extract(img_path, scale=1.0)
    return _assemble(did, title or path.stem, "raster", float(w), float(h),
                     str(img_path), extracted, use_vlm=use_vlm, task_id=task_id)


def _assemble(did: str, title: str, source_kind: str, width: float, height: float,
              image_path: str, extracted: dict[str, Any], *, use_vlm: bool,
              task_id: str | None) -> DrawingAnalysis:
    symbols: list[Symbol] = extracted["symbols"]
    tags: list[TextTag] = extracted["tags"]

    # Tolerances are calibrated for a nominal A3 sheet in PDF points; a raster
    # page carries far more coordinate units for the same physical distance, so
    # everything geometric is scaled by the sheet's width ratio.
    coord_scale = max(1.0, width / 842.0)
    graph.associate_tags(symbols, tags, scale=coord_scale)
    chains = graph.chain_lines(extracted["lines"],
                               tol=graph.JOIN_TOLERANCE * coord_scale)
    edges = graph.build_edges(chains, symbols, tags, source_kind=source_kind,
                              scale=coord_scale)

    analysis = DrawingAnalysis(
        drawing_id=did, title=title, source_kind=source_kind, width=width,
        height=height, image_path=image_path, symbols=symbols, tags=tags,
        edges=edges)

    # Honest warnings. These are what an engineer needs to know before trusting it.
    untagged = [s for s in symbols if not s.tag and s.sym_class != "unknown"]
    if untagged:
        analysis.warnings.append(
            f"{len(untagged)} of {len(symbols)} symbols carry no recognised tag; "
            f"they are identified by shape and position only")
    unresolved = [e for e in edges if e.status == UNRESOLVED]
    if unresolved:
        analysis.warnings.append(
            f"{len(unresolved)} traced line(s) could not be attached at both ends; "
            f"connectivity for these is reported as CANNOT DETERMINE")
    if source_kind == "raster":
        analysis.warnings.append(
            "connectivity was recovered from a raster image, so no connection on "
            "this drawing is rated better than PROBABLE; obtain the vector PDF for "
            "a confirmed reading")
    unknown = [s for s in symbols if s.sym_class == "unknown"]
    if unknown:
        analysis.warnings.append(
            f"{len(unknown)} shape(s) did not match the configured legend and are "
            f"reported as unclassified rather than guessed")

    analysis.notes.append(
        f"coordinate scale {coord_scale:.2f} (tolerances scaled accordingly)")
    analysis.notes.append(
        f"topology: {db.jdump(graph.analyse_topology(symbols, edges))}")

    if use_vlm:
        analysis.vlm_reading = _vlm_crosscheck(image_path, analysis)

    _persist(analysis, task_id)
    audit.record("drawings", "analysed", task_id=task_id,
                 detail={"drawing_id": did, "title": title, **analysis.summary()})
    return analysis


_VLM_PROMPT = """This is an engineering drawing (a piping and instrumentation diagram).

Report ONLY what you can actually read on the sheet:
1. The drawing number and title from the title block.
2. Every equipment and instrument tag you can read (e.g. V-204, P-101A, PSV-204A).
3. Any note or set-point text.

Do NOT describe what is connected to what. Do NOT infer anything you cannot read
directly. If text is illegible, say [illegible]."""


def _vlm_crosscheck(image_path: str, analysis: DrawingAnalysis) -> str:
    """Ask the local VLM to read the sheet, and compare tag sets.

    Deliberately scoped to *reading*, not connectivity: the failure mode we are
    guarding against is a fluent, wrong description of topology.
    """
    from ..gateway import gateway
    from ..gateway.base import GenRequest
    from ..gateway.registry import registry

    vision = registry.vision_models()
    if not vision:
        return ""
    card = vision[0]
    msg = gateway.image_message("user", _VLM_PROMPT, [image_path])
    res = gateway.generate(GenRequest(messages=[msg], model=card.name,
                                      temperature=0.0, max_tokens=700,
                                      timeout_s=420), allow_fallback=False)
    if not res.ok:
        analysis.warnings.append(
            f"vision cross-check unavailable: {res.error[:120]}")
        return ""

    import re
    vlm_tags = {t.upper() for t in re.findall(r"\b[A-Z]{1,4}-\d{2,4}[A-Z]?\b",
                                              res.text.upper())}
    geo_tags = {s.tag.upper() for s in analysis.symbols if s.tag}
    missed = sorted(vlm_tags - geo_tags)
    extra = sorted(geo_tags - vlm_tags)
    if missed:
        analysis.warnings.append(
            f"the vision model read tag(s) {missed} that geometric extraction did "
            f"not attach to any symbol; these may be off-sheet references or "
            f"unattached annotations")
    if extra:
        analysis.notes.append(
            f"geometric extraction attached tag(s) {extra} that the vision model "
            f"did not report; geometry is authoritative for attachment")
    return res.text


def _persist(a: DrawingAnalysis, task_id: str | None) -> None:
    db.upsert("drawings", {
        "id": a.drawing_id, "doc_id": None, "title": a.title, "page_no": 1,
        "source_kind": a.source_kind, "width": a.width, "height": a.height,
        "image_path": a.image_path, "status": "ANALYSED",
        "summary": db.jdump(a.summary()), "created_at": time.time()}, key="id")

    db.execute("DELETE FROM drawing_symbols WHERE drawing_id=?", (a.drawing_id,))
    db.execute("DELETE FROM drawing_tags WHERE drawing_id=?", (a.drawing_id,))
    db.execute("DELETE FROM drawing_edges WHERE drawing_id=?", (a.drawing_id,))

    with db.tx() as c:
        for s in a.symbols:
            c.execute(
                "INSERT INTO drawing_symbols (id,drawing_id,label,sym_class,bbox,"
                "confidence,method,tag) VALUES (?,?,?,?,?,?,?,?)",
                (f"{a.drawing_id}:{s.id}", a.drawing_id, s.label, s.sym_class,
                 db.jdump(s.bbox.as_list()), s.confidence, s.method, s.tag))
        for t in a.tags:
            c.execute(
                "INSERT INTO drawing_tags (id,drawing_id,text,tag_type,bbox,"
                "confidence,symbol_id,assoc_dist) VALUES (?,?,?,?,?,?,?,?)",
                (f"{a.drawing_id}:{t.id}", a.drawing_id, t.text, t.tag_type,
                 db.jdump(t.bbox.as_list()), t.confidence, t.symbol_id,
                 t.assoc_distance))
        for e in a.edges:
            c.execute(
                "INSERT INTO drawing_edges (id,drawing_id,src,dst,line_type,"
                "polyline,confidence,status,rationale) VALUES (?,?,?,?,?,?,?,?,?)",
                (f"{a.drawing_id}:{e.id}", a.drawing_id, e.src, e.dst, e.line_type,
                 db.jdump([[round(x, 1), round(y, 1)] for x, y in e.polyline]),
                 e.confidence, e.status, e.rationale))


def load(drawing_id: str) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM drawings WHERE id=?", (drawing_id,))
    if not row:
        return None
    syms = db.rows_to_dicts(db.query(
        "SELECT * FROM drawing_symbols WHERE drawing_id=?", (drawing_id,)))
    tags = db.rows_to_dicts(db.query(
        "SELECT * FROM drawing_tags WHERE drawing_id=?", (drawing_id,)))
    edges = db.rows_to_dicts(db.query(
        "SELECT * FROM drawing_edges WHERE drawing_id=?", (drawing_id,)))
    for s in syms:
        s["bbox"] = db.jload(s["bbox"], [])
    for t in tags:
        t["bbox"] = db.jload(t["bbox"], [])
    for e in edges:
        e["polyline"] = db.jload(e["polyline"], [])
    return {**dict(row), "summary": db.jload(row["summary"], {}),
            "symbols": syms, "tags": tags, "edges": edges}


def score_against_truth(a: DrawingAnalysis, truth: dict[str, Any],
                        statuses: tuple[str, ...] | None = None) -> dict[str, Any]:
    """Score an analysis against a ground-truth file.

    Used by the verification suite so the drawing pipeline is measured rather
    than admired.

    Each source is scored at the tier it is *permitted* to assert. Vector
    extraction is scored on CONFIRMED edges, because those are the only ones it
    may state as fact. Raster extraction is capped at PROBABLE by design, so
    scoring it on CONFIRMED alone would always return zero and would measure the
    cap rather than the pipeline.
    """
    if statuses is None:
        statuses = ((CONFIRMED,) if a.source_kind == "vector"
                    else (CONFIRMED, PROBABLE))
    truth_nodes = {k.upper() for k in truth.get("nodes", {})}
    found_tags = {s.tag.upper() for s in a.symbols if s.tag}
    tp = truth_nodes & found_tags
    fn = truth_nodes - found_tags
    fp = found_tags - truth_nodes

    by_id = {s.id: s for s in a.symbols}

    def key(u: str, v: str) -> tuple[str, str]:
        return tuple(sorted((u.upper(), v.upper())))  # type: ignore[return-value]

    truth_edges = {key(e["from"], e["to"]) for e in truth.get("edges", [])}
    got_edges = set()
    for e in a.edges:
        if e.status not in statuses or e.line_type != "process":
            continue
        s, d = by_id.get(e.src), by_id.get(e.dst)
        if s and d and s.tag and d.tag:
            got_edges.add(key(s.tag, d.tag))

    e_tp = truth_edges & got_edges
    e_fn = truth_edges - got_edges
    e_fp = got_edges - truth_edges

    def prf(tp_n: int, fp_n: int, fn_n: int) -> dict[str, float]:
        p = tp_n / (tp_n + fp_n) if tp_n + fp_n else 0.0
        r = tp_n / (tp_n + fn_n) if tp_n + fn_n else 0.0
        f = 2 * p * r / (p + r) if p + r else 0.0
        return {"precision": round(p, 3), "recall": round(r, 3), "f1": round(f, 3)}

    return {
        "scored_at": list(statuses),
        "tags": {**prf(len(tp), len(fp), len(fn)),
                 "matched": sorted(tp), "missed": sorted(fn), "spurious": sorted(fp)},
        "connectivity": {**prf(len(e_tp), len(e_fp), len(e_fn)),
                         "matched": sorted("-".join(e) for e in e_tp),
                         "missed": sorted("-".join(e) for e in e_fn),
                         "spurious": sorted("-".join(e) for e in e_fp)},
    }
