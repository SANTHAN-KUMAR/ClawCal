"""Local document text extraction.

Three tiers, tried in order, because each fails differently:

1. **Native PDF text layer** (PyMuPDF). Exact, with real word geometry. Free.
2. **Tesseract OCR** on a rendered page. Gives per-word boxes and confidences,
   which is what the evidence layer needs to point at a *region*, not just a page.
3. **Local VLM** (qwen2.5-vl). Handles handwriting and degraded scans that
   Tesseract mangles, but returns no geometry, so its output is marked as
   page-level evidence only.

If all three fail the page is marked EXTRACTION_FAILED. Nothing here ever invents
text: a failed page must be visible as a failure, because a silently empty page
becomes a silently missing finding.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import EVIDENCE_DIR

RENDER_DPI = 200
MIN_NATIVE_CHARS = 40        # below this a "text layer" is probably just a stamp
# Tesseract's mean confidence means different things on different inputs.
#
# On a rendered page of typed text it is a reasonable signal: at 45+ the words
# are usually right. On a photograph or a handwritten note it is not — measured
# on the corpus field note, Tesseract reports 68% confidence while turning
# "N 9.4  E 9.2  S 9.6  W 9.3" into "No.4 E92 59 4 Wo,3". Accepting that would
# put confidently wrong thickness readings into the evidence store, which is the
# exact failure this system exists to prevent. Images therefore have to clear a
# much higher bar before OCR is believed over the vision model.
MIN_TESS_CONF = 45.0            # rendered pages of typed text
MIN_TESS_CONF_IMAGE = 82.0      # photographs, handwriting, camera captures


@dataclass
class Word:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    conf: float = 100.0

    def as_tuple(self) -> tuple[float, float, float, float]:
        return (self.x0, self.y0, self.x1, self.y1)


@dataclass
class PageText:
    page_no: int
    text: str
    words: list[Word] = field(default_factory=list)
    extractor: str = "none"
    confidence: float = 0.0
    width: float = 0.0
    height: float = 0.0
    image_path: str = ""
    ok: bool = True
    error: str = ""

    def to_words_json(self) -> list[dict[str, Any]]:
        return [{"t": w.text, "b": [round(w.x0, 1), round(w.y0, 1),
                                    round(w.x1, 1), round(w.y1, 1)],
                 "c": round(w.conf, 1)} for w in self.words]


def _tesseract_tsv(image_path: Path, psm: int = 3) -> tuple[str, list[Word], float]:
    """Run tesseract in TSV mode so we keep word geometry and confidence."""
    if not shutil.which("tesseract"):
        return "", [], 0.0
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "out"
        cmd = ["tesseract", str(image_path), str(out), "--psm", str(psm),
               "-c", "preserve_interword_spaces=1", "tsv"]
        try:
            subprocess.run(cmd, capture_output=True, timeout=180, check=True)
        except Exception:
            return "", [], 0.0
        tsv = out.with_suffix(".tsv")
        if not tsv.exists():
            return "", [], 0.0
        words: list[Word] = []
        lines: dict[tuple, list[str]] = {}
        for row in tsv.read_text(errors="replace").splitlines()[1:]:
            p = row.split("\t")
            if len(p) < 12:
                continue
            txt = p[11].strip()
            if not txt:
                continue
            try:
                conf = float(p[10])
                x, y, w, h = (float(p[6]), float(p[7]), float(p[8]), float(p[9]))
            except ValueError:
                continue
            if conf < 0:
                continue
            words.append(Word(txt, x, y, x + w, y + h, conf))
            lines.setdefault((p[2], p[3], p[4]), []).append(txt)
        text = "\n".join(" ".join(v) for v in lines.values())
        mean_conf = sum(w.conf for w in words) / len(words) if words else 0.0
        return text, words, mean_conf


def _vlm_read(image_path: Path, hint: str = "") -> tuple[str, float]:
    """Last-resort read with the local vision model. No geometry, so page-level only."""
    from ..gateway import gateway
    from ..gateway.base import GenRequest
    from ..gateway.registry import registry

    vision = registry.vision_models()
    if not vision:
        return "", 0.0
    card = vision[0]
    prompt = (
        "Transcribe all text visible in this page image, preserving reading order, "
        "table rows and numbers exactly. Do not summarise, do not explain, do not "
        "add anything that is not written on the page. If a value is illegible "
        "write [illegible] in its place."
    )
    if hint:
        prompt += f"\n\nContext: {hint}"
    msg = gateway.image_message("user", prompt, [image_path])
    res = gateway.generate(
        GenRequest(messages=[msg], model=card.name, temperature=0.0,
                   max_tokens=1800, timeout_s=600),
        allow_fallback=False)
    if not res.ok or not res.text.strip():
        return "", 0.0
    # A VLM transcription is never as trustworthy as OCR with geometry.
    return res.text.strip(), 60.0


def render_page(doc: Any, page_no: int, out_dir: Path, dpi: int = RENDER_DPI) -> Path:
    import fitz
    page = doc[page_no - 1]
    pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72))
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"page-{page_no:04d}.png"
    pix.save(str(p))
    return p


def extract_pdf(path: Path, doc_id: str, *, use_vlm: bool = True,
                max_pages: int = 200) -> list[PageText]:
    import fitz

    out_dir = EVIDENCE_DIR / doc_id
    out_dir.mkdir(parents=True, exist_ok=True)
    pages: list[PageText] = []
    doc = fitz.open(str(path))
    try:
        for i in range(1, min(doc.page_count, max_pages) + 1):
            page = doc[i - 1]
            rect = page.rect
            pt = PageText(page_no=i, text="", width=rect.width, height=rect.height)

            # -- tier 1: native text layer
            native = page.get_text("words")     # x0,y0,x1,y1,word,block,line,word_no
            native_text = page.get_text("text").strip()
            if len(native_text) >= MIN_NATIVE_CHARS:
                pt.text = native_text
                pt.words = [Word(w[4], w[0], w[1], w[2], w[3], 100.0) for w in native]
                pt.extractor = "pdf-text-layer"
                pt.confidence = 99.0
                # Render anyway so the evidence viewer can show the region.
                pt.image_path = str(render_page(doc, i, out_dir))
                pages.append(pt)
                continue

            # -- tier 2: OCR
            img = render_page(doc, i, out_dir)
            pt.image_path = str(img)
            scale = RENDER_DPI / 72.0
            text, words, conf = _tesseract_tsv(img)
            if text and conf >= MIN_TESS_CONF:
                # Map pixel geometry back to PDF points so regions overlay correctly.
                pt.text = text
                pt.words = [Word(w.text, w.x0 / scale, w.y0 / scale,
                                 w.x1 / scale, w.y1 / scale, w.conf) for w in words]
                pt.extractor = "tesseract"
                pt.confidence = conf
                pages.append(pt)
                continue

            # -- tier 3: VLM
            if use_vlm:
                vtext, vconf = _vlm_read(img)
                if vtext:
                    pt.text = vtext
                    pt.extractor = "vlm"
                    pt.confidence = vconf
                    pages.append(pt)
                    continue

            pt.ok = False
            pt.extractor = "failed"
            pt.error = (f"no text layer, tesseract confidence {conf:.0f} below "
                        f"{MIN_TESS_CONF}, and the vision model returned nothing")
            pages.append(pt)
    finally:
        doc.close()
    return pages


def extract_image(path: Path, doc_id: str, *, use_vlm: bool = True) -> list[PageText]:
    from PIL import Image

    out_dir = EVIDENCE_DIR / doc_id
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"page-0001{path.suffix.lower()}"
    if str(dest) != str(path):
        shutil.copyfile(path, dest)
    with Image.open(dest) as im:
        w, h = im.size

    pt = PageText(page_no=1, text="", width=float(w), height=float(h),
                  image_path=str(dest))
    text, words, conf = _tesseract_tsv(dest)
    if text and conf >= MIN_TESS_CONF_IMAGE:
        pt.text, pt.words, pt.extractor, pt.confidence = text, words, "tesseract", conf
        return [pt]

    if use_vlm:
        vtext, vconf = _vlm_read(
            dest, hint="This is a photograph or a handwritten note. Transcribe "
                       "every number exactly as written.")
        if vtext:
            pt.text, pt.extractor, pt.confidence = vtext, "vlm", vconf
            # Keep the OCR words as weak geometry hints for region highlighting,
            # but never as the text of record: they are what was rejected.
            pt.words = [w for w in words if w.conf >= 60]
            if text and conf >= MIN_TESS_CONF:
                pt.error = (f"OCR read this at {conf:.0f}% mean confidence, below "
                            f"the {MIN_TESS_CONF_IMAGE:.0f}% required for a "
                            f"photograph or handwriting, so the vision model's "
                            f"reading was used instead")
            return [pt]

    # OCR was not good enough and no vision model answered. If OCR produced
    # something readable, return it clearly marked as low confidence rather than
    # discarding the page — but never silently.
    if text and conf >= MIN_TESS_CONF:
        pt.text, pt.words = text, words
        pt.extractor = "tesseract-low-confidence"
        pt.confidence = conf
        pt.error = (f"only OCR was available and its {conf:.0f}% confidence is "
                    f"below the {MIN_TESS_CONF_IMAGE:.0f}% required for this input "
                    f"type; treat every value on this page as unverified")
        return [pt]

    pt.ok = False
    pt.extractor = "failed"
    pt.error = (f"tesseract confidence {conf:.0f} is too low for a photograph or "
                f"handwriting and no vision model was available")
    return [pt]


def extract_xlsx(path: Path) -> list[PageText]:
    """Read a workbook as one pseudo-page per sheet.

    Cells are emitted as `Header: value` pairs where a header row is present, so
    the same labelled-value extractor that reads an inspection report also works
    on an equipment register kept in Excel. Formulas are read as their cached
    values, because the formula is not what the engineer is citing.
    """
    from openpyxl import load_workbook

    wb = load_workbook(str(path), data_only=True, read_only=True)
    pages: list[PageText] = []
    try:
        for idx, ws in enumerate(wb.worksheets, 1):
            rows = list(ws.iter_rows(values_only=True))
            if not rows:
                continue
            grid = [["" if c is None else str(c).strip() for c in (r or ())]
                    for r in rows]
            lines = [f"SHEET: {ws.title}", ""] + _table_lines(grid)
            pages.append(PageText(page_no=idx, text="\n".join(lines),
                                  extractor="xlsx", confidence=100.0))
    finally:
        wb.close()
    if not pages:
        pages = [PageText(page_no=1, text="", extractor="xlsx", ok=False,
                          error="the workbook contains no readable cells")]
    return pages


def extract_docx(path: Path) -> list[PageText]:
    """Read a Word document: paragraphs in order, then tables as labelled rows."""
    from docx import Document

    doc = Document(str(path))
    blocks = [p.text.strip() for p in doc.paragraphs if p.text.strip()]

    for ti, table in enumerate(doc.tables, 1):
        rows = [[c.text.strip() for c in r.cells] for r in table.rows]
        if not rows:
            continue
        blocks.append(f"\nTABLE {ti}")
        blocks.extend(_table_lines(rows))

    text = "\n".join(blocks)
    if not text.strip():
        return [PageText(page_no=1, text="", extractor="docx", ok=False,
                         error="the document contains no readable text")]
    # Word has no fixed pagination we can recover, so paginate for citability.
    per_page = 3200
    return [PageText(page_no=i // per_page + 1, text=text[i:i + per_page],
                     extractor="docx", confidence=100.0)
            for i in range(0, len(text), per_page)]


def _table_lines(rows: list[list[str]]) -> list[str]:
    """Render a table as text a reader and the value extractor can both use.

    Two columns in an industrial document is a parameter/value list, so the
    useful pairing is column one with column two -- "Design pressure: 16 bar g".
    Pairing each cell with its *header* instead gives "Parameter: Design
    pressure  Value: 16 bar g", which is technically faithful and useless: the
    labelled-value extractor then finds a field called "Parameter" whose value
    is a name. Wider tables genuinely need their headers, so they keep them.
    """
    if not rows:
        return []
    width = max(len(r) for r in rows)
    out: list[str] = []

    if width == 2:
        for r in rows:
            cells = (r + ["", ""])[:2]
            key, val = cells[0].strip(), cells[1].strip()
            if key and val:
                out.append(f"{key}: {val}")
            elif key or val:
                out.append(key or val)
        return out

    header = [c.strip() for c in rows[0]]
    has_header = sum(1 for c in header
                     if c and not c.replace(".", "").isdigit()) >= 2
    if has_header:
        out.append(" | ".join(h for h in header if h))
        body = rows[1:]
    else:
        body = rows
    for r in body:
        cells = [c.strip() for c in r]
        if not any(cells):
            continue
        if has_header:
            pairs = [f"{h}: {v}" for h, v in zip(header, cells) if h and v]
            out.append("  ".join(pairs) if pairs else " | ".join(
                c for c in cells if c))
        else:
            out.append(" | ".join(c for c in cells if c))
    return out


def extract_pptx(path: Path) -> list[PageText]:
    """Read a deck as one page per slide, including speaker notes."""
    from pptx import Presentation

    prs = Presentation(str(path))
    pages: list[PageText] = []
    for idx, slide in enumerate(prs.slides, 1):
        parts: list[str] = []
        for shape in slide.shapes:
            if shape.has_text_frame and shape.text_frame.text.strip():
                parts.append(shape.text_frame.text.strip())
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    cells = [c.text.strip() for c in row.cells]
                    if any(cells):
                        parts.append(" | ".join(c for c in cells if c))
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                parts.append(f"[speaker notes] {notes}")
        pages.append(PageText(page_no=idx,
                              text=f"SLIDE {idx}\n" + "\n".join(parts),
                              extractor="pptx", confidence=100.0,
                              ok=bool(parts)))
    if not pages:
        pages = [PageText(page_no=1, text="", extractor="pptx", ok=False,
                          error="the presentation contains no readable text")]
    return pages


def extract_text_file(path: Path) -> list[PageText]:
    raw = path.read_text(errors="replace")
    # Split long plain text into pseudo-pages so citations stay locatable.
    per_page = 3200
    pages = []
    for idx in range(0, max(1, len(raw)), per_page):
        pages.append(PageText(page_no=idx // per_page + 1,
                              text=raw[idx:idx + per_page],
                              extractor="plain-text", confidence=100.0))
    return pages


def region_for_span(words: list[Word], phrase: str) -> dict[str, Any] | None:
    """Locate a phrase in a page's word geometry and return its bounding box.

    This is what turns "page 7" into "page 7, the pressure findings table" -- the
    difference between a citation and a pointer.
    """
    if not words or not phrase:
        return None
    target = [t for t in re.findall(r"[A-Za-z0-9.\-/]+", phrase.lower()) if t]
    if not target:
        return None
    toks = [re.sub(r"[^a-z0-9.\-/]", "", w.text.lower()) for w in words]
    n = len(target)
    best: tuple[int, int] | None = None
    best_hits = 0
    for i in range(len(toks)):
        window = toks[i:i + n + 4]
        hits = sum(1 for t in target if t in window)
        if hits > best_hits:
            best_hits, best = hits, (i, min(len(words), i + n + 4))
    if not best or best_hits < max(1, n // 2):
        return None
    sel = words[best[0]:best[1]]
    if not sel:
        return None
    return {
        "x0": round(min(w.x0 for w in sel), 1), "y0": round(min(w.y0 for w in sel), 1),
        "x1": round(max(w.x1 for w in sel), 1), "y1": round(max(w.y1 for w in sel), 1),
        "match_ratio": round(best_hits / n, 2),
    }
