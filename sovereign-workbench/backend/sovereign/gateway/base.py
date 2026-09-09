"""Backend-neutral contract for local inference.

Nothing above this module knows whether generation is served by Ollama, vLLM,
llama.cpp or anything else. Swapping a backend means adding a class here and a row
in the model registry -- no product code changes. (Acceptance criterion A15.)
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Iterator


@dataclass
class ChatMessage:
    role: str                      # system | developer | user | assistant | tool
    content: str
    name: str | None = None        # tool name, when role == "tool"
    tool_calls: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name:
            d["name"] = self.name
        return d


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class GenRequest:
    messages: list[ChatMessage]
    model: str
    tools: list[ToolSpec] = field(default_factory=list)
    temperature: float = 0.3
    top_p: float = 0.9
    max_tokens: int = 1400
    ctx_tokens: int = 8192
    stop: list[str] = field(default_factory=list)
    reasoning: str = "medium"      # low | medium | high  (harmony models)
    json_mode: bool = False
    timeout_s: float = 900.0
    task_id: str | None = None


@dataclass
class GenResult:
    text: str                      # the user-visible final answer
    reasoning: str = ""            # hidden chain-of-thought channel, if the model emits one
    tool_calls: list[dict] = field(default_factory=list)
    model: str = ""
    backend: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    ttft_s: float = 0.0
    total_s: float = 0.0
    decode_tps: float = 0.0
    finish_reason: str = "stop"
    ok: bool = True
    error: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return self.ok and not (self.text or "").strip() and not self.tool_calls


class ModelBackend(abc.ABC):
    """One local inference server."""

    name: str = "backend"

    @abc.abstractmethod
    def health(self) -> dict[str, Any]:
        ...

    @abc.abstractmethod
    def list_models(self) -> list[dict[str, Any]]:
        ...

    @abc.abstractmethod
    def generate(self, req: GenRequest, card: Any) -> GenResult:
        ...

    def stream(self, req: GenRequest, card: Any) -> Iterator[dict[str, Any]]:
        """Default: no true streaming, emit one chunk. Backends override."""
        res = self.generate(req, card)
        yield {"delta": res.text, "done": False}
        yield {"done": True, "result": res}

    # -- residency control ------------------------------------------------
    # The scheduler needs an actuator to pin or evict weights. Backends that
    # cannot do this report False and the scheduler falls back to modelling
    # residency rather than commanding it.
    supports_residency_control: bool = False

    def pin(self, card: Any, seconds: int = 1800) -> bool:
        return False

    def evict(self, card: Any) -> bool:
        return False

    def resident_models(self) -> list[dict[str, Any]]:
        return []
