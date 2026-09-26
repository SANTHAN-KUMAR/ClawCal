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
import re
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


_MEM_REFUSAL = re.compile(r"requires more system memory \(([\d.]+)\s*(GiB|MiB|GB|MB)\)",
                          re.I)


def _backend_memory_refusal(error: str | None) -> float | None:
    """The MB a backend said a model needs, if it refused a load for memory."""
    m = _MEM_REFUSAL.search(error or "")
    if not m:
        return None
    v = float(m.group(1))
    return v * 1024 if m.group(2).lower().startswith("gi") else \
        v * 1000 if m.group(2).lower() == "gb" else v


def _learn_footprint(card: ModelCard, need_mb: float) -> None:
    """Record a backend-measured footprint on the registry row.

    It is written to `est_vram_mb`, which the memory guard reads, and the row is
    marked edited by `backend-measured` so the shipped catalogue never
    overwrites a measurement with a guess.
    """
    if need_mb <= (card.est_vram_mb or 0):
        return
    db.update("model_registry", "name", card.name,
              {"est_vram_mb": int(need_mb), "edited_by": "backend-measured"})
    registry.invalidate()
    audit.record("gateway", "footprint_learned", outcome="MEASURED",
                 detail={"model": card.name, "need_mb": round(need_mb)})


