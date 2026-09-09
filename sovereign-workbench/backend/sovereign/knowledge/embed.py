"""Local embeddings. No network at inference time once the model is on disk."""
from __future__ import annotations

import threading
from typing import Any

import numpy as np

from ..config import MODEL_CACHE_DIR, settings

_model: Any = None
_lock = threading.Lock()


def _load() -> Any:
    global _model
    with _lock:
        if _model is None:
            from sentence_transformers import SentenceTransformer
            # Uses the standard local HF cache. Once the weights are on disk the
            # encoder never touches the network again, which is what matters here.
            _model = SentenceTransformer(settings.knowledge.embed_model, device="cpu")
    return _model


_load_error: str = ""


def available() -> bool:
    """Whether dense retrieval is usable, recording why not if it is not.

    Failing quietly here would leave the system retrieving lexically only, which
    still returns plausible passages — so the degradation is invisible unless it
    is reported.
    """
    global _load_error
    try:
        _load()
        return True
    except Exception as exc:
        _load_error = f"{type(exc).__name__}: {exc}"[:300]
        return False


def status() -> dict:
    ok = available()
    return {"available": ok, "model": settings.knowledge.embed_model,
            "dim": settings.knowledge.embed_dim,
            "error": "" if ok else _load_error}


def encode(texts: list[str], batch_size: int = 32) -> np.ndarray:
    """L2-normalised float32 embeddings, so cosine similarity is a dot product."""
    if not texts:
        return np.zeros((0, settings.knowledge.embed_dim), dtype=np.float32)
    m = _load()
    v = m.encode(texts, batch_size=batch_size, convert_to_numpy=True,
                 normalize_embeddings=True, show_progress_bar=False)
    return v.astype(np.float32)


def encode_one(text: str) -> np.ndarray:
    return encode([text])[0]


def to_blob(vec: np.ndarray) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)
