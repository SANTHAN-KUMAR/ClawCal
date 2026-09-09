"""Per-model prompt adapters.

Most local models take an OpenAI-shaped chat array. gpt-oss does not: it speaks
OpenAI *harmony*, and getting it wrong is the difference between a working model
and one that looks broken.

The failure we measured on this hardware (2026-09-09, gpt-oss-20b MXFP4 via Ollama)
is worth recording, because it is the exact bug that made the earlier prototype
look useless:

    prompt ending in `<|start|>assistant<|channel|>final<|message|>`
        -> 58.1 s wall clock, 2 tokens emitted, frequently empty
    prompt ending in `<|start|>assistant`
        ->  1.6 s wall clock, correct analysis + final channels

Forcing the `final` channel fights the model's trained behaviour: it wants to open
an `analysis` channel first. Denied that, it stalls. The adapter therefore leaves
the channel open and *parses* the channels out of the completion instead.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from .base import ChatMessage, GenRequest, ToolSpec

# ---------------------------------------------------------------- harmony tokens
H_START, H_MSG, H_END = "<|start|>", "<|message|>", "<|end|>"
H_CHANNEL, H_CALL, H_RETURN = "<|channel|>", "<|call|>", "<|return|>"
H_CONSTRAIN = "<|constrain|>"
HARMONY_STOPS = [H_RETURN, H_CALL]


@dataclass
class RenderedPrompt:
    """Either a raw completion string (harmony) or a chat array (everything else)."""
    raw_prompt: str | None = None
    messages: list[dict[str, Any]] | None = None
    stop: list[str] = None          # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.stop is None:
            self.stop = []


class PromptAdapter:
    key = "chat"

    def render(self, req: GenRequest) -> RenderedPrompt:
        msgs = []
        for m in req.messages:
            role = m.role
            if role == "developer":        # collapse: only harmony has a developer role
                role = "system"
            msgs.append({"role": role, "content": m.content,
                         **({"name": m.name} if m.name else {})})
        if req.tools:
            msgs = _inject_json_tool_protocol(msgs, req.tools)
        return RenderedPrompt(messages=msgs, stop=list(req.stop))

    def parse(self, text: str) -> dict[str, Any]:
        return {"final": text.strip(), "reasoning": "", "tool_calls": []}


class QwenAdapter(PromptAdapter):
    """Qwen3 exposes a <think> block when thinking mode is on; strip it into the
    reasoning channel so it never leaks into an approval note."""

    key = "qwen"

    def render(self, req: GenRequest) -> RenderedPrompt:
        rp = super().render(req)
        # Qwen3 honours /no_think as a soft switch on the last user turn.
        if req.reasoning == "low" and rp.messages:
            for m in reversed(rp.messages):
                if m["role"] == "user":
                    m["content"] = m["content"] + " /no_think"
                    break
        return rp

    def parse(self, text: str) -> dict[str, Any]:
        reasoning = ""
        m = re.search(r"<think>(.*?)</think>", text, re.S)
        if m:
            reasoning = m.group(1).strip()
            text = text[m.end():]
        text = re.sub(r"</?think>", "", text)
        return {"final": text.strip(), "reasoning": reasoning, "tool_calls": []}


class HarmonyAdapter(PromptAdapter):
    """OpenAI harmony response format, as used by gpt-oss-20b / gpt-oss-120b."""

    key = "harmony"

    def render(self, req: GenRequest) -> RenderedPrompt:
        system_parts: list[str] = []
        developer_parts: list[str] = []
        convo: list[str] = []

        for m in req.messages:
            if m.role == "system":
                developer_parts.append(m.content)
            elif m.role == "developer":
                developer_parts.append(m.content)
            elif m.role == "user":
                convo.append(f"{H_START}user{H_MSG}{m.content}{H_END}")
            elif m.role == "assistant":
                convo.append(f"{H_START}assistant{H_CHANNEL}final{H_MSG}{m.content}{H_END}")
            elif m.role == "tool":
                # Tool output returns on the commentary channel, addressed back to the assistant.
                convo.append(
                    f"{H_START}functions.{m.name} to=assistant{H_CHANNEL}commentary"
                    f"{H_MSG}{m.content}{H_END}"
                )

        # The harmony `system` message carries identity and runtime metadata only.
        system_parts.append(
            "You are a sovereign on-premise industrial assistant.\n"
            f"Knowledge cutoff: 2024-06\nCurrent date: {date.today().isoformat()}\n\n"
            f"Reasoning: {req.reasoning}\n\n"
            "# Valid channels: analysis, commentary, final. "
            "Channel must be included for every message."
        )
        if req.tools:
            system_parts.append(
                "Calls to these tools must go to the commentary channel: 'functions'."
            )

        head = f"{H_START}system{H_MSG}" + "\n".join(system_parts) + H_END

        dev = ""
        if developer_parts or req.tools:
            body = "# Instructions\n\n" + "\n\n".join(developer_parts)
            if req.tools:
                body += "\n\n# Tools\n\n## functions\n\nnamespace functions {\n\n"
                for t in req.tools:
                    body += _harmony_tool_decl(t) + "\n\n"
                body += "} // namespace functions"
            dev = f"{H_START}developer{H_MSG}{body}{H_END}"

        # Critical: end at `<|start|>assistant` with NO channel. See module docstring.
        prompt = "\n".join(x for x in [head, dev, *convo] if x) + f"\n{H_START}assistant"
        return RenderedPrompt(raw_prompt=prompt, stop=list(HARMONY_STOPS) + list(req.stop))

    def parse(self, text: str) -> dict[str, Any]:
        """Split a harmony completion into analysis / commentary / final channels.

        The completion begins immediately after `<|start|>assistant`, so the first
        channel marker belongs to the first assistant message.
        """
        analysis: list[str] = []
        finals: list[str] = []
        tool_calls: list[dict[str, Any]] = []

        # Normalise: treat the implicit leading assistant header uniformly.
        body = text
        if not body.lstrip().startswith(H_CHANNEL) and H_CHANNEL not in body:
            # Model emitted bare text with no channel markers at all -- accept it.
            return {"final": body.strip(), "reasoning": "", "tool_calls": []}

        # Each segment starts at a `<|channel|>` marker.
        for seg in re.split(r"(?=<\|channel\|>)", body):
            seg = seg.strip()
            if not seg.startswith(H_CHANNEL):
                # Text before any channel marker: stray preamble, keep as final.
                if seg and not seg.startswith(H_START):
                    finals.append(_strip_tokens(seg))
                continue
            header, _, content = seg[len(H_CHANNEL):].partition(H_MSG)
            header = header.strip()
            content = _strip_tokens(content)
            chan = header.split()[0] if header else "final"

            if chan.startswith("analysis"):
                analysis.append(content)
            elif chan.startswith("commentary"):
                call = _parse_harmony_call(header, content)
                if call:
                    tool_calls.append(call)
                else:
                    analysis.append(content)
            else:
                finals.append(content)

        final_text = "\n".join(f for f in finals if f).strip()
        # A model that only reasoned and then called a tool has no final text; that
        # is legitimate, so do not synthesise one.
        if not final_text and not tool_calls and analysis:
            # Reasoned but never concluded -- surface the reasoning rather than nothing.
            final_text = analysis[-1].strip()
        return {"final": final_text, "reasoning": "\n".join(analysis).strip(),
                "tool_calls": tool_calls}


def _strip_tokens(s: str) -> str:
    for t in (H_END, H_RETURN, H_CALL, H_START + "assistant", H_START):
        s = s.replace(t, "")
    return s.strip()


def _parse_harmony_call(header: str, content: str) -> dict[str, Any] | None:
    """`commentary to=functions.calculator <|constrain|>json` + JSON body."""
    m = re.search(r"to=functions\.([A-Za-z0-9_\-]+)", header)
    if not m:
        return None
    name = m.group(1)
    args = _loads_lenient(content)
    return {"name": name, "arguments": args if isinstance(args, dict) else {"_raw": content}}


def _harmony_tool_decl(t: ToolSpec) -> str:
    desc = t.description.strip().replace("\n", "\n// ")
    props = t.parameters.get("properties", {})
    required = set(t.parameters.get("required", []))
    if not props:
        return f"// {desc}\ntype {t.name} = () => any;"
    lines = [f"// {desc}", f"type {t.name} = (_: {{"]
    for pname, spec in props.items():
        pdesc = spec.get("description", "")
        ptype = _ts_type(spec)
        opt = "" if pname in required else "?"
        if pdesc:
            lines.append(f"// {pdesc}")
        lines.append(f"{pname}{opt}: {ptype},")
    lines.append("}) => any;")
    return "\n".join(lines)


def _ts_type(spec: dict[str, Any]) -> str:
    t = spec.get("type", "string")
    if "enum" in spec:
        return " | ".join(json.dumps(e) for e in spec["enum"])
    if t == "array":
        return f"{_ts_type(spec.get('items', {'type': 'string'}))}[]"
    return {"integer": "number", "number": "number", "boolean": "boolean",
            "object": "object"}.get(t, "string")


# ---------------------------------------------------------------- JSON fallback protocol
_JSON_TOOL_PREAMBLE = """
# Tool protocol

