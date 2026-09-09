"""Hybrid local retrieval: dense + lexical, fused, then reranked.

Why hybrid: industrial documents are full of tags (V-204, PSV-101-A) and clause
numbers that dense embeddings blur together, and full of prose that keyword search
misses. Reciprocal rank fusion over both is markedly more reliable than either
alone, and it degrades gracefully -- if the embedder is unavailable the system
still retrieves lexically instead of returning nothing.

Reranking is optional and local: a small cross-check pass by the resident model,
used only when the caller asks for it, because it costs a generation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from .. import db
from ..config import settings
from . import embed

RRF_K = 60.0


@dataclass
class Passage:
    chunk_id: str
    doc_id: str
    doc_title: str
    doc_class: str
    page_no: int
    text: str
    region: dict[str, Any] | None = None
    dense_score: float = 0.0
    lexical_score: float = 0.0
    score: float = 0.0
    rerank_score: float | None = None
    why: list[str] = field(default_factory=list)

    def citation(self) -> str:
        return f"{self.doc_title}, page {self.page_no}"

    def to_dict(self) -> dict[str, Any]:
        return {"chunk_id": self.chunk_id, "doc_id": self.doc_id,
                "doc_title": self.doc_title, "doc_class": self.doc_class,
                "page_no": self.page_no, "text": self.text, "region": self.region,
                "score": round(self.score, 4),
                "dense_score": round(self.dense_score, 4),
                "lexical_score": round(self.lexical_score, 4),
                "rerank_score": self.rerank_score, "citation": self.citation(),
                "why": self.why}


# Function words carry no discriminative weight but do dilute BM25 across the
# whole corpus, which matters a lot when the corpus is a few hundred clauses.
_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "are", "was", "were",
    "any", "all", "its", "has", "have", "had", "not", "but", "which", "what",
    "when", "who", "how", "why", "into", "than", "then", "there", "their",
    "shall", "may", "can", "will", "would", "should", "does", "did", "you",
    "your", "our", "per",
}


def _fts_escape(q: str) -> str:
    """FTS5 MATCH syntax is picky; quote every term and OR them."""
    terms = re.findall(r"[A-Za-z0-9][A-Za-z0-9._/\-]*", q)
    kept: list[str] = []
    for t in terms:
        if len(t) <= 1 or t.lower() in _STOPWORDS:
            continue
        if t not in kept:
            kept.append(t)
    if not kept:
        kept = [t for t in terms if len(t) > 1]
    if not kept:
        return ""
    return " OR ".join(f'"{t}"' for t in kept[:24])


def _doc_filter_sql(doc_ids: list[str] | None,
                    doc_classes: list[str] | None) -> tuple[str, list[Any]]:
    clauses, params = [], []
    if doc_ids:
        clauses.append(f"c.doc_id IN ({','.join('?' * len(doc_ids))})")
        params += doc_ids
    if doc_classes:
        clauses.append(f"d.doc_class IN ({','.join('?' * len(doc_classes))})")
        params += doc_classes
    return (" AND " + " AND ".join(clauses)) if clauses else "", params


def lexical_search(query: str, k: int, doc_ids: list[str] | None = None,
                   doc_classes: list[str] | None = None) -> list[tuple[str, float]]:
    match = _fts_escape(query)
    if not match:
        return []
    where, params = _doc_filter_sql(doc_ids, doc_classes)
    sql = (
        "SELECT f.chunk_id AS cid, bm25(chunks_fts) AS rank "
        "FROM chunks_fts f JOIN chunks c ON c.id = f.chunk_id "
        "JOIN documents d ON d.id = c.doc_id "
        f"WHERE chunks_fts MATCH ?{where} ORDER BY rank LIMIT ?"
    )
    try:
        rows = db.query(sql, [match, *params, k])
    except Exception:
        return []
    # SQLite's bm25() returns a *negative* score where more negative is a better
    # match. Clamping it at zero -- the obvious-looking way to make it positive --
    # collapses every result to the same value and silently disables lexical
    # ranking altogether. Negate instead.
    return [(r["cid"], -float(r["rank"])) for r in rows]


def dense_search(query: str, k: int, doc_ids: list[str] | None = None,
                 doc_classes: list[str] | None = None) -> list[tuple[str, float]]:
    if not embed.available():
        return []
    where, params = _doc_filter_sql(doc_ids, doc_classes)
    rows = db.query(
        "SELECT c.id AS cid, c.embedding AS emb FROM chunks c "
        f"JOIN documents d ON d.id = c.doc_id WHERE c.embedding IS NOT NULL{where}",
        params)
    if not rows:
        return []
    ids = [r["cid"] for r in rows]
    mat = np.vstack([embed.from_blob(r["emb"]) for r in rows])
    q = embed.encode_one(query)
    sims = mat @ q                       # both sides are L2-normalised
    order = np.argsort(-sims)[:k]
    return [(ids[i], float(sims[i])) for i in order]


def _hydrate(chunk_ids: list[str]) -> dict[str, Passage]:
    if not chunk_ids:
        return {}
    marks = ",".join("?" * len(chunk_ids))
    rows = db.query(
        "SELECT c.id, c.doc_id, c.page_no, c.text, c.region, d.title, d.doc_class "
        f"FROM chunks c JOIN documents d ON d.id=c.doc_id WHERE c.id IN ({marks})",
        chunk_ids)
    return {r["id"]: Passage(chunk_id=r["id"], doc_id=r["doc_id"],
                             doc_title=r["title"], doc_class=r["doc_class"] or "other",
                             page_no=r["page_no"] or 0, text=r["text"],
                             region=db.jload(r["region"]))
            for r in rows}


def search(query: str, *, k: int | None = None, doc_ids: list[str] | None = None,
           doc_classes: list[str] | None = None,
           rerank: bool = False, model: str | None = None) -> list[Passage]:
    """Retrieve evidence passages for a query."""
    k = k or settings.knowledge.retrieve_k
    pool = max(k * 3, settings.knowledge.retrieve_k)

    dense = dense_search(query, pool, doc_ids, doc_classes)
    lexical = lexical_search(query, pool, doc_ids, doc_classes)
    if not dense and not embed.available():
        # Loud, once per query, rather than a silent halving of retrieval quality.
        from .. import audit
        audit.record("knowledge", "dense_retrieval_unavailable", outcome="DEGRADED",
                     detail=embed.status())

    # Reciprocal rank fusion: robust to the two scorers being on different scales.
    fused: dict[str, float] = {}
    for rank, (cid, _s) in enumerate(dense):
        fused[cid] = fused.get(cid, 0.0) + settings.knowledge.dense_weight / (RRF_K + rank + 1)
    for rank, (cid, _s) in enumerate(lexical):
        w = 1.0 - settings.knowledge.dense_weight
        fused[cid] = fused.get(cid, 0.0) + w / (RRF_K + rank + 1)

    dense_map = dict(dense)
    lex_map = dict(lexical)
    ordered = sorted(fused, key=lambda c: -fused[c])[:k]
    passages = _hydrate(ordered)

    out: list[Passage] = []
    for cid in ordered:
        p = passages.get(cid)
        if not p:
            continue
        p.dense_score = dense_map.get(cid, 0.0)
        p.lexical_score = lex_map.get(cid, 0.0)
        p.score = fused[cid]
        if cid in dense_map and cid in lex_map:
            p.why.append("matched both semantically and by keyword")
        elif cid in dense_map:
            p.why.append("semantic match")
        else:
            p.why.append("keyword match")
        out.append(p)

    if rerank and out:
        out = rerank_passages(query, out, model=model)
    return out


_RERANK_PROMPT = """You are ranking retrieved passages for relevance to a query.

