"""Tool gateway.

Agents never touch the host. Every capability is a `Tool` with a declared JSON
schema and a static risk class, invoked through `ToolGateway.invoke`, which:

    1. checks the tool is in the set granted to this task;
    2. asks the policy engine whether it may run and whether a human must approve;
    3. blocks for approval if required;
    4. executes with a timeout;
    5. records arguments, decision, reason, result, duration and outcome.

Every one of those steps produces a row an operator can read afterwards, and the
policy step also writes a decision record (authority `tool_policy`).

Every result carries one of the four outcomes of the refusal contract
(`sovereign.outcomes`). A tool that knows better sets it; otherwise it is
derived here — a failure is CANNOT_DETERMINE, never silence.
"""
from __future__ import annotations

import abc
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

from .. import audit, db, outcomes
from ..gateway.base import ToolSpec
from ..policy import tool_policy
from ..policy.tool_policy import Risk, policy


@dataclass
class ToolResult:
    ok: bool
    content: Any = None
    display: str = ""
    error: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    # The refusal contract. Empty means "derive it" (see ToolGateway._settle).
    outcome: str = ""
    outcome_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "content": self.content, "display": self.display,
                "error": self.error, "meta": self.meta, "outcome": self.outcome,
                "outcome_reason": self.outcome_reason}

    def for_model(self, limit: int = 6000) -> str:
        """The observation string handed back to the agent.

        The outcome leads, so the model reads DEGRADED or CANNOT DETERMINE
        before it reads the content and can carry it into its answer.
        """
        tag = ""
        if self.outcome and self.outcome != outcomes.ESTABLISHED:
            tag = f"[OUTCOME: {outcomes.LABELS[self.outcome]}"
            tag += f" — {self.outcome_reason}]\n" if self.outcome_reason else "]\n"
        if not self.ok:
            return f"{tag}ERROR: {self.error}"
        if isinstance(self.content, str):
            body = self.content
        else:
            body = db.jdump(self.content)
        if len(body) > limit:
            body = body[:limit] + f"\n[... truncated, {len(body) - limit} more chars]"
        return tag + body


class Tool(abc.ABC):
    name: str = "tool"
    description: str = ""
    risk: Risk = Risk.READ_ONLY
    parameters: dict[str, Any] = {"type": "object", "properties": {}}
    timeout_s: float = 120.0

    @abc.abstractmethod
    def run(self, args: dict[str, Any], ctx: "ToolContext") -> ToolResult:
        ...

    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=self.description,
                        parameters=self.parameters)

    def approval_summary(self, args: dict[str, Any]) -> str:
        return f"{self.name} with arguments {db.jdump(args)[:400]}"


@dataclass
class ToolContext:
    """What a tool is allowed to know about its caller."""
    task_id: str
    workspace: Any = None                # pathlib.Path, set by the harness
    step: int = 0
    emit: Callable[..., None] | None = None
    scratch: dict[str, Any] = field(default_factory=dict)
    # Who the task runs for, and whether it has been told to stop. The second
    # lets a wait on a human approval end when the task is cancelled.
    principal: str = "system"
    should_stop: Callable[[], bool] | None = None
    session_id: str | None = None

    def note(self, kind: str, label: str, detail: str = "",
             payload: Any = None) -> None:
        if self.emit:
            self.emit(kind, label, detail, payload)


