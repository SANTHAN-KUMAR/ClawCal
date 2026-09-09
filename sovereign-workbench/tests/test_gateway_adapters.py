"""Prompt adapters, and the harmony bug that made gpt-oss look broken."""
from __future__ import annotations

import pytest

from sovereign.gateway.adapters import (HarmonyAdapter, QwenAdapter,
                                        get_adapter, parse_json_tool_reply)
from sovereign.gateway.base import ChatMessage, GenRequest, ToolSpec

TOOL = ToolSpec("calculator", "Evaluate an arithmetic expression",
                {"type": "object",
                 "properties": {"expression": {"type": "string",
                                               "description": "e.g. 16-12"}},
                 "required": ["expression"]})


def _req(**kw) -> GenRequest:
    base = dict(messages=[ChatMessage("system", "Be terse."),
                          ChatMessage("user", "What is 17*23?")],
                model="gpt-oss-20b")
    base.update(kw)
    return GenRequest(**base)


class TestHarmony:
    def test_prompt_does_not_force_the_final_channel(self):
        """The regression that cost 36x latency and produced empty output.

        Ending the prompt at `<|channel|>final<|message|>` forecloses the
        analysis channel the model is trained to open first; it then stalls.
        """
        rendered = HarmonyAdapter().render(_req())
        assert rendered.raw_prompt.endswith("<|start|>assistant")
        assert not rendered.raw_prompt.endswith("<|message|>")
        assert "<|channel|>final" not in rendered.raw_prompt

    def test_stops_on_return_not_end(self):
        # <|end|> terminates the *analysis* message; stopping there truncates the
        # model before it ever produces an answer.
        stops = HarmonyAdapter().render(_req()).stop
        assert "<|return|>" in stops and "<|call|>" in stops
        assert "<|end|>" not in stops

    def test_instructions_go_to_the_developer_message(self):
        p = HarmonyAdapter().render(_req()).raw_prompt
        assert "<|start|>developer<|message|>" in p
        head = p[:p.index("<|start|>developer")]
        assert "Be terse." not in head, "instructions leaked into the system message"
        assert "Reasoning:" in head and "Valid channels" in head

    def test_reasoning_effort_is_declared(self):
        for level in ("low", "medium", "high"):
            assert f"Reasoning: {level}" in HarmonyAdapter().render(
                _req(reasoning=level)).raw_prompt

    def test_tools_render_as_a_typescript_namespace(self):
        p = HarmonyAdapter().render(_req(tools=[TOOL])).raw_prompt
        assert "namespace functions" in p
        assert "type calculator = (_: {" in p
        assert "expression: string," in p

    def test_parses_analysis_and_final_channels(self):
        out = HarmonyAdapter().parse(
            "<|channel|>analysis<|message|>17*23 = 391<|end|>"
            "<|start|>assistant<|channel|>final<|message|>391<|return|>")
        assert out["final"] == "391"
        assert "391" in out["reasoning"]
        assert out["tool_calls"] == []

    def test_parses_a_commentary_tool_call(self):
        out = HarmonyAdapter().parse(
            "<|channel|>analysis<|message|>need the calculator<|end|>"
            "<|start|>assistant<|channel|>commentary to=functions.calculator "
            '<|constrain|>json<|message|>{"expression":"17*23"}<|call|>')
        assert out["tool_calls"] == [
            {"name": "calculator", "arguments": {"expression": "17*23"}}]
        assert out["final"] == ""

    def test_bare_text_without_channels_is_accepted(self):
        assert HarmonyAdapter().parse("just an answer")["final"] == "just an answer"

    def test_reasoning_without_a_conclusion_is_surfaced(self):
        # Better to show the reasoning than to return nothing at all.
        out = HarmonyAdapter().parse(
            "<|channel|>analysis<|message|>I think the answer is 391<|end|>")
        assert "391" in out["final"]


class TestQwen:
    def test_think_block_is_routed_to_reasoning(self):
        out = QwenAdapter().parse("<think>reasoning here</think>The answer is 4.")
        assert out["final"] == "The answer is 4."
        assert out["reasoning"] == "reasoning here"

    def test_low_effort_appends_the_no_think_switch(self):
        r = QwenAdapter().render(_req(reasoning="low", model="qwen3-8b"))
        assert r.messages[-1]["content"].endswith("/no_think")


class TestJsonFallback:
    def test_parses_a_tool_call(self):
        out = parse_json_tool_reply('{"tool":"calculator","arguments":{"expression":"1+1"}}')
        assert out["tool_calls"][0]["name"] == "calculator"

    def test_parses_a_final_answer(self):
        assert parse_json_tool_reply('{"final":"done"}')["final"] == "done"

    def test_recovers_from_a_code_fence(self):
        out = parse_json_tool_reply('```json\n{"final":"fenced"}\n```')
        assert out["final"] == "fenced"

    def test_recovers_an_embedded_object(self):
        out = parse_json_tool_reply('Sure! {"tool":"calculator","arguments":{}} ok')
        assert out["tool_calls"][0]["name"] == "calculator"

    def test_plain_prose_passes_through(self):
        assert parse_json_tool_reply("no json here")["final"] == "no json here"


def test_adapter_lookup_falls_back_to_chat():
    assert get_adapter("nonexistent").key == "chat"
    assert get_adapter(None).key == "chat"