class ModelGateway:
    def __init__(self) -> None:
        self._backends: dict[str, ModelBackend] = {}
        self._lock = threading.RLock()
        self._inflight: dict[str, int] = {}
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

        from .registry import ALT_REFS
        from ..control import decisions

        report: dict[str, Any] = {"available": [], "unavailable": [],
                                  "resolved": {}}
        for card in registry.all(include_disabled=True):
            names = served.get(card.backend, set())
            candidates = (card.backend_ref, *ALT_REFS.get(card.name, ()))
            hit = next((r for r in candidates if r in names), None)
            if hit is None:
                # A bare family name ("qwen3") matches only the card's own
                # ref, never an alias: qwen3:8b must not enable qwen3-32b.
                if card.backend_ref.split(":")[0] in names and \
                        ":" not in card.backend_ref:
                    hit = card.backend_ref
            ok = bool(names) and hit is not None
            if ok and hit != card.backend_ref:
                registry.set_backend_ref(card.name, hit)
                report["resolved"][card.name] = hit
            if ok != card.enabled:
                registry.set_enabled(card.name, ok)
                decisions.record(
                    "registry", "ENABLED" if ok else "DISABLED",
                    (f"{card.backend} serves {hit}" if ok else
                     f"{card.backend} does not serve any of {list(candidates)}"),
                    subject_kind="model", subject_id=card.name,
                    basis={"backend": card.backend, "served": sorted(names)[:40]})
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
    _RESIDENT_TTL_S = 1.0

    def resident(self, max_age_s: float | None = None) -> list[dict[str, Any]]:
        """What the backends hold in memory, cached for about a second.

        One admission evaluation asks this several times, and the scheduler
        evaluates every queued task per tick. Pin and evict invalidate the
        cache, so no decision acts on residency the gateway itself changed.
        """
        ttl = self._RESIDENT_TTL_S if max_age_s is None else max_age_s
        cached = getattr(self, "_resident_cache", None)
        if cached and time.time() - cached[0] < ttl:
            return [dict(m) for m in cached[1]]
        out = self._resident_query()
        self._resident_cache = (time.time(), out)
        return [dict(m) for m in out]

    def _invalidate_resident(self) -> None:
        self._resident_cache = None

    def _resident_query(self) -> list[dict[str, Any]]:
        out = []
        for bname, backend in self._backends.items():
            for m in backend.resident_models():
                card = registry.get(m["name"])
                out.append({**m, "backend": bname,
                            "model": card.name if card else m["name"]})
        return out

    # -- in-flight tracking, so a model is never evicted mid-generation -------
    def _begin(self, name: str) -> None:
        with self._lock:
            self._inflight[name] = self._inflight.get(name, 0) + 1

    def _end(self, name: str) -> None:
        with self._lock:
            self._inflight[name] = max(0, self._inflight.get(name, 0) - 1)

    def make_room(self, card: ModelCard, ctx_tokens: int | None = None
                  ) -> tuple[bool, str]:
        """memory_check, but evicting idle resident models if that makes it fit.

        An agent running on a text model calls a tool that needs the vision
        model; on an 8 GB GPU the two cannot be resident together, and the text
        model is idle between steps. Refusing the vision load there would
        silently degrade the page to unconfirmed OCR. Residency is the control
        plane's job: evict what is idle, record why, and load.
        """
        ok, why = self.memory_check(card, ctx_tokens)
        if ok:
            return ok, why
        with self._lock:
            idle = [m for m in self.resident()
                    if (m.get("model") or m.get("name")) != card.name
                    and not self._inflight.get(m.get("model") or m.get("name"), 0)]
        if not idle:
            return ok, why
        from .. import hardware
        snap = hardware.snapshot()
        freed_vram = sum(m.get("vram_mb", 0.0) or 0.0 for m in idle)
        freed_ram = sum(m.get("cpu_mb", 0.0) or 0.0 for m in idle)
        vram_free = max(0.0, snap["usable_vram_mb"] - snap["gpu"].get("used_mb", 0.0))
        fits, why2, _, _ = hardware.memory_verdict(card, vram_free + freed_vram, snap,
                                                   freed_ram_mb=freed_ram,
                                                   ctx_tokens=ctx_tokens)
        if not fits:
            return False, why
        before = hardware.gpu_state(max_age_s=0).used_mb
        for m in idle:
            other = registry.get(m.get("model") or m.get("name") or "")
            if other:
                self.evict(other, reason=f"idle; made room for {card.name}")
        # Unloading is asynchronous: the backend acknowledges the eviction before
        # the GPU has released the memory. Re-checking at once saw the old
        # numbers and refused the very load the eviction was for. Wait, bounded,
        # for the memory to actually come back, reading the GPU uncached.
        expect = before - 0.8 * freed_vram
        deadline = time.time() + 20.0
        while time.time() < deadline:
            if hardware.gpu_state(max_age_s=0).used_mb <= expect:
                break
            time.sleep(0.5)
        self._invalidate_resident()
        return self.memory_check(card, ctx_tokens)

    def memory_check(self, card: ModelCard,
                     ctx_tokens: int | None = None) -> tuple[bool, str]:
        """Last gate before weights load: will this fit in host memory now?

        Admission already prices RAM, but models are also reached from inside
        tools (the OCR fallback, the drawing cross-check) and by the backend
        itself when a request names a model that is not loaded. Every one of
        those passes here, so no path can load weights the machine cannot hold.
        """
        from .. import hardware
        names = {m.get("model") or m.get("name") for m in self.resident()}
        if card.name in names or card.backend_ref in names:
            return True, "already resident"
        if card.backend != "ollama":
            return True, "memory is managed by the external backend"
        snap = hardware.snapshot()
        vram_free = max(0.0, snap["usable_vram_mb"] - snap["gpu"].get("used_mb", 0.0))
        ok, why, need, room = hardware.memory_verdict(card, vram_free, snap,
                                                      ctx_tokens=ctx_tokens)
        if not ok:
            audit.record("gateway", "load_refused_memory", outcome="REFUSED",
                         detail={"model": card.name, "need_mb": round(need),
                                 "room_mb": round(room)})
        return ok, why

    def pin(self, card: ModelCard, seconds: int = 1800) -> float:
        """Make a model resident. Returns measured load seconds."""
        b = self.backend_for(card)
        if not b or not b.supports_residency_control:
            return 0.0
        ok, why = self.memory_check(card)
        if not ok:
            db.insert("residency_log", {
                "action": "pin_refused", "model": card.name, "reason": why[:300],
                "cost_s": 0.0, "vram_mb": card.residency_mb, "ts": time.time()})
            return 0.0
        t0 = time.time()
        ok = b.pin(card, seconds)
        cost = round(time.time() - t0, 2)
        self._invalidate_resident()
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
        self._invalidate_resident()
        db.insert("residency_log", {
            "action": "evict" if ok else "evict_failed", "model": card.name,
            "reason": reason or "scheduler eviction", "cost_s": round(time.time() - t0, 2),
            "vram_mb": card.residency_mb, "ts": time.time()})
        audit.record("runtime", "model_evicted", outcome="OK" if ok else "FAILED",
                     detail={"model": card.name, "reason": reason})
        from ..control import decisions
        decisions.record("residency", "EVICTED" if ok else "EVICT_FAILED",
                         reason or "scheduler eviction", subject_kind="model",
                         subject_id=card.name,
                         basis={"vram_mb": round(card.residency_mb, 1)})
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
            # A text task falls back to a text model of comparable standing. At
            # "text >= 0.5, fastest first" the small vision models led the list,
            # and a failed agent step turned into a string of pointless loads of
            # models that could not do the work — each one host RAM it did not have.
            if not need_vision and (c.modality == "vision"
                                    or c.cap("text") < min(0.85, card.cap("text"))):
                continue
            if card.cap("tool_use") >= 0.6 and c.cap("tool_use") < 0.6:
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
            fits, why = self.make_room(c, min(req.ctx_tokens, c.ctx_max))
            if not fits:
                attempts.append({"model": c.name, "skipped": why})
                continue

            sub = GenRequest(**{**req.__dict__, "model": c.name})
            sub.timeout_s = min(req.timeout_s, settings.limits.generation_timeout_s)
            self._begin(c.name)
            try:
                res = backend.generate(sub, c)
            finally:
                self._end(c.name)

            # An empty completion is a failure, not a success. This is exactly how
            # a mis-adapted harmony model presents, and it must not reach the user
            # as a blank answer.
            if res.ok and res.empty:
                res.ok = False
                res.error = res.error or "backend returned an empty completion"
                res.finish_reason = "empty"

            need_mb = _backend_memory_refusal(res.error) if not res.ok else None
            if need_mb:
                # The backend refused for memory, which says nothing about the
                # model's health — so the circuit breaker is not charged — and it
                # handed over a measurement: remember it, so the next attempt is
                # refused before it reaches the backend.
                _learn_footprint(c, need_mb)
                res.error = (f"insufficient memory: the backend reports {c.name} "
                             f"needs {need_mb:.0f} MB in total, more than this "
                             f"machine has available")
            else:
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
        skipped = [a["skipped"] for a in attempts if "host RAM" in str(a.get("skipped"))]
        return GenResult(text="", ok=False, model=card.name,
                         error=("insufficient memory: " + skipped[0]) if skipped
                         else "all compatible backends failed",
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
        fits, why = self.make_room(card, min(req.ctx_tokens, card.ctx_max))
        if not fits:
            yield {"done": True, "result": GenResult(
                text="", ok=False, error=f"insufficient memory: {why}")}
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
        from ..runtime import perfmodel
        perfmodel.record_decode(card.name, res.decode_tps)

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
