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
# Calibrated on the held-out dirty corpus (scripts/eval_dirty.py, OCR-only run),
# not chosen: on 21 real FUNSD scans every page read at >= 70 mean confidence
# recovered 62-96% of its words, while 11 of the 16 below 70 recovered under
# half — including a fax-quality page at 54% that recovered 7%. The previous
# 45 accepted exactly those pages.
MIN_TESS_CONF = 70.0
# Above this a rendered page of a scanned PDF is accepted on OCR alone, without
# the VLM cross-check, so a 200-page scan does not cost 200 VLM calls.
TRUST_TESS_CONF = 90.0
# Kept for callers that report it; images now need the cross-check instead.
MIN_TESS_CONF_IMAGE = MIN_TESS_CONF
VLM_CTX_TOKENS = 6144

# Two independent readers. A photographed receipt was read by Tesseract at 89%
# mean confidence with two thirds of its values wrong: confidence alone cannot be
# trusted on camera images at any threshold. OCR is ESTABLISHED only when an
# independent VLM reading of the same image agrees with it; otherwise the VLM's
# reading is used and labelled as interpretation.
AGREE_MIN_WORDS = 0.75           # token-bag F1 between the two readings
AGREE_MIN_NUMBERS = 0.90         # share of OCR's numbers the VLM also read


def _crosscheck_mode() -> str:
    import os
    return os.environ.get("SOVEREIGN_OCR_CROSSCHECK", "auto").lower()


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:[.,][0-9]+)?", (text or "").lower())


def agreement(a: str, b: str) -> dict[str, float]:
    """How far two independent readings of one image agree."""
    from collections import Counter
    ta, tb = Counter(_tokens(a)), Counter(_tokens(b))
    hit = sum((ta & tb).values())
    na, nb = sum(ta.values()), sum(tb.values())
    f1 = 2 * hit / (na + nb) if (na + nb) else 0.0
    nums_a = {t for t in ta if re.fullmatch(r"\d+(?:[.,]\d+)?", t)}
    nums_b = {t for t in tb if re.fullmatch(r"\d+(?:[.,]\d+)?", t)}
    # Symmetric: a number either reader saw must be seen by both. Checking only
    # that OCR's numbers were confirmed let an OCR reading that had *dropped*
    # a receipt's cash and change values be established as the page's text.
    both = nums_a | nums_b
    num = (len(nums_a & nums_b) / len(both)) if both else 1.0
    return {"words": round(f1, 3), "numbers": round(num, 3)}


_REFUSAL_PHRASES = re.compile(
    r"\b(?:too\s+(?:blurry|small|low[- ]resolution|faint|dark|pixelated)|"
    r"(?:cannot|can't|can\s+not|unable\s+to|not\s+able\s+to)\s+(?:be\s+)?"
    r"(?:read|make\s+out|discern|transcribe|decipher)|(?:not|isn't|is\s+not)\s+"
    r"(?:legible|readable|clear\s+enough)|illegible\s+(?:image|page|document))\b",
    re.I)


def degenerate(text: str) -> str | None:
    """Why a VLM reading is not a transcription, or None.

    A vision model shown something it cannot read does not always say so. It
    may describe its difficulty in prose, or it may loop, emitting the same
    phrase until its token budget runs out. Neither is a reading of the page,
    and storing either as the page's text would make it citable.
    """
    t = (text or "").strip()
    if not t:
        return "empty"
    head = t[:400]
    if re.search(r"transcribe\s+all\s+text\s+visible|do\s+not\s+summari[sz]e,\s+do\s+not\s+"
                 r"explain", head, re.I):
        return "the model echoed its instructions instead of reading the image"
    if _REFUSAL_PHRASES.search(head) and len(t) < 600:
        return "the model said it could not read the image"
    toks = re.findall(r"\S+", t.lower())
    if len(toks) >= 40:
        grams = [" ".join(toks[i:i + 4]) for i in range(len(toks) - 3)]
        from collections import Counter
        top, n = Counter(grams).most_common(1)[0]
        if n * 4 / len(toks) > 0.35:
            return f"the reading repeats itself ('{top}' x{n}), a model loop"
        if len(set(toks)) / len(toks) < 0.12:
            return "the reading is almost all repeated words, a model loop"
    return None


def _mostly_illegible(text: str) -> bool:
    marks = len(re.findall(r"\[\s*illegible\s*\]", text or "", re.I))
    words = len(re.findall(r"[A-Za-z0-9]+", re.sub(r"\[\s*illegible\s*\]", " ",
                                                   text or "", flags=re.I)))
    return marks >= 3 and marks > 0.3 * (words + marks)


