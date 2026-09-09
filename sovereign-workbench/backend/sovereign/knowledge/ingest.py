"""Document ingestion: file -> pages -> chunks -> local index.

Chunks carry the page number *and* the bounding box of their text, so every
retrieved passage can be shown highlighted on the original page image. That is
the precondition for Class A evidence.
"""
from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path
from typing import Any

from .. import audit, db
from ..config import UPLOAD_DIR, settings
from . import embed, ocr

PDF_EXT = {".pdf"}
IMG_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
TXT_EXT = {".txt", ".md", ".csv", ".log", ".tsv", ".json", ".yaml", ".yml"}
# Office formats carry their text natively; there is nothing to OCR, and reading
# them properly is what makes "spreadsheet work" a real capability rather than a
# file-type rejection.
XLSX_EXT = {".xlsx", ".xlsm"}
DOCX_EXT = {".docx"}
PPTX_EXT = {".pptx"}

DOC_CLASSES = ("sop", "manual", "inspection_report", "correspondence",
               "drawing", "specification", "other")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def guess_class(title: str, text: str = "") -> str:
    """Classify a document.

    The title is checked first and on its own. Body text is unreliable here
    because industrial documents cite each other constantly -- an equipment
    register that references SOP-MECH-021 would otherwise be filed as a
    procedure, and would then be excluded from register lookups.
    """
    title_l = title.lower()
    if re.search(r"register|equipment list|asset list", title_l):
        return "specification"
    if re.search(r"p&id|pid-|isometric|drawing|dwg", title_l):
        return "drawing"
    if re.search(r"\bsop\b|sop-|procedure", title_l):
        return "sop"
    if re.search(r"inspection report|\bir-\d|ndt|thickness", title_l):
        return "inspection_report"

    t = f"{title} {text[:1500]}".lower()
    if re.search(r"p&id|piping and instrument|isometric|drawing no|dwg", t):
        return "drawing"
    if re.search(r"plant equipment register|equipment register", t):
        return "specification"
    if re.search(r"standard operating procedure|procedure no", t):
        return "sop"
    if re.search(r"inspection report|inspection findings|ndt report|thickness survey", t):
        return "inspection_report"
    if re.search(r"\bmanual\b|operating manual|maintenance manual", t):
        return "manual"
    if re.search(r"letter|memo|correspondence|ref no.*dated", t):
        return "correspondence"
    if re.search(r"specification|data sheet|datasheet", t):
        return "specification"
    return "other"


# A numbered clause is the natural retrieval unit in a procedure: "clause 4.4
# says X" is exactly the granularity an engineer cites, and splitting there keeps
# a threshold and its rule in the same chunk.
_CLAUSE_BREAK = re.compile(r"\n(?=\s*\d{1,2}\.\d{1,2}\s)")
_SECTION_BREAK = re.compile(r"\n(?=\s*\d{1,2}\.\s+[A-Z])")


def _chunk_page(text: str, target_tokens: int, overlap: int) -> list[str]:
    """Clause- and paragraph-aware chunking with a token-ish budget (~4 chars/token)."""
    target_chars = target_tokens * 4
    overlap_chars = overlap * 4

    blocks = [text]
    for pattern in (_SECTION_BREAK, _CLAUSE_BREAK):
        nxt: list[str] = []
        for b in blocks:
            nxt.extend(x for x in pattern.split(b) if x.strip())
        blocks = nxt

    paras = [p.strip() for b in blocks
             for p in re.split(r"\n\s*\n", b) if p.strip()]
    if not paras:
        return []
    chunks: list[str] = []
    cur = ""
    for p in paras:
        if len(p) > target_chars * 1.6:
            # A single huge paragraph (often a table) -- split on line boundaries.
            lines = p.splitlines()
            buf = ""
            for ln in lines:
                if len(buf) + len(ln) > target_chars and buf:
                    chunks.append(buf.strip())
                    buf = buf[-overlap_chars:] if overlap_chars else ""
                buf += ln + "\n"
            if buf.strip():
                chunks.append(buf.strip())
            continue
        if len(cur) + len(p) > target_chars and cur:
            chunks.append(cur.strip())
            cur = cur[-overlap_chars:] if overlap_chars else ""
        cur += p + "\n\n"
    if cur.strip():
        chunks.append(cur.strip())
    return [c for c in chunks if len(c) > 25]


