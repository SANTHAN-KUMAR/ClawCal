"""Ollama backend.

Ollama is the reference backend on this deployment because it is the one local
server that exposes a *residency actuator*: `keep_alive` on any request pins or
evicts weights, and `/api/ps` reports what is currently resident and how it is
split between GPU and CPU. That is the control surface the residency scheduler
needs (the role FreeToken's /v1/cache/rebuild plays in the survey document).
"""
from __future__ import annotations

import json
import time
from typing import Any, Iterator

import requests

from ..config import settings
from .adapters import get_adapter, parse_json_tool_reply
from .base import GenRequest, GenResult, ModelBackend


class OllamaBackend(ModelBackend):
    name = "ollama"
    supports_residency_control = True

    def __init__(self, url: str | None = None) -> None:
        self.url = (url or settings.backends.ollama_url).rstrip("/")
        self._session = requests.Session()

    # -- health / inventory ----------------------------------------------
    def health(self) -> dict[str, Any]:
        try:
            v = self._session.get(f"{self.url}/api/version", timeout=3).json()
            return {"status": "up", "version": v.get("version"), "url": self.url}
        except Exception as exc:
            return {"status": "down", "error": str(exc)[:200], "url": self.url}

    def list_models(self) -> list[dict[str, Any]]:
        try:
            r = self._session.get(f"{self.url}/api/tags", timeout=6).json()
            return r.get("models", []) or []
        except Exception:
            return []

    def resident_models(self) -> list[dict[str, Any]]:
        """What is loaded right now, and how much of it is on the GPU."""
        try:
            r = self._session.get(f"{self.url}/api/ps", timeout=4).json()
        except Exception:
            return []
        out = []
        for m in r.get("models", []) or []:
            total = m.get("size", 0) or 0
            vram = m.get("size_vram", 0) or 0
            out.append({
                "name": m.get("name"), "total_mb": round(total / 1e6, 1),
                "vram_mb": round(vram / 1e6, 1),
                "cpu_mb": round(max(0, total - vram) / 1e6, 1),
                "gpu_fraction": round(vram / total, 3) if total else 0.0,
                "expires_at": m.get("expires_at"),
                "context": m.get("context_length"),
            })
        return out

    # -- residency actuator ----------------------------------------------
    def pin(self, card: Any, seconds: int = 1800) -> bool:
        """Load weights and hold them resident. Empty prompt = load-only."""
        try:
            self._session.post(f"{self.url}/api/generate", timeout=600, json={
                "model": card.backend_ref, "prompt": "", "keep_alive": f"{seconds}s",
            })
            return True
        except Exception:
            return False

    def evict(self, card: Any) -> bool:
        """keep_alive=0 tells Ollama to unload immediately."""
        try:
            self._session.post(f"{self.url}/api/generate", timeout=120, json={
                "model": card.backend_ref, "prompt": "", "keep_alive": 0,
            })
            return True
        except Exception:
            return False

    # -- generation -------------------------------------------------------
    def _payload(self, req: GenRequest, card: Any) -> tuple[str, dict[str, Any], Any]:
        adapter = get_adapter(card.prompt_adapter)
        rp = adapter.render(req)
        opts = {
            "temperature": req.temperature,
            "top_p": req.top_p,
            "num_ctx": min(req.ctx_tokens, card.ctx_max),
            "num_predict": req.max_tokens,
        }
        if rp.stop:
            opts["stop"] = rp.stop
        keep = f"{int(settings.limits.min_residency_dwell_s * 20)}s"

        if rp.raw_prompt is not None:
            body = {"model": card.backend_ref, "prompt": rp.raw_prompt, "raw": True,
                    "options": opts, "keep_alive": keep}
            return "/api/generate", body, adapter

        body = {"model": card.backend_ref, "messages": rp.messages,
                "options": opts, "keep_alive": keep}
        if req.json_mode:
            body["format"] = "json"
        # Attach images for vision models (base64, already encoded by the caller).
        images = [im for m in req.messages for im in getattr(m, "images", []) or []]
        if images and body.get("messages"):
            body["messages"][-1]["images"] = images
        return "/api/chat", body, adapter

    def generate(self, req: GenRequest, card: Any) -> GenResult:
        path, body, adapter = self._payload(req, card)
        body["stream"] = False
        t0 = time.time()
        try:
            r = self._session.post(f"{self.url}{path}", json=body,
                                   timeout=req.timeout_s)
            r.raise_for_status()
            data = r.json()
        except requests.Timeout:
            return GenResult(text="", ok=False, model=card.name, backend=self.name,
                             finish_reason="timeout",
                             error=f"generation exceeded {req.timeout_s:.0f}s deadline")
        except Exception as exc:
            return GenResult(text="", ok=False, model=card.name, backend=self.name,
                             finish_reason="error", error=str(exc)[:400])

        raw_text = (data.get("response")
                    if path.endswith("generate")
                    else (data.get("message") or {}).get("content", "")) or ""
        return self._finish(raw_text, data, req, card, adapter, t0)

    def stream(self, req: GenRequest, card: Any) -> Iterator[dict[str, Any]]:
        path, body, adapter = self._payload(req, card)
        body["stream"] = True
        t0 = time.time()
        ttft = 0.0
        buf: list[str] = []
        last: dict[str, Any] = {}
        try:
            with self._session.post(f"{self.url}{path}", json=body, stream=True,
                                    timeout=req.timeout_s) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line:
                        continue
                    chunk = json.loads(line)
                    piece = (chunk.get("response")
                             if path.endswith("generate")
                             else (chunk.get("message") or {}).get("content", "")) or ""
                    if piece:
                        if not ttft:
                            ttft = time.time() - t0
                        buf.append(piece)
                        yield {"delta": piece, "done": False}
                    if chunk.get("done"):
                        last = chunk
                        break
        except Exception as exc:
            yield {"done": True, "result": GenResult(
                text="".join(buf), ok=False, model=card.name, backend=self.name,
                error=str(exc)[:400], finish_reason="error")}
            return
        res = self._finish("".join(buf), last, req, card, adapter, t0)
        res.ttft_s = round(ttft, 3)
        yield {"done": True, "result": res}

    def _finish(self, raw_text: str, data: dict[str, Any], req: GenRequest,
                card: Any, adapter: Any, t0: float) -> GenResult:
        parsed = adapter.parse(raw_text)
        # Models without native harmony tool-calling use the JSON fallback protocol.
        if req.tools and not parsed["tool_calls"] and card.prompt_adapter != "harmony":
            parsed = parse_json_tool_reply(parsed["final"] or raw_text) | {
                "reasoning": parsed.get("reasoning", "")}
        elif req.tools and not parsed["tool_calls"] and parsed["final"]:
            # A harmony model may still answer in the JSON protocol if asked to.
            alt = parse_json_tool_reply(parsed["final"])
            if alt["tool_calls"]:
                parsed = alt | {"reasoning": parsed.get("reasoning", "")}

        total = time.time() - t0
        eval_count = data.get("eval_count", 0) or 0
        eval_ns = data.get("eval_duration", 0) or 0
        tps = (eval_count / (eval_ns / 1e9)) if eval_ns else (
            eval_count / total if total else 0.0)
        return GenResult(
            text=parsed["final"], reasoning=parsed.get("reasoning", ""),
            tool_calls=parsed["tool_calls"], model=card.name, backend=self.name,
            prompt_tokens=data.get("prompt_eval_count", 0) or 0,
            completion_tokens=eval_count,
            ttft_s=round((data.get("prompt_eval_duration", 0) or 0) / 1e9, 3),
            total_s=round(total, 3), decode_tps=round(tps, 2),
            finish_reason=data.get("done_reason", "stop") or "stop",
            ok=True, raw={"raw_text": raw_text[:4000]},
        )
