"""Tool gateway.

Agents never touch the host. Every capability is a `Tool` with a declared JSON
schema and a static risk class, invoked through `ToolGateway.invoke`, which:

    1. checks the tool is in the set granted to this task;
    2. asks the policy engine whether it may run and whether a human must approve;
    3. blocks for approval if required;
    4. executes with a timeout;
    5. records arguments, decision, reason, result and duration.

Every one of those steps produces a row an operator can read afterwards.
"""
from __future__ import annotations

import abc
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

from .. import audit, db
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

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "content": self.content, "display": self.display,
                "error": self.error, "meta": self.meta}

    def for_model(self, limit: int = 6000) -> str:
        """The observation string handed back to the agent."""
        if not self.ok:
            return f"ERROR: {self.error}"
        if isinstance(self.content, str):
            body = self.content
        else:
            body = db.jdump(self.content)
        if len(body) > limit:
            body = body[:limit] + f"\n[... truncated, {len(body) - limit} more chars]"
        return body


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
               approval_timeout_s: float = 300.0) -> ToolResult:
        started = time.time()
        call_id = db.new_id("tc")
        tool = self._tools.get(name)

        if tool is None:
            res = ToolResult(False, error=f"no such tool {name!r}; available tools "
                                          f"are {sorted(self._tools)}")
            self._record(call_id, ctx, name, args, "NO_SUCH_TOOL",
                         res, started, "tool not registered")
            return res

        decision = policy.evaluate(name, tool.risk, args, task_id=ctx.task_id,
                                   allowed_tools=allowed_tools)
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
            state = tool_policy.wait_for_approval(aid, timeout_s=approval_timeout_s)
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
        db.insert("tool_calls", {
            "id": call_id, "task_id": ctx.task_id, "step": ctx.step, "tool": name,
            "args": db.jdump(args)[:4000], "decision": decision,
            "policy_mode": policy.mode, "reason": reason[:600],
            "result": db.jdump(res.to_dict())[:8000], "ok": int(res.ok),
            "duration_s": round(duration, 3), "created_at": started})
        audit.record("tool", name, outcome=decision, task_id=ctx.task_id,
                     detail={"args": db.jdump(args)[:400], "ok": res.ok,
                             "duration_s": round(duration, 2),
                             "error": res.error[:200]})


tools = ToolGateway()