Query: {query}

Passages:
{passages}

Return ONLY a JSON object mapping each passage number to a relevance score from 0
to 10, like {{"1": 8, "2": 0, "3": 3}}. Score 0 if a passage does not help answer
the query. Do not explain."""


def rerank_passages(query: str, passages: list[Passage], *,
                    model: str | None = None,
                    keep: int | None = None) -> list[Passage]:
    """Local LLM rerank. Falls back to fusion order if the model misbehaves."""
    from ..gateway import gateway
    from ..gateway.base import ChatMessage, GenRequest
    from ..gateway.registry import registry

    keep = keep or settings.knowledge.rerank_k
    if not model:
        cands = [c for c in registry.all() if c.cap("extraction") >= 0.6]
        if not cands:
            return passages[:keep]
        model = max(cands, key=lambda c: c.cap("speed")).name

    listing = "\n\n".join(
        f"[{i + 1}] ({p.doc_title} p.{p.page_no}) {p.text[:600]}"
        for i, p in enumerate(passages))
    res = gateway.generate(GenRequest(
        messages=[ChatMessage("user", _RERANK_PROMPT.format(query=query,
                                                            passages=listing))],
        model=model, temperature=0.0, max_tokens=400, json_mode=True,
        reasoning="low", timeout_s=180))
    scores = db.jload(res.text, {}) if res.ok else {}
    if not isinstance(scores, dict) or not scores:
        return passages[:keep]
    for i, p in enumerate(passages):
        try:
            p.rerank_score = float(scores.get(str(i + 1), 0))
        except (TypeError, ValueError):
            p.rerank_score = 0.0
    ranked = sorted(passages, key=lambda p: -(p.rerank_score or 0.0))
    # Drop passages the reranker judged irrelevant -- feeding them forward is how
    # a grounded answer quietly becomes an ungrounded one.
    ranked = [p for p in ranked if (p.rerank_score or 0.0) > 0] or passages[:keep]
    return ranked[:keep]


def evidence_package(query: str, **kwargs: Any) -> dict[str, Any]:
    """Retrieval result shaped for the agent and the evidence panel."""
    passages = search(query, **kwargs)
    return {
        "query": query,
        "count": len(passages),
        "passages": [p.to_dict() for p in passages],
        "context": "\n\n".join(
            f"[{i + 1}] SOURCE: {p.doc_title}, page {p.page_no}\n{p.text}"
            for i, p in enumerate(passages)),
    }