def _decide(pt: "PageText", img: Path, text: str, words: list["Word"], conf: float,
            *, use_vlm: bool, vlm_model: str | None, hint: str,
            trust_alone_at: float | None) -> "PageText":
    """Choose between OCR and the VLM for one image, under the refusal contract.

    tesseract                 OCR, confirmed by the VLM (or trusted alone above
                              `trust_alone_at`): ESTABLISHED, with word geometry
    vlm                       the VLM's reading: INTERPRETED, page-level only
    tesseract-low-confidence  OCR only, no VLM to confirm it: DEGRADED
    failed                    nothing readable: CANNOT DETERMINE
    """
    mode = _crosscheck_mode()
    confident = bool(text) and conf >= MIN_TESS_CONF
    alone = (confident and trust_alone_at is not None and conf >= trust_alone_at
             and mode != "always") or (confident and mode == "never")
    if alone:
        pt.text, pt.words, pt.extractor, pt.confidence = text, words, "tesseract", conf
        return pt

    vtext = ""
    if use_vlm:
        vtext, _ = _vlm_read(img, hint=hint, model=vlm_model)
    if vtext and confident:
        ag = agreement(text, vtext)
        if ag["words"] >= AGREE_MIN_WORDS and ag["numbers"] >= AGREE_MIN_NUMBERS:
            pt.text, pt.words, pt.extractor, pt.confidence = text, words, "tesseract", conf
            pt.error = (f"OCR ({conf:.0f}%) confirmed by an independent VLM reading "
                        f"(word agreement {ag['words']:.0%}, numbers {ag['numbers']:.0%})")
            return pt
        pt.error = (f"OCR ({conf:.0f}%) and the VLM disagree (words {ag['words']:.0%}, "
                    f"numbers {ag['numbers']:.0%}); the VLM's reading is used and "
                    f"marked as interpretation")
    why_degenerate = degenerate(vtext) if vtext else None
    if vtext and why_degenerate and why_degenerate != "empty":
        pt.ok = False
        pt.extractor = "failed"
        pt.error = (f"the vision model's output is not a transcription: "
                    f"{why_degenerate}. Nothing on this page is established; "
                    f"the page image is kept for an engineer to read")
        return pt
    if vtext and _mostly_illegible(vtext):
        # The model itself says it cannot read the page. That is a refusal, not
        # an interpretation with holes in it.
        pt.ok = False
        pt.extractor = "failed"
        pt.error = ("the vision model marked most of this page [illegible]; it "
                    "cannot be read, and nothing on it is established")
        return pt
    if vtext:
        pt.text, pt.extractor, pt.confidence = vtext, "vlm", 60.0
        # OCR words stay only as geometry hints for highlighting, never as text.
        pt.words = [w for w in words if w.conf >= 60]
        if not pt.error and text:
            pt.error = (f"OCR confidence {conf:.0f}% is below the calibrated "
                        f"{MIN_TESS_CONF:.0f}%; the VLM's reading is used and marked "
                        f"as interpretation")
        return pt
    if text and conf >= 45.0:
        pt.text, pt.words = text, words
        pt.extractor = "tesseract-low-confidence"
        pt.confidence = conf
        pt.error = (f"only OCR was available and it could not be confirmed "
                    f"({conf:.0f}% mean confidence"
                    + ("" if confident else f", below the calibrated {MIN_TESS_CONF:.0f}%")
                    + "); treat every value on this page as unverified")
        return pt
    pt.ok = False
    pt.extractor = "failed"
    pt.error = (f"OCR confidence {conf:.0f}% and no readable VLM transcription; "
                f"the page could not be read")
    return pt


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


def _vlm_read(image_path: Path, hint: str = "",
              model: str | None = None) -> tuple[str, float]:
    """Last-resort read with the local vision model. No geometry, so page-level only."""
    from ..gateway import gateway
    from ..gateway.base import GenRequest
    from ..gateway.registry import registry

    vision = registry.vision_models()
    if model:
        vision = [c for c in vision if c.name == model] or \
            [c for c in [registry.get(model)] if c]
    if not vision:
        return "", 0.0
    prompt = (
        "Transcribe all text visible in this page image, preserving reading order, "
        "table rows and numbers exactly. Do not summarise, do not explain, do not "
        "add anything that is not written on the page. If a value is illegible "
        "write [illegible] in its place."
    )
    if hint:
        prompt += f"\n\nContext: {hint}"
    msg = gateway.image_message("user", prompt, [image_path])
    # Best measured reader first; if it cannot run here (memory refused it, its
    # breaker is open), the next that can. Asking only the best one meant a page
    # degraded to unconfirmed OCR while a smaller reader sat idle.
    for card in vision:
        res = gateway.generate(
            # An explicit, modest context: an image costs ~1-2K tokens and the
            # transcription up to 1.8K. Left at the default, the KV cache for an
            # 8B VLM alone overflows an 8 GB GPU into host RAM.
            GenRequest(messages=[msg], model=card.name, temperature=0.0,
                       max_tokens=1800, timeout_s=600, ctx_tokens=VLM_CTX_TOKENS,
                       reasoning="off"),
            allow_fallback=False)
        if res.ok and res.text.strip():
            # A VLM transcription is never as trustworthy as OCR with geometry.
            return _strip_preamble(res.text.strip()), 60.0
        if not _unavailable(res.error):
            return "", 0.0          # it ran and failed: do not shop for an answer
    return "", 0.0


