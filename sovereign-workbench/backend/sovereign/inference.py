"""The node's OpenAI-compatible inference surface for client harnesses (§6.3).

"The OpenAI-compatible API as the seam": a client's harness (our opencode
build) points its provider at `https://<node>/v1` and never learns which engine
serves it. Behind this surface is the same gateway the node's own harness uses
— the same prompt adapters (harmony for gpt-oss, the JSON tool protocol for
the rest), memory pricing before every load, circuit breakers and deadlines.

What is different from a bare engine endpoint:

* **identity** — every call names a principal and, when it comes from a member
  device, the device and its lease;
* **the pin** — the model alias `generalist` resolves to the model B1 pinned for
  the caller's plan at /api/admit, so a client's expert cache and the node's
  prefix cache stay warm for the whole plan;
* **waits, not evicts** — a call takes one of the node's external slots, sized
  by the same memory-derived limit the scheduler uses, and waits for one rather
  than displacing a running task's model;
* **the record** — every call is an audit row with its token counts.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Iterator

from . import audit, placement
from .config import settings
from .gateway import gateway
from .gateway.base import ChatMessage, GenRequest, GenResult, ToolSpec
from .gateway.registry import registry

ALIASES = ("generalist", "node/generalist", "clawcal/generalist")
SLOT_WAIT_S = 120.0


class InferenceError(Exception):
    def __init__(self, status: int, message: str, code: str = "invalid_request_error"):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code

    def body(self) -> dict[str, Any]:
        return {"error": {"message": self.message, "type": self.code,
                          "code": self.status}}


def models_for(principal: Any) -> dict[str, Any]:
    now = int(time.time())
    data = [{"id": "generalist", "object": "model", "created": now,
             "owned_by": "trust-domain node",
             "description": "the model B1 pinned for your plan"}]
    for c in registry.all():
        if c.modality == "vision":
            continue
        data.append({"id": c.name, "object": "model", "created": now,
                     "owned_by": "trust-domain node",
                     "context_window": c.ctx_max})
    return {"object": "list", "data": data}


def resolve_model(requested: str, session_id: str | None, principal: str,
                  device_id: str | None) -> tuple[str, str]:
    """(registry model, why). The alias follows the plan's pin."""
    req = (requested or "generalist").strip()
    if req in ALIASES or req.endswith("/generalist"):
        if session_id:
            t = placement.attached_task(session_id, principal, device_id)
            if t and t.get("selected_model"):
                return t["selected_model"], f"pinned at admission for {t['id']}"
        from . import router
        from .runtime.residency import residency
        cls = router.classify("general conversation")
        dec = router.select_model(cls, resident=residency.resident_names(),
                                  budget_for=residency.context_budget_tokens)
        if not dec.model:
            raise InferenceError(503, f"no model available: {dec.reason}",
                                 "service_unavailable")
        return dec.model, "no plan pinned; router's choice for general work"
    name = req.split("/", 1)[-1]
    card = registry.get(name)
    if not card or not card.enabled:
        raise InferenceError(404, f"model {req!r} is not served by this node",
                             "model_not_found")
    return card.name, "requested by name"


def _text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict) and p.get("type") in ("text", "input_text"):
                parts.append(str(p.get("text", "")))
            elif isinstance(p, dict) and p.get("type") in ("image_url", "input_image"):
                raise InferenceError(
                    400, "images are not accepted on /v1: upload the document or "
                         "photograph to the node, where OCR and the vision model "
                         "read it and mint span ids (extract/vision run on the "
                         "node)")
        return "\n".join(parts)
    return str(content)


def to_messages(raw: list[dict[str, Any]]) -> list[ChatMessage]:
    """OpenAI chat messages -> the gateway's messages."""
    out: list[ChatMessage] = []
    call_names: dict[str, str] = {}
    for m in raw:
        role = m.get("role")
        if role in ("system", "developer"):
            out.append(ChatMessage("system", _text(m.get("content"))))
        elif role == "user":
            out.append(ChatMessage("user", _text(m.get("content"))))
        elif role == "assistant":
            calls = []
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {"_raw": fn.get("arguments")}
                calls.append({"name": fn.get("name", ""), "arguments": args})
                call_names[tc.get("id", "")] = fn.get("name", "")
            text = _text(m.get("content"))
            if calls and not text.strip():
                # Rendered as the JSON tool protocol the node's adapters teach
                # the model, so the transcript it sees is one it could produce.
                text = "\n".join(json.dumps({"tool": c["name"],
                                             "arguments": c["arguments"]})
                                 for c in calls)
            out.append(ChatMessage("assistant", text, tool_calls=calls))
        elif role == "tool":
            name = call_names.get(m.get("tool_call_id", ""), m.get("name") or "tool")
            out.append(ChatMessage("tool", _text(m.get("content")), name=name))
    if not any(x.role == "user" for x in out):
        raise InferenceError(400, "messages must include a user message")
    return out


def to_tools(raw: list[dict[str, Any]] | None) -> list[ToolSpec]:
    out = []
    for t in raw or []:
        fn = t.get("function") if t.get("type", "function") == "function" else None
        if fn and fn.get("name"):
            out.append(ToolSpec(fn["name"], fn.get("description", ""),
                                fn.get("parameters") or {"type": "object",
                                                         "properties": {}}))
    return out


