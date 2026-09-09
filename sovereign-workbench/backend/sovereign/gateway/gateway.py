"""Model Gateway.

Single entry point for all inference. Responsibilities:

* dispatch a request to whichever backend serves the chosen model;
* health-check backends and open a circuit breaker on repeated failure;
* fall back down a compatible chain rather than failing the product
  ("an inference backend failure must not equal a product failure");
* enforce a hard deadline on every call, because the documented worst failure
  mode of local MoE serving is a silent hang, not an error;
* expose residency state and the pin/evict actuator to the scheduler.
"""
from __future__ import annotations

import base64
import threading
import time
from pathlib import Path
from typing import Any, Iterator

from .. import audit, db
from ..config import settings
from .base import ChatMessage, GenRequest, GenResult, ModelBackend
from .ollama_backend import OllamaBackend
from .openai_backend import OpenAICompatBackend
from .registry import ModelCard, registry

# After this many consecutive failures a model is taken out of rotation for
# `BREAKER_COOLDOWN_S`, so a wedged backend cannot absorb the whole queue.
BREAKER_THRESHOLD = 3
BREAKER_COOLDOWN_S = 120.0


class ModelGateway:
    def __init__(self) -> None:
        self._backends: dict[str, ModelBackend] = {}
        self._lock = threading.RLock()
        self._register_backends()

    # -- backends ---------------------------------------------------------
    def _register_backends(self) -> None:
        self._backends["ollama"] = OllamaBackend()
        if settings.backends.openai_compat_url:
            self._backends["openai_compat"] = OpenAICompatBackend()
        if settings.backends.llamacpp_url:
            self._backends["llamacpp"] = OpenAICompatBackend(
                settings.backends.llamacpp_url, label="llamacpp")

    def backend_for(self, card: ModelCard) -> ModelBackend | None:
        return self._backends.get(card.backend)

    def backend_health(self) -> dict[str, Any]:
        return {name: b.health() for name, b in self._backends.items()}

    # -- catalogue reconciliation ----------------------------------------
    def sync_registry(self) -> dict[str, Any]:
        """Disable registry entries the local backends do not actually serve.

        The catalogue may legitimately describe more models than this machine
        holds; the workbench must never offer one it cannot run.
        """
        registry.seed()
        served: dict[str, set[str]] = {}
        for bname, backend in self._backends.items():
            names = set()
            for m in backend.list_models():
                n = m.get("name") or m.get("model") or m.get("id") or ""
                if n:
                    names.add(n)
                    names.add(n.split(":")[0])
            served[bname] = names

        report: dict[str, Any] = {"available": [], "unavailable": []}
        for card in registry.all(include_disabled=True):
            names = served.get(card.backend, set())
            ok = bool(names) and (card.backend_ref in names
                                  or card.backend_ref.split(":")[0] in names)
            if ok != card.enabled:
                registry.set_enabled(card.name, ok)
            (report["available"] if ok else report["unavailable"]).append(card.name)
        registry.invalidate()
        audit.record("gateway", "registry_sync", detail=report)
        return report

    # -- health / breaker -------------------------------------------------
    def _breaker_open(self, model: str) -> bool:
        row = db.query_one("SELECT open_until FROM model_health WHERE model=?", (model,))
        return bool(row and (row["open_until"] or 0) > time.time())

    def _note_result(self, model: str, ok: bool, detail: str = "") -> None:
        row = db.query_one("SELECT failures FROM model_health WHERE model=?", (model,))
        failures = 0 if ok else ((row["failures"] if row else 0) or 0) + 1
        open_until = 0.0
        if failures >= BREAKER_THRESHOLD:
            open_until = time.time() + BREAKER_COOLDOWN_S
            audit.record("gateway", "circuit_open", outcome="DEGRADED",
                         detail={"model": model, "failures": failures,
                                 "cooldown_s": BREAKER_COOLDOWN_S})
        db.upsert("model_health", {
            "model": model, "status": "healthy" if ok else "failing",
            "detail": detail[:300], "failures": failures, "open_until": open_until,
            "checked_at": time.time(),
        }, key="model")

    def health_report(self) -> list[dict[str, Any]]:
        rows = {r["model"]: dict(r) for r in db.query("SELECT * FROM model_health")}
        out = []
        for c in registry.all(include_disabled=True):
            h = rows.get(c.name, {})
            out.append({
                "model": c.name, "backend": c.backend, "enabled": c.enabled,
                "status": ("breaker_open" if self._breaker_open(c.name)
                           else h.get("status", "unknown")),
                "failures": h.get("failures", 0), "detail": h.get("detail", ""),
                "role": c.role, "adapter": c.prompt_adapter,
            })
        return out

    # -- residency --------------------------------------------------------
    def resident(self) -> list[dict[str, Any]]:
        out = []
        for bname, backend in self._backends.items():
            for m in backend.resident_models():
                card = registry.get(m["name"])
                out.append({**m, "backend": bname,
                            "model": card.name if card else m["name"]})
        return out

    def pin(self, card: ModelCard, seconds: int = 1800) -> float:
        """Make a model resident. Returns measured load seconds."""
        b = self.backend_for(card)
        if not b or not b.supports_residency_control:
            return 0.0
        t0 = time.time()
        ok = b.pin(card, seconds)
        cost = round(time.time() - t0, 2)
        db.insert("residency_log", {
            "action": "pin" if ok else "pin_failed", "model": card.name,
            "reason": "scheduler requested residency", "cost_s": cost,
            "vram_mb": card.residency_mb, "ts": time.time()})
        return cost

    def evict(self, card: ModelCard, reason: str = "") -> bool:
        b = self.backend_for(card)
        if not b or not b.supports_residency_control:
            return False
        t0 = time.time()
        ok = b.evict(card)
        db.insert("residency_log", {
            "action": "evict" if ok else "evict_failed", "model": card.name,
            "reason": reason or "scheduler eviction", "cost_s": round(time.time() - t0, 2),
            "vram_mb": card.residency_mb, "ts": time.time()})
        audit.record("runtime", "model_evicted", outcome="OK" if ok else "FAILED",
                     detail={"model": card.name, "reason": reason})
        return ok

    # -- generation -------------------------------------------------------
    def fallback_chain(self, card: ModelCard, need_vision: bool = False) -> list[ModelCard]:
        """Compatible alternatives, cheapest-to-run first.

        Compatibility means capability coverage, not family: a vision task may
        only fall back to another vision model, and we never silently downgrade
        a reasoning task below 0.6 on the reasoning axis.
        """
        cands = []
        for c in registry.all():
            if c.name == card.name or self._breaker_open(c.name):
                continue
            if need_vision and c.cap("vision") < 0.4:
                continue
            if not need_vision and c.cap("text") < 0.5:
                continue
            if card.cap("reasoning") >= 0.8 and c.cap("reasoning") < 0.6:
                continue
            cands.append(c)
        return sorted(cands, key=lambda c: (-c.cap("speed"), c.residency_mb))

    def generate(self, req: GenRequest, *, allow_fallback: bool = True,
                 task_id: str | None = None) -> GenResult:
        card = registry.get(req.model)
        if card is None:
            return GenResult(text="", ok=False, error=f"unknown model {req.model!r}")

        need_vision = any(getattr(m, "images", None) for m in req.messages)
        chain = [card] + (self.fallback_chain(card, need_vision) if allow_fallback else [])
        attempts: list[dict[str, Any]] = []

        for idx, c in enumerate(chain):
            if self._breaker_open(c.name):
                attempts.append({"model": c.name, "skipped": "circuit breaker open"})
                continue
            backend = self.backend_for(c)
            if backend is None:
                attempts.append({"model": c.name, "skipped": "backend not registered"})
                continue

            sub = GenRequest(**{**req.__dict__, "model": c.name})
            sub.timeout_s = min(req.timeout_s, settings.limits.generation_timeout_s)
            res = backend.generate(sub, c)

            # An empty completion is a failure, not a success. This is exactly how
            # a mis-adapted harmony model presents, and it must not reach the user
            # as a blank answer.
            if res.ok and res.empty:
                res.ok = False
                res.error = "backend returned an empty completion"
                res.finish_reason = "empty"

            self._note_result(c.name, res.ok, res.error)
            attempts.append({"model": c.name, "ok": res.ok, "error": res.error,
                             "total_s": res.total_s, "tokens": res.completion_tokens})
            if res.ok:
                if idx > 0:
                    audit.record("gateway", "fallback_used", outcome="DEGRADED",
                                 task_id=task_id,
                                 detail={"requested": card.name, "served_by": c.name,
                                         "attempts": attempts})
                res.raw["attempts"] = attempts
                self._record_observation(c, res)
                return res

        audit.record("gateway", "generation_failed", outcome="FAILED", task_id=task_id,
                     detail={"requested": card.name, "attempts": attempts})
        return GenResult(text="", ok=False, model=card.name,
                         error="all compatible backends failed",
                         raw={"attempts": attempts})

    def stream(self, req: GenRequest, task_id: str | None = None) -> Iterator[dict[str, Any]]:
        card = registry.get(req.model)
        if card is None:
            yield {"done": True, "result": GenResult(text="", ok=False,
                                                     error=f"unknown model {req.model!r}")}
            return
        backend = self.backend_for(card)
        if backend is None:
            yield {"done": True, "result": GenResult(text="", ok=False,
                                                     error="backend not registered")}
            return
        req.timeout_s = min(req.timeout_s, settings.limits.generation_timeout_s)
        final: GenResult | None = None
        for chunk in backend.stream(req, card):
            if chunk.get("done"):
                final = chunk.get("result")
            yield chunk
        if final is not None:
            if final.ok and final.empty:
                final.ok = False
                final.error = "backend returned an empty completion"
            self._note_result(card.name, final.ok, final.error)
            if final.ok:
                self._record_observation(card, final)

    def _record_observation(self, card: ModelCard, res: GenResult) -> None:
        """Fold live throughput back into the model profile.

        The scheduler's estimates improve with every real request instead of
        depending on a one-off benchmark.
        """
        if res.completion_tokens < 12 or res.decode_tps <= 0:
            return
        row = db.query_one("SELECT decode_tps, samples FROM model_profiles WHERE model=?",
                           (card.name,))
        if row and row["decode_tps"]:
            n = (row["samples"] or 1)
            tps = (row["decode_tps"] * n + res.decode_tps) / (n + 1)
            db.update("model_profiles", "model", card.name,
                      {"decode_tps": round(tps, 2), "samples": n + 1})
        else:
            db.upsert("model_profiles", {
                "model": card.name, "decode_tps": res.decode_tps, "samples": 1,
                "measured_at": time.time()}, key="model")
        registry.invalidate()

    # -- convenience ------------------------------------------------------
    # Long edge, in pixels, that images are reduced to before inference.
    # A 3B vision model tiles its input, so a full-resolution scan becomes
    # thousands of image tokens: on this hardware the runner terminates without
    # a response, which surfaces as a connection reset rather than an error.
    # Downscaling also costs nothing in accuracy for document reading, where the
    # limiting factor is glyph legibility rather than absolute resolution.
    MAX_IMAGE_EDGE = 1280

    @staticmethod
    def image_message(role: str, text: str, image_paths: list[str | Path],
                      max_edge: int | None = None) -> ChatMessage:
        from io import BytesIO

        from PIL import Image

        limit = max_edge or ModelGateway.MAX_IMAGE_EDGE
        m = ChatMessage(role=role, content=text)
        imgs = []
        for p in image_paths:
            path = Path(p)
            try:
                with Image.open(path) as im:
                    im = im.convert("RGB")
                    if max(im.size) > limit:
                        scale = limit / max(im.size)
                        im = im.resize((max(1, int(im.width * scale)),
                                        max(1, int(im.height * scale))),
                                       Image.LANCZOS)
                    buf = BytesIO()
                    im.save(buf, "JPEG", quality=88)
                    data = buf.getvalue()
            except Exception:
                # Not a decodable image, or PIL is unavailable: send it as-is and
                # let the backend decide.
                data = path.read_bytes()
            imgs.append(base64.b64encode(data).decode("ascii"))
        setattr(m, "images", imgs)
        return m


gateway = ModelGateway()