class ToolGateway:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._lock = threading.RLock()

    def register(self, tool: Tool) -> None:
        with self._lock:
            self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def specs(self, names: list[str] | None = None) -> list[ToolSpec]:
        tools = ([self._tools[n] for n in names if n in self._tools]
                 if names else self.all())
        return [t.spec() for t in tools]

    def catalogue(self) -> list[dict[str, Any]]:
        return [{"name": t.name, "description": t.description,
                 "risk": int(t.risk), "risk_name": Risk(t.risk).name,
                 "parameters": t.parameters} for t in self.all()]

    def invoke(self, name: str, args: dict[str, Any], ctx: ToolContext, *,
               allowed_tools: set[str] | None = None,
               approval_timeout_s: float | None = None) -> ToolResult:
        started = time.time()
        call_id = db.new_id("tc")
        tool = self._tools.get(name)

        if tool is None:
            res = ToolResult(False, error=f"no such tool {name!r}; available tools "
                                          f"are {sorted(self._tools)}")
            self._record(call_id, ctx, name, args, "NO_SUCH_TOOL",
                         res, started, "tool not registered")
            return res

        if not isinstance(args, dict):
            # A model that emits a string or a list here would otherwise crash
            # the tool with an AttributeError several frames down.
            res = ToolResult(False, error=f"arguments to {name} must be a JSON "
                                          f"object, got {type(args).__name__}")
            self._record(call_id, ctx, name, {"_raw": str(args)[:400]},
                         "BAD_ARGUMENTS", res, started, "arguments not an object")
            return res

        decision = policy.evaluate(name, tool.risk, args, task_id=ctx.task_id,
                                   allowed_tools=allowed_tools)
        from ..control import decisions
        decisions.record(
            "tool_policy",
            "DENY" if not decision.allowed
            else "ASK" if decision.requires_approval else "ALLOW",
            decision.reason, subject_kind="tool_call", subject_id=call_id,
            task_id=ctx.task_id, principal=ctx.principal,
            basis={"tool": name, "risk": Risk(tool.risk).name,
                   "mode": decision.mode})
        if not decision.allowed:
            res = ToolResult(False, error=f"POLICY_DENIED: {decision.reason}")
            self._record(call_id, ctx, name, args, "DENIED", res, started,
                         decision.reason)
            audit.record("policy", "tool_denied", outcome="DENIED",
                         task_id=ctx.task_id,
                         detail={"tool": name, "reason": decision.reason})
            ctx.note("policy_denied", f"Tool denied: {name}", decision.reason)
            return res

        if decision.requires_approval:
            summary = tool.approval_summary(args)
            aid = tool_policy.request_approval(ctx.task_id, name, args, summary)
            ctx.note("approval_pending", f"Approval required: {name}",
                     decision.reason, {"approval_id": aid, "summary": summary})
            state = tool_policy.wait_for_approval(aid, timeout_s=approval_timeout_s,
                                                  should_stop=ctx.should_stop)
            if state != "APPROVED":
                res = ToolResult(False, error=f"APPROVAL_{state}: the operator did "
                                              f"not approve {name}")
                self._record(call_id, ctx, name, args, f"APPROVAL_{state}", res,
                             started, decision.reason)
                ctx.note("approval_denied", f"Approval {state.lower()}: {name}", "")
                return res
            ctx.note("approval_granted", f"Approved: {name}", "",
                     {"approval_id": aid})

        try:
            res = self._run_with_timeout(tool, args, ctx)
        except Exception as exc:
            res = ToolResult(False, error=f"{type(exc).__name__}: {exc}"[:500],
                             meta={"traceback": traceback.format_exc()[:2000]})

        self._record(call_id, ctx, name, args,
                     "EXECUTED" if res.ok else "FAILED", res, started,
                     decision.reason)
        return res

    @staticmethod
    def _settle(res: ToolResult) -> ToolResult:
        """Give every result an outcome. A tool that set one keeps it."""
        if res.outcome:
            outcomes.check(res.outcome)
        elif not res.ok:
            res.outcome = outcomes.CANNOT_DETERMINE
            res.outcome_reason = res.outcome_reason or (res.error or "")[:240]
        elif res.meta.get("degraded"):
            res.outcome = outcomes.DEGRADED
            res.outcome_reason = res.outcome_reason or str(res.meta["degraded"])[:240]
        else:
            res.outcome = outcomes.ESTABLISHED
        return res

    def _run_with_timeout(self, tool: Tool, args: dict[str, Any],
                          ctx: ToolContext) -> ToolResult:
        """Run in a worker thread so a wedged tool cannot hold the agent forever."""
        box: dict[str, Any] = {}

        def target() -> None:
            try:
                box["res"] = tool.run(args, ctx)
            except Exception as exc:
                box["res"] = ToolResult(
                    False, error=f"{type(exc).__name__}: {exc}"[:500],
                    meta={"traceback": traceback.format_exc()[:2000]})

        th = threading.Thread(target=target, daemon=True,
                              name=f"tool-{tool.name}")
        th.start()
        th.join(tool.timeout_s)
        if th.is_alive():
            return ToolResult(False,
                              error=f"tool {tool.name} exceeded its "
                                    f"{tool.timeout_s:.0f}s timeout and was abandoned")
        return box.get("res", ToolResult(False, error="tool produced no result"))

    def _record(self, call_id: str, ctx: ToolContext, name: str,
                args: dict[str, Any], decision: str, res: ToolResult,
                started: float, reason: str) -> None:
        duration = time.time() - started
        self._settle(res)
        from ..policy.tool_policy import mode_for_task
        mode = mode_for_task(ctx.task_id)[0] or policy.mode
        db.insert("tool_calls", {
            "id": call_id, "task_id": ctx.task_id, "step": ctx.step, "tool": name,
            "args": db.jdump(args)[:4000], "decision": decision,
            "policy_mode": mode, "reason": reason[:600],
            "result": db.jdump(res.to_dict())[:8000], "ok": int(res.ok),
            "duration_s": round(duration, 3), "created_at": started,
            "outcome": res.outcome, "outcome_reason": res.outcome_reason[:600],
            "principal": ctx.principal})
        audit.record("tool", name, outcome=decision, task_id=ctx.task_id,
                     actor=ctx.principal,
                     detail={"args": db.jdump(args)[:400], "ok": res.ok,
                             "duration_s": round(duration, 2),
                             "error": res.error[:200]})


tools = ToolGateway()