def _tool_calls(res: GenResult) -> list[dict[str, Any]]:
    return [{"id": f"call_{uuid.uuid4().hex[:20]}", "type": "function",
             "function": {"name": c.get("name", ""),
                          "arguments": json.dumps(c.get("arguments") or {})}}
            for c in res.tool_calls]


def complete(body: dict[str, Any], *, principal: Any, device: Any,
             session_id: str | None) -> tuple[GenResult, str, dict[str, Any]]:
    """Run one chat completion through the gateway. Returns (result, model, meta)."""
    device_id = device.device_id if device else None
    model, why = resolve_model(str(body.get("model") or ""), session_id,
                               principal.name, device_id)
    card = registry.get(model)
    msgs = to_messages(body.get("messages") or [])
    max_tokens = int(body.get("max_completion_tokens") or body.get("max_tokens")
                     or 2048)
    from .runtime.residency import residency
    req = GenRequest(messages=msgs, model=model, tools=to_tools(body.get("tools")),
                     temperature=float(body.get("temperature", 0.3)),
                     top_p=float(body.get("top_p", 0.9)),
                     max_tokens=max(16, min(max_tokens, 8192)),
                     ctx_tokens=min(card.ctx_max, residency.context_budget_tokens(card)),
                     stop=list(body.get("stop") or []) if isinstance(
                         body.get("stop"), list) else
                     ([body["stop"]] if body.get("stop") else []),
                     timeout_s=float(settings.limits.generation_timeout_s))
    if session_id:
        placement.touch((placement.attached_task(session_id, principal.name,
                                                 device_id) or {}).get("id"))
    if not placement.external_slots.acquire(SLOT_WAIT_S, principal.name):
        raise InferenceError(429, f"the node is saturated: every slot has been busy "
                                  f"for {SLOT_WAIT_S:.0f} s. The call waited rather "
                                  f"than evict a running task's model; retry",
                             "rate_limit_exceeded")
    t0 = time.time()
    try:
        # The pin holds: no silent fallback to another model mid-plan.
        res = gateway.generate(req, allow_fallback=False)
    finally:
        placement.external_slots.release(principal.name)
    meta = {"model": model, "why": why, "device": device_id, "session": session_id,
            "prompt_tokens": res.prompt_tokens,
            "completion_tokens": res.completion_tokens,
            "tool_calls": len(res.tool_calls), "wall_s": round(time.time() - t0, 2),
            "ok": res.ok}
    audit.record("inference", "chat_completion", actor=principal.name,
                 outcome="OK" if res.ok else "FAILED",
                 task_id=(placement.attached_task(session_id, principal.name,
                                                  device_id) or {}).get("id")
                 if session_id else None, detail=meta)
    if not res.ok:
        code = 507 if "memory" in (res.error or "") else 503
        raise InferenceError(code, f"{model}: {res.error}", "service_unavailable")
    return res, model, meta


def response_body(res: GenResult, alias: str) -> dict[str, Any]:
    calls = _tool_calls(res)
    msg: dict[str, Any] = {"role": "assistant", "content": res.text or None}
    if calls:
        msg["tool_calls"] = calls
    return {"id": f"chatcmpl-{uuid.uuid4().hex[:24]}", "object": "chat.completion",
            "created": int(time.time()), "model": alias,
            "choices": [{"index": 0, "message": msg,
                         "finish_reason": "tool_calls" if calls else
                         ("length" if res.finish_reason == "length" else "stop")}],
            "usage": {"prompt_tokens": res.prompt_tokens,
                      "completion_tokens": res.completion_tokens,
                      "total_tokens": res.prompt_tokens + res.completion_tokens}}


def stream_chunks(res: GenResult, alias: str, include_usage: bool) -> Iterator[bytes]:
    """The completion as Server-Sent Events, in OpenAI's chunk shape.

    The gateway's adapters parse a whole completion (harmony channels, the JSON
    tool protocol), so the answer is produced in full and then streamed; the
    client sees the same event sequence a streaming engine would send.
    """
    cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())

    def ev(delta: dict[str, Any], finish: str | None = None,
           usage: dict[str, Any] | None = None) -> bytes:
        chunk: dict[str, Any] = {"id": cid, "object": "chat.completion.chunk",
                                 "created": created, "model": alias,
                                 "choices": [{"index": 0, "delta": delta,
                                              "finish_reason": finish}]}
        if usage is not None:
            chunk["choices"] = []
            chunk["usage"] = usage
        return f"data: {json.dumps(chunk)}\n\n".encode()

    yield ev({"role": "assistant", "content": ""})
    text = res.text or ""
    for i in range(0, len(text), 400):
        yield ev({"content": text[i:i + 400]})
    calls = _tool_calls(res)
    for i, c in enumerate(calls):
        yield ev({"tool_calls": [{"index": i, "id": c["id"], "type": "function",
                                  "function": c["function"]}]})
    yield ev({}, "tool_calls" if calls else "stop")
    if include_usage:
        yield ev({}, None, {"prompt_tokens": res.prompt_tokens,
                            "completion_tokens": res.completion_tokens,
                            "total_tokens": res.prompt_tokens + res.completion_tokens})
    yield b"data: [DONE]\n\n"