You may call local tools. To call one, reply with ONLY a JSON object, no prose,
no code fence:

{"tool": "<name>", "arguments": {...}}

To give your final answer instead, reply with ONLY:

{"final": "<your answer>"}

Exactly one JSON object per reply. Available tools:
"""


def _inject_json_tool_protocol(msgs: list[dict[str, Any]],
                               tools: list[ToolSpec]) -> list[dict[str, Any]]:
    """For backends/models without native tool calling, describe a strict JSON
    protocol in the system prompt. Deterministic to parse, works on every model."""
    decl = _JSON_TOOL_PREAMBLE + "\n".join(
        f"- {t.name}: {t.description}\n  arguments schema: {json.dumps(t.parameters)}"
        for t in tools
    )
    out = list(msgs)
    for m in out:
        if m["role"] == "system":
            m["content"] = m["content"] + "\n\n" + decl
            return out
    out.insert(0, {"role": "system", "content": decl})
    return out


def _loads_lenient(s: str) -> Any:
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"\n?```$", "", s).strip()
    try:
        return json.loads(s)
    except ValueError:
        pass
    # Recover the first balanced JSON object.
    start = s.find("{")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(s[start:i + 1])
                except ValueError:
                    return None
    return None


def parse_json_tool_reply(text: str) -> dict[str, Any]:
    """Parse the JSON fallback protocol out of a plain completion."""
    obj = _loads_lenient(text)
    if isinstance(obj, dict):
        if "tool" in obj:
            return {"final": "", "reasoning": "",
                    "tool_calls": [{"name": obj["tool"],
                                    "arguments": obj.get("arguments", {}) or {}}]}
        if "final" in obj:
            return {"final": str(obj["final"]), "reasoning": "", "tool_calls": []}
    return {"final": text.strip(), "reasoning": "", "tool_calls": []}


ADAPTERS: dict[str, PromptAdapter] = {
    "chat": PromptAdapter(),
    "qwen": QwenAdapter(),
    "harmony": HarmonyAdapter(),
}


def get_adapter(key: str | None) -> PromptAdapter:
    return ADAPTERS.get(key or "chat", ADAPTERS["chat"])
