"""Explicit human-approval tool.

Distinct from the automatic approval gate in the tool policy: this lets an agent
*ask* for a decision it has no authority to make, such as sign-off on a
recommendation, and blocks until an operator answers.
"""
from __future__ import annotations

from typing import Any

from ..policy import tool_policy
from ..policy.tool_policy import Risk
from .base import Tool, ToolContext, ToolResult


class RequestApprovalTool(Tool):
    name = "request_human_approval"
    risk = Risk.READ_ONLY          # asking is safe; the gate is what it waits on
    timeout_s = 900.0
    description = (
        "Ask a human operator to approve or reject a decision, and wait for the "
        "answer. Use this when a judgement is outside your authority -- for "
        "example before recommending that equipment stays in service.")
    parameters = {"type": "object", "properties": {
        "question": {"type": "string"},
        "context": {"type": "string",
                    "description": "what the operator needs to know to decide"},
    }, "required": ["question"]}

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        question = str(args.get("question", "")).strip()
        if not question:
            return ToolResult(False, error="question must not be empty")
        summary = question + (f"\n\nContext: {args.get('context')}"
                              if args.get("context") else "")
        aid = tool_policy.request_approval(ctx.task_id, "human_decision",
                                           dict(args), summary)
        ctx.note("approval_pending", "Human decision requested", question,
                 {"approval_id": aid})
        state = tool_policy.wait_for_approval(aid, timeout_s=self.timeout_s - 30)
        ctx.note("approval_decided", f"Operator {state.lower()}", question,
                 {"approval_id": aid, "state": state})
        return ToolResult(
            True,
            content={"decision": state, "question": question,
                     "approval_id": aid},
            display=f"operator {state.lower()}")
