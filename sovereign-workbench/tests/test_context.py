"""Context budgeting: compaction, trimming, and conversation threading.

A trim that overshoots the budget it was trimming to is worse than no trim at
all — it costs the tokens of a summary and still overflows — so the invariant
under test is that whatever `_trim` returns actually fits.
"""
from __future__ import annotations

import pytest

from sovereign import db
from sovereign.agent.harness import (CHARS_PER_TOKEN, SUMMARY_HEADER,
                                     SUMMARY_MAX_CHARS, AgentHarness)


class RecordingCtl:
    """The slice of AgentControl that trimming actually touches."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, str]] = []
        self.step = 0

    def emit(self, kind: str, label: str, detail: str = "") -> None:
        self.events.append((kind, label, detail))


def harness(budget: int) -> tuple[AgentHarness, RecordingCtl]:
    ctl = RecordingCtl()
    h = AgentHarness({"id": db.new_id("ctx"), "selected_model": "qwen3-8b",
                      "context_budget": budget, "task_type": "general",
                      "attachments": "[]"}, ctl)
    h.context_budget = budget
    return h, ctl


def trajectory(steps: int, obs_chars: int = 900) -> list[dict[str, str]]:
    msgs = [{"role": "system", "content": "SYS " * 50},
            {"role": "user", "content": "Original task: check wall thickness."}]
    for i in range(steps):
        msgs.append({"role": "assistant", "content": f"step {i}: calling a tool."})
        msgs.append({"role": "tool", "name": f"tool_{i}",
                     "content": f"observation {i}: " + "x" * obs_chars})
    return msgs


def budget_chars(budget: int) -> int:
    return int(budget * CHARS_PER_TOKEN * 0.72)


class TestTrim:
    @pytest.mark.parametrize("budget,steps,obs", [
        (4096, 14, 900), (4096, 60, 900), (2048, 30, 2000),
        (8192, 200, 400), (4096, 1, 200), (32768, 14, 900),
    ])
    def test_result_fits_the_budget(self, budget, steps, obs):
        h, _ = harness(budget)
        out = h._trim(trajectory(steps, obs))
        assert sum(len(m["content"]) for m in out) <= budget_chars(budget)

    def test_short_trajectory_is_returned_untouched(self):
        h, ctl = harness(32768)
        msgs = trajectory(4)
        assert h._trim(msgs) is msgs
        assert h.last_compaction == 0
        assert ctl.events == []

    def test_system_prompt_and_original_task_always_survive(self):
        h, _ = harness(4096)
        msgs = trajectory(60)
        out = h._trim(msgs)
        assert out[0] is msgs[0] and out[1] is msgs[1]

    def test_surviving_tail_is_contiguous(self):
        """A hole in the middle of the trajectory reads as a false sequence."""
        h, _ = harness(4096)
        msgs = trajectory(40)
        out = h._trim(msgs)
        tail = out[3:]                       # past system, task, summary
        assert msgs[len(msgs) - len(tail):] == tail

    def test_compaction_summarises_rather_than_discards(self):
        h, ctl = harness(4096)
        out = h._trim(trajectory(20))
        summary = next(m for m in out if m["content"].startswith(SUMMARY_HEADER))
        assert "tool_" in summary["content"]           # names what ran
        assert "observation" in summary["content"]     # and what it returned
        assert h.last_compaction > 0
        assert [e[0] for e in ctl.events] == ["compacted"]

    def test_summary_stays_inside_its_reservation(self):
        h, _ = harness(4096)
        out = h._trim(trajectory(200))
        summary = next(m for m in out if m["content"].startswith(SUMMARY_HEADER))
        assert len(summary["content"]) <= SUMMARY_MAX_CHARS
        assert "dropped entirely" in summary["content"]

    def test_used_tokens_tracks_the_trimmed_request(self):
        h, _ = harness(4096)
        msgs = trajectory(60)
        assert h.context_used_tokens(h._trim(msgs)) <= h.context_budget
        assert h.context_used_tokens(msgs) > h.context_budget


class TestThreading:
    def test_history_replays_prior_turns_of_the_same_conversation(self):
        conv = db.new_id("conv")
        for n, (prompt, answer) in enumerate([
                ("What is in the attached drawing?", "A P&ID symbols library."),
                ("How many valves?", "Twenty manual valves.")]):
            tid = db.new_id("task")
            db.insert("tasks", {
                "id": tid, "conversation_id": conv, "title": prompt[:40],
                "prompt": prompt, "state": "COMPLETED", "workflow": "general",
                "owner": "test", "department": "default", "priority": "MEDIUM",
                "created_at": 1000.0 + n,
                "result": db.jdump({"summary": answer})})

        later = db.new_id("task")
        db.insert("tasks", {
            "id": later, "conversation_id": conv, "title": "follow-up",
            "prompt": "And the vessels?", "state": "RUNNING",
            "workflow": "general", "owner": "test", "department": "default",
            "priority": "MEDIUM", "created_at": 1002.0})

        h, _ = harness(8192)
        h.task = db.rows_to_dicts([db.query_one(
            "SELECT * FROM tasks WHERE id=?", (later,))])[0]
        history = h._conversation_history()

        assert history, "a threaded follow-up must see the earlier turns"
        text = " ".join(m["content"] for m in history)
        assert "How many valves?" in text
        assert "Twenty manual valves." in text
        assert "And the vessels?" not in text, "the current turn is not history"
