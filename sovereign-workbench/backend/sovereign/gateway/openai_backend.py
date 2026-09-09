"""OpenAI-compatible backend.

Covers vLLM, llama.cpp's server, LM Studio, FreeToken and anything else that
speaks /v1/chat/completions on localhost. Registering one of these is the proof
of backend independence (A15): no product code changes, only a registry row and
an environment variable.
"""
from __future__ import annotations

import time
from typing import Any

import requests

from ..config import settings
from .adapters import get_adapter, parse_json_tool_reply
from .base import GenRequest, GenResult, ModelBackend


class OpenAICompatBackend(ModelBackend):
    name = "openai_compat"

    def __init__(self, url: str | None = None, label: str = "openai_compat") -> None:
        self.url = (url or settings.backends.openai_compat_url).rstrip("/")
        self.name = label
        self._session = requests.Session()

    def health(self) -> dict[str, Any]:
        if not self.url:
            return {"status": "unconfigured"}
        try:
            r = self._session.get(f"{self.url}/v1/models", timeout=3)
            return {"status": "up" if r.ok else "degraded", "code": r.status_code,
                    "url": self.url}
        except Exception as exc:
            return {"status": "down", "error": str(exc)[:200], "url": self.url}

    def list_models(self) -> list[dict[str, Any]]:
        if not self.url:
            return []
        try:
            return self._session.get(f"{self.url}/v1/models",
                                     timeout=6).json().get("data", [])
        except Exception:
            return []

    def generate(self, req: GenRequest, card: Any) -> GenResult:
        adapter = get_adapter(card.prompt_adapter)
        rp = adapter.render(req)
        t0 = time.time()

        if rp.raw_prompt is not None:
            path, body = "/v1/completions", {
                "model": card.backend_ref, "prompt": rp.raw_prompt,
                "max_tokens": req.max_tokens, "temperature": req.temperature,
                "top_p": req.top_p, "stop": rp.stop or None,
            }
        else:
            path, body = "/v1/chat/completions", {
                "model": card.backend_ref, "messages": rp.messages,
                "max_tokens": req.max_tokens, "temperature": req.temperature,
                "top_p": req.top_p, "stop": rp.stop or None,
            }
            if req.json_mode:
                body["response_format"] = {"type": "json_object"}

        try:
            r = self._session.post(f"{self.url}{path}", json=body, timeout=req.timeout_s)
            r.raise_for_status()
            data = r.json()
        except requests.Timeout:
            return GenResult(text="", ok=False, model=card.name, backend=self.name,
                             finish_reason="timeout",
                             error=f"generation exceeded {req.timeout_s:.0f}s deadline")
        except Exception as exc:
            return GenResult(text="", ok=False, model=card.name, backend=self.name,
                             error=str(exc)[:400], finish_reason="error")

        choice = (data.get("choices") or [{}])[0]
        raw_text = choice.get("text") or (choice.get("message") or {}).get("content", "") or ""
        parsed = adapter.parse(raw_text)
        if req.tools and not parsed["tool_calls"]:
            parsed = parse_json_tool_reply(parsed["final"] or raw_text) | {
                "reasoning": parsed.get("reasoning", "")}
        usage = data.get("usage", {}) or {}
        total = time.time() - t0
        ct = usage.get("completion_tokens", 0) or 0
        return GenResult(
            text=parsed["final"], reasoning=parsed.get("reasoning", ""),
            tool_calls=parsed["tool_calls"], model=card.name, backend=self.name,
            prompt_tokens=usage.get("prompt_tokens", 0) or 0, completion_tokens=ct,
            total_s=round(total, 3), decode_tps=round(ct / total, 2) if total else 0.0,
            finish_reason=choice.get("finish_reason", "stop") or "stop", ok=True,
            raw={"raw_text": raw_text[:4000]},
        )
