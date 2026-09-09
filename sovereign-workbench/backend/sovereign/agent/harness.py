"""Governed agent harness.

A deliberately small reason/act loop. The locked architecture says not to build a
general-purpose agent framework, and this is not one: it has no plugin system, no
multi-agent orchestration and no memory subsystem. What it does have is the four
properties an off-the-shelf harness could not give the control plane:

* **admission-gated** -- it only ever runs inside a slot the scheduler granted;
* **checkpointable at workflow boundaries** -- after every observation it
  persists resumable state and yields if preempted, which is how a low-priority
  batch job gets out of the way of a safety question without being killed;
* **tool-policy-gated per call** -- every tool invocation goes through the policy
  engine, which can require human approval before the call happens;
* **context-budgeted** -- history is trimmed to the KV budget the runtime
  measured, so a long trajectory degrades gracefully instead of stalling.

Everything else -- the tools, the models, the evidence rules -- lives outside.
"""
from __future__ import annotations

import time
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import db
from ..config import WORKSPACE_DIR, settings
from ..gateway import gateway
from ..gateway.base import ChatMessage, GenRequest
from ..gateway.registry import registry
from ..runtime.control import TaskControl
from ..tools import register_all
from ..tools.base import ToolContext
from .prompts import system_prompt

CHARS_PER_TOKEN = 3.6          # conservative for English + tables
OBSERVATION_LIMIT = 6000


@dataclass
class AgentState:
    """Everything needed to resume a trajectory after a pause."""
    messages: list[dict[str, Any]] = field(default_factory=list)
    step: int = 0
    tool_calls: int = 0
    scratch: dict[str, Any] = field(default_factory=dict)
    final: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"messages": self.messages, "step": self.step,
                "tool_calls": self.tool_calls, "scratch": self.scratch,
                "final": self.final}

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "AgentState":
        d = d or {}
        return cls(messages=d.get("messages", []), step=d.get("step", 0),
                   tool_calls=d.get("tool_calls", 0),
                   scratch=d.get("scratch", {}), final=d.get("final", ""))


@dataclass
class AgentResult:
    final: str
    steps: int
    tool_calls: int
    model: str
    stopped_because: str
    scratch: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        # `scratch` carries the retrieved passages, with their document regions,
        # which the provenance check needs. Without it the check falls back to
        # the evidence table and loses the region pointers.
        return {"summary": self.final, "steps": self.steps,
                "tool_calls": self.tool_calls, "model": self.model,
                "stopped_because": self.stopped_because,
                "scratch": {"passages": self.scratch.get("passages", [])}}