def _unavailable(error: str | None) -> bool:
    """The reader could not run at all, as opposed to running and failing."""
    e = (error or "").lower()
    return any(k in e for k in ("insufficient memory", "host ram", "breaker",
                                "not loading", "unknown model", "all compatible"))


_PREAMBLE = re.compile(r"^(?:(?:sure|certainly|of course)[,!.]?\s*)?(?:here\s+is|here's|"
                       r"below\s+is)\s+(?:the\s+)?(?:full\s+|complete\s+|exact\s+)?"
                       r"(?:transcription|text|transcribed\s+text)[^:\n]{0,40}:\s*", re.I)


def _strip_preamble(text: str) -> str:
    """Drop chat framing a model adds around a transcription, and code fences."""
    t = _PREAMBLE.sub("", text, count=1)
    t = re.sub(r"^```[a-z]*\s*\n", "", t)
    t = re.sub(r"\n```\s*$", "", t)
    return t.strip()


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

            # -- tiers 2 and 3: OCR, cross-checked by the VLM where it matters
            img = render_page(doc, i, out_dir)
            pt.image_path = str(img)
            scale = RENDER_DPI / 72.0
            text, words, conf = _tesseract_tsv(img)
            # Word geometry back into PDF points, so regions overlay correctly.
            words = [Word(w.text, w.x0 / scale, w.y0 / scale,
                          w.x1 / scale, w.y1 / scale, w.conf) for w in words]
            pages.append(_decide(pt, img, text, words, conf, use_vlm=use_vlm,
                                 vlm_model=None, hint="",
                                 trust_alone_at=TRUST_TESS_CONF))
    finally:
        doc.close()
    return pages


def extract_image(path: Path, doc_id: str, *, use_vlm: bool = True,
                  vlm_model: str | None = None) -> list[PageText]:
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
    # A standalone image is always cross-checked: this is where a confident,
    # wrong OCR reading was measured.
    return [_decide(pt, dest, text, words, conf, use_vlm=use_vlm,
                    vlm_model=vlm_model,
                    hint="This may be a photograph, a scan or a handwritten note. "
                         "Transcribe every number exactly as written.",
                    trust_alone_at=None)]


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


def extract_cad(path: Path) -> list[PageText]:
    """Read a CAD drawing's text content for the knowledge base.

    The geometry is handled by the drawings pipeline; this is the searchable
    text, and it is *exact* — DXF stores the strings the engineer typed, so a
    tag indexed from here has no OCR error in it at all. That makes a CAD
    drawing the best possible source for the plant tag register that the raster
    reader later snaps against.
    """
    from ..drawings import cad

    data = cad.extract(path)
    a = data["assessment"]

    # A symbol library is a legend. Learning it here means the next drawing from
    # the same project can name its blocks instead of calling them unknown.
    try:
        legend = cad.learn_legend(path)
        if legend["learned"]:
            cad.save_legend(legend)
            a["legend_entries_learned"] = legend["learned"]
    except Exception:
        pass
    lines = [
        f"CAD DRAWING: {path.name}",
        f"Format: DXF {a.get('dxf_version')}"
        + (" (converted from DWG)" if a.get("converted_from_dwg") else ""),
        f"Entities: {a.get('entities')}",
        f"Layers: {', '.join(a.get('layers', [])[:30])}",
        "",
    ]
    if a.get("blocks_used"):
        lines.append("Symbol blocks used: " + ", ".join(
            f"{k} x{v}" for k, v in a["blocks_used"].items()))
        lines.append("")
    tags = sorted({t.text for t in data["tags"]})
    if tags:
        lines.append(f"Tags found ({len(tags)}): " + ", ".join(tags))
        lines.append("")
    by_class: dict[str, int] = {}
    for sym in data["symbols"]:
        by_class[sym.sym_class] = by_class.get(sym.sym_class, 0) + 1
    if by_class:
        lines.append("Symbols by class: " + ", ".join(
            f"{k} x{v}" for k, v in sorted(by_class.items())))
        lines.append("")

    text_entities = data.get("text_entities") or []
    if text_entities:
        lines.append("Text on the drawing:")
        lines.extend(f"  {t}" for t in text_entities[:2000])

    return [PageText(page_no=1, text="\n".join(lines), extractor="cad",
                     confidence=100.0,
                     width=data["page_box"].w, height=data["page_box"].h)]


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