def ingest_file(path: str | Path, *, title: str | None = None,
                doc_class: str | None = None, use_vlm: bool = True,
                copy: bool = True) -> dict[str, Any]:
    src = Path(path).expanduser().resolve()
    if not src.is_file():
        raise FileNotFoundError(src)

    digest = sha256_file(src)
    existing = db.query_one(
        "SELECT id, title, pages, status FROM documents WHERE sha256=?", (digest,))
    if existing and existing["status"] == "READY":
        return {"doc_id": existing["id"], "title": existing["title"],
                "pages": existing["pages"], "reused": True,
                "detail": "identical content already indexed"}
    if existing:
        # A previous attempt failed and left a row behind. Deduplicating against
        # it makes the failure permanent: the file can never be retried, because
        # its content hash now matches a document that holds nothing. Clear it
        # and ingest properly -- a retry after a fix has to be able to succeed.
        for table in ("chunks", "pages"):
            db.execute(f"DELETE FROM {table} WHERE doc_id=?", (existing["id"],))
        db.execute("DELETE FROM documents WHERE id=?", (existing["id"],))
        audit.record("knowledge", "failed_ingest_cleared",
                     detail={"doc_id": existing["id"], "title": existing["title"],
                             "previous_status": existing["status"]})

    doc_id = db.new_id("doc")
    stored = src
    if copy:
        stored = UPLOAD_DIR / f"{doc_id}{src.suffix.lower()}"
        shutil.copyfile(src, stored)

    ext = src.suffix.lower()
    db.insert("documents", {
        "id": doc_id, "title": title or src.name, "kind": ext.lstrip("."),
        "doc_class": doc_class or "other", "path": str(stored), "sha256": digest,
        "bytes": stored.stat().st_size, "status": "PROCESSING",
        "created_at": db.now()})

    try:
        if ext in PDF_EXT:
            pages = ocr.extract_pdf(stored, doc_id, use_vlm=use_vlm)
        elif ext in IMG_EXT:
            pages = ocr.extract_image(stored, doc_id, use_vlm=use_vlm)
        elif ext in XLSX_EXT:
            pages = ocr.extract_xlsx(stored)
        elif ext in DOCX_EXT:
            pages = ocr.extract_docx(stored)
        elif ext in PPTX_EXT:
            pages = ocr.extract_pptx(stored)
        elif ext in TXT_EXT:
            pages = ocr.extract_text_file(stored)
        else:
            supported = sorted(PDF_EXT | IMG_EXT | TXT_EXT | XLSX_EXT
                               | DOCX_EXT | PPTX_EXT)
            raise ValueError(
                f"unsupported file type {ext!r}. Supported: "
                f"{', '.join(supported)}. Legacy .xls/.doc/.ppt are not read "
                f"directly; convert them with LibreOffice first "
                f"(soffice --headless --convert-to xlsx <file>).")
    except Exception as exc:
        db.update("documents", "id", doc_id,
                  {"status": "FAILED", "status_detail": str(exc)[:400]})
        audit.record("knowledge", "ingest_failed", outcome="FAILED",
                     detail={"doc": str(src), "error": str(exc)[:300]})
        raise

    ok_pages = [p for p in pages if p.ok and p.text.strip()]
    failed = [p.page_no for p in pages if not p.ok]

    for p in pages:
        db.insert("pages", {
            "id": f"{doc_id}-p{p.page_no}", "doc_id": doc_id, "page_no": p.page_no,
            "width": p.width, "height": p.height, "text": p.text,
            "extractor": p.extractor, "ocr_conf": p.confidence,
            "image_path": p.image_path, "words": db.jdump(p.to_words_json()),
        })

    # Chunk + locate + embed.
    rows: list[dict[str, Any]] = []
    for p in ok_pages:
        for ordinal, ctext in enumerate(_chunk_page(
                p.text, settings.knowledge.chunk_tokens,
                settings.knowledge.chunk_overlap)):
            region = ocr.region_for_span(p.words, ctext[:220]) if p.words else None
            rows.append({
                "id": db.new_id("chunk"), "doc_id": doc_id, "page_no": p.page_no,
                "ordinal": ordinal, "text": ctext, "region": db.jdump(region),
                "tokens": len(ctext) // 4,
            })

    if rows and embed.available():
        vecs = embed.encode([r["text"] for r in rows])
        for r, v in zip(rows, vecs):
            r["embedding"] = embed.to_blob(v)
    else:
        for r in rows:
            r["embedding"] = None

    with db.tx() as c:
        for r in rows:
            c.execute(
                "INSERT INTO chunks (id,doc_id,page_no,ordinal,text,region,tokens,"
                "embedding) VALUES (?,?,?,?,?,?,?,?)",
                (r["id"], r["doc_id"], r["page_no"], r["ordinal"], r["text"],
                 r["region"], r["tokens"], r["embedding"]))
            c.execute("INSERT INTO chunks_fts (text, chunk_id) VALUES (?,?)",
                      (r["text"], r["id"]))

    head = " ".join(p.text for p in ok_pages[:2])[:1500]
    final_class = doc_class or guess_class(title or src.name, head)
    status = "READY" if ok_pages else "EXTRACTION_FAILED"
    db.update("documents", "id", doc_id, {
        "pages": len(pages), "status": status, "doc_class": final_class,
        "status_detail": (f"{len(ok_pages)}/{len(pages)} pages extracted, "
                          f"{len(rows)} chunks indexed"
                          + (f"; pages {failed} could not be read" if failed else "")),
        "meta": db.jdump({"extractors": sorted({p.extractor for p in pages}),
                          "failed_pages": failed,
                          "mean_conf": round(sum(p.confidence for p in ok_pages)
                                             / len(ok_pages), 1) if ok_pages else 0.0}),
    })
    audit.record("knowledge", "document_ingested",
                 outcome="OK" if ok_pages else "DEGRADED",
                 detail={"doc_id": doc_id, "title": title or src.name,
                         "pages": len(pages), "chunks": len(rows),
                         "failed_pages": failed, "class": final_class})
    return {"doc_id": doc_id, "title": title or src.name, "pages": len(pages),
            "chunks": len(rows), "failed_pages": failed, "doc_class": final_class,
            "status": status, "reused": False,
            "extractors": sorted({p.extractor for p in pages})}


def ingest_directory(root: str | Path, doc_class: str | None = None,
                     use_vlm: bool = True) -> list[dict[str, Any]]:
    out = []
    known = PDF_EXT | IMG_EXT | TXT_EXT | XLSX_EXT | DOCX_EXT | PPTX_EXT
    for p in sorted(Path(root).rglob("*")):
        if p.is_file() and p.suffix.lower() in known:
            try:
                out.append(ingest_file(p, doc_class=doc_class, use_vlm=use_vlm))
            except Exception as exc:
                out.append({"path": str(p), "error": str(exc)[:300]})
    return out


def reindex_embeddings() -> int:
    """Backfill embeddings for chunks indexed before the embedder was available."""
    rows = db.query("SELECT id, text FROM chunks WHERE embedding IS NULL")
    if not rows or not embed.available():
        return 0
    texts = [r["text"] for r in rows]
    vecs = embed.encode(texts)
    with db.tx() as c:
        for r, v in zip(rows, vecs):
            c.execute("UPDATE chunks SET embedding=? WHERE id=?",
                      (embed.to_blob(v), r["id"]))
    return len(rows)