class AgentHarness:
    def __init__(self, task: dict[str, Any], ctl: TaskControl,
                 *, allowed_tools: list[str] | None = None,
                 max_steps: int | None = None,
                 extra_instructions: str = "") -> None:
        self.task = task
        self.ctl = ctl
        self.tools = register_all()
        self.allowed = set(allowed_tools or [t.name for t in self.tools.all()])
        self.max_steps = max_steps or settings.limits.max_agent_steps
        self.model_name = task.get("selected_model") or ""
        self.card = registry.get(self.model_name)
        self.context_budget = int(task.get("context_budget")
                                  or (self.card.ctx_max if self.card else 8192))
        self.extra_instructions = extra_instructions
        self.workspace = WORKSPACE_DIR / task["id"]
        self.workspace.mkdir(parents=True, exist_ok=True)

    # -- context management ----------------------------------------------
    def _trim(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Keep the system prompt, the original task, and the most recent turns.

        Middle-of-trajectory tool observations are the first thing dropped: they
        have already been folded into the reasoning that followed them, and they
        are by far the largest consumers of context.
        """
        budget_chars = int(self.context_budget * CHARS_PER_TOKEN * 0.72)
        if sum(len(m["content"]) for m in messages) <= budget_chars:
            return messages

        head = messages[:2]                       # system + original task
        tail: list[dict[str, Any]] = []
        used = sum(len(m["content"]) for m in head)
        dropped = 0
        for m in reversed(messages[2:]):
            if used + len(m["content"]) > budget_chars:
                dropped += 1
                continue
            tail.insert(0, m)
            used += len(m["content"])
        if dropped:
            tail.insert(0, {"role": "user", "content":
                            f"[{dropped} earlier tool observation(s) were removed "
                            f"to stay inside this model's context budget of "
                            f"{self.context_budget} tokens. Their conclusions are "
                            f"reflected in the reasoning that follows them. If you "
                            f"need one again, call the tool again.]"})
        return head + tail

    # -- main loop --------------------------------------------------------
    def run(self, resume: dict[str, Any] | None = None) -> AgentResult:
        state = AgentState.from_dict(resume)
        tool_ctx = ToolContext(task_id=self.task["id"], workspace=self.workspace,
                               emit=self.ctl.emit, scratch=state.scratch)
        tool_ctx.scratch.setdefault(
            "attachments", db.jload(self.task.get("attachments"), []) or [])

        if not state.messages:
            state.messages = [
                {"role": "system",
                 "content": system_prompt(self.task.get("task_type") or "general",
                                          self.extra_instructions)},
                {"role": "user", "content": self._opening_message()},
            ]
            self.ctl.emit("plan", "Task started",
                          f"model {self.model_name}, tools: "
                          f"{', '.join(sorted(self.allowed))}")
        else:
            self.ctl.emit("resumed", "Resumed from checkpoint",
                          f"continuing at step {state.step}")

        specs = self.tools.specs(sorted(self.allowed))
        stopped = "completed"

        while state.step < self.max_steps:
            state.step += 1
            self.ctl.step = state.step
            tool_ctx.step = state.step

            # -- the safe boundary. Pause, cancel and budget are honoured here.
            self.ctl.checkpoint_barrier(state.to_dict(),
                                        label=f"before step {state.step}")

            req = GenRequest(
                messages=[ChatMessage(m["role"], m["content"], m.get("name"))
                          for m in self._trim(state.messages)],
                model=self.model_name, tools=specs, temperature=0.2,
                max_tokens=1600, ctx_tokens=self.context_budget,
                reasoning="low" if state.step > 1 else "medium",
                timeout_s=min(settings.limits.step_time_budget_s,
                              settings.limits.generation_timeout_s),
                task_id=self.task["id"])

            res = gateway.generate(req, task_id=self.task["id"])
            if not res.ok:
                self.ctl.emit("model_error", "Generation failed", res.error)
                state.messages.append({
                    "role": "user",
                    "content": f"The previous generation failed: {res.error}. "
                               f"Continue with a smaller step, or give your final "
                               f"answer using what you already know."})
                if state.step >= 3:
                    stopped = f"generation failed repeatedly: {res.error}"
                    break
                continue

            self.model_name = res.model      # a fallback may have served this
            if res.reasoning:
                self.ctl.emit("reasoning", f"Step {state.step} reasoning",
                              res.reasoning[:1500])

            if res.tool_calls:
                call = res.tool_calls[0]
                name = str(call.get("name", ""))
                cargs = call.get("arguments") or {}
                self.ctl.emit("tool_call", f"Calling {name}",
                              db.jdump(cargs)[:600], {"tool": name, "args": cargs})

                # Repetition guard. A model that gets an unhelpful error often
                # retries the identical call indefinitely, burning the step
                # budget without changing anything. Detect it and say so
                # explicitly rather than letting the trajectory spin.
                signature = f"{name}:{db.jdump(cargs)}"
                history = state.scratch.setdefault("call_signatures", [])
                repeats = history.count(signature)
                history.append(signature)
                if repeats >= 1:
                    self.ctl.emit("loop_detected", f"Repeated call to {name}",
                                  f"identical arguments seen {repeats + 1} times")
                    state.messages.append({
                        "role": "user",
                        "content": (
                            f"You have already called `{name}` with these exact "
                            f"arguments {repeats + 1} times and received the same "
                            f"result. Repeating it will not change the "
                            f"outcome. Either change the arguments materially, "
                            f"use a different tool, or give your final answer "
                            f"recording what you could not establish as "
                            f"CANNOT DETERMINE.")})
                    continue

                result = self.tools.invoke(name, cargs, tool_ctx,
                                           allowed_tools=self.allowed)
                state.tool_calls += 1
                observation = result.for_model(OBSERVATION_LIMIT)
                self.ctl.emit("observation", f"{name} -> "
                                             f"{'ok' if result.ok else 'error'}",
                              (result.display or observation)[:900])

                if res.text.strip():
                    state.messages.append({"role": "assistant",
                                           "content": res.text.strip()})
                state.messages.append({
                    "role": "tool", "name": name,
                    "content": f"Result of {name}:\n{observation}"})
                state.scratch = tool_ctx.scratch
                continue

            # No tool call: this is the final answer.
            state.final = res.text.strip()
            state.messages.append({"role": "assistant", "content": state.final})
            break
        else:
            stopped = f"reached the {self.max_steps}-step limit"

        if not state.final:
            state.final = self._forced_summary(state, tool_ctx)
            if stopped == "completed":
                stopped = "no final answer produced; summarised from the trajectory"

        self.ctl.save_checkpoint(state.to_dict(), reason="final")
        return AgentResult(final=state.final, steps=state.step,
                           tool_calls=state.tool_calls, model=self.model_name,
                           stopped_because=stopped, scratch=state.scratch)

    # -- helpers ----------------------------------------------------------
    # Words that promise a specific document the user believes they supplied.
    _DEICTIC = re.compile(
        r"\b(the |this |these |that |attached|uploaded|enclosed|provided)\s*"
        r"(attached|uploaded|enclosed|provided|doc\w*|file|pdf|report|drawing|"
        r"sheet|spreadsheet|paper|note)\b", re.I)

    def _opening_message(self) -> str:
        parts = [self.task["prompt"]]
        attachments = db.jload(self.task.get("attachments"), []) or []

        if attachments:
            # Name the exact call to make. Told only to "work on these
            # documents", the model listed the whole document catalogue and
            # answered with that instead of opening the file it had been given.
            lines = ["", "ATTACHED TO THIS TASK — this is what the request is about:"]
            for a in attachments:
                lines.append(
                    f"  - {a.get('title') or a.get('name')} "
                    f"({a.get('kind', 'file')}"
                    + (f", doc_id={a['doc_id']}" if a.get("doc_id") else "")
                    + (f", {a['pages']} page(s)" if a.get("pages") else "") + ")")

            first = attachments[0]
            ref = first.get("doc_id") or first.get("title") or ""
            pages = int(first.get("pages") or 1)
            lines += [
                "",
                "Your FIRST action must be to read it. Do not call list_files or "
                "list_documents — you already know which document this is about, "
                "and listing the catalogue is not an answer.",
                "",
                f'  read_document_page with {{"doc_id": "{ref}", "page_no": 1}}',
            ]
            if pages > 1:
                lines.append(f"  ... then pages 2 to {min(pages, 6)} as needed.")
            if first.get("kind") == "drawing":
                lines.append('  analyse_drawing for the drawing itself.')
            lines += [
                "",
                "`extract_document_values` gets labelled engineering values off "
                "it, and `search_knowledge` finds passages within it. Answer only "
                "about this document; never substitute another.",
            ]
            parts.append("\n".join(lines))

        elif self._DEICTIC.search(self.task["prompt"] or ""):
            # The request names a document the operator thinks they attached, and
            # nothing is attached. Left unsaid, a model will pick the most
            # plausible indexed document and answer about that instead —
            # observed doing exactly this, summarising an inspection report the
            # user had never mentioned, with every value correctly cited and the
            # whole answer about the wrong file.
            # Give the model the real inventory rather than asking it to recall
            # one. Told to "list what is indexed", it produced a confident,
            # entirely fictional list — correct in form, invented in content.
            rows = db.query(
                "SELECT title, doc_class, pages FROM documents "
                "WHERE status='READY' ORDER BY created_at DESC LIMIT 25")
            if rows:
                inventory = "\n".join(
                    f"  - {r['title']} ({r['doc_class']}, {r['pages']} pages)"
                    for r in rows)
            else:
                inventory = "  (nothing is indexed yet)"
            parts.append(
                "\nIMPORTANT: your request refers to an attached or specific "
                "document, but NOTHING IS ATTACHED to this task.\n\n"
                "You must not substitute a different document. Reply that no "
                "document was attached and ask for the file to be attached "
                "through the workbench. Do not summarise or analyse anything "
                "else.\n\n"
                "These documents are already indexed, in case one of them was "
                "meant. This is the complete and exact list — reproduce it "
                "verbatim if you show it, and do not add to it:\n"
                + inventory)
        return "\n".join(parts)

    def _forced_summary(self, state: AgentState, ctx: ToolContext) -> str:
        """Ask for a conclusion when the loop ended without one.

        Better than returning nothing: the trajectory usually contains the
        answer, and an explicit summary keeps the failure visible.
        """
        state.messages.append({
            "role": "user",
            "content": "Stop calling tools. Give your final answer now, using "
                       "only what the tool results above established. State "
                       "CANNOT DETERMINE for anything they do not support."})
        res = gateway.generate(GenRequest(
            messages=[ChatMessage(m["role"], m["content"], m.get("name"))
                      for m in self._trim(state.messages)],
            model=self.model_name, temperature=0.1, max_tokens=1200,
            ctx_tokens=self.context_budget, reasoning="low",
            timeout_s=settings.limits.step_time_budget_s,
            task_id=self.task["id"]), task_id=self.task["id"])
        if res.ok and res.text.strip():
            return res.text.strip()
        return ("CANNOT DETERMINE - the agent did not reach a conclusion and the "
                "final summarisation attempt also failed. The tool results "
                "recorded in the trace are the only established facts.")


def run_agent(task: dict[str, Any], ctl: TaskControl,
              resume: dict[str, Any] | None = None, *,
              allowed_tools: list[str] | None = None,
              extra_instructions: str = "",
              max_steps: int | None = None) -> dict[str, Any]:
    harness = AgentHarness(task, ctl, allowed_tools=allowed_tools,
                           max_steps=max_steps,
                           extra_instructions=extra_instructions)
    result = harness.run(resume)
    return result.to_dict()
