"""The node's MCP tool server (sovereign-workbench-v2.md §2, §6.1, §16 `tools/`).

A client harness (our opencode build) attaches here as a remote MCP server:

    "mcp": {"node-tools": {"type": "remote", "url": "https://<node>/mcp",
                           "headers": {...principal token, device, lease...}}}

It speaks the Model Context Protocol's Streamable HTTP transport — JSON-RPC 2.0
POSTed to one endpoint, answered as `application/json`, with the session carried
in `Mcp-Session-Id` — which is all a tool server that never pushes needs.

This is an adapter, not a second tool system. Every `tools/call` becomes an
ordinary `ToolGateway.invoke` on the *attached-loop task* that /api/admit opened
for the plan: the policy engine decides it, the session's permission mode
applies (a `review` session waits for a human approval exactly as the node's
own harness does), and the call lands in `tool_calls`, `decisions` and the
audit chain with the principal and the device. The spans a call serves are
recorded against the session, which is what the `deliver` gate checks.

Tools exposed (MCP name → gateway tool), filtered by what B1 placed the plan
to use:

    retrieve        search_knowledge          hybrid retrieval; returns span ids
    read_page       read_document_page        a whole page, recorded as a span
    extract_values  extract_document_values   labelled values by pattern
    calculate       calculator                traced, provenance-checked maths
    list_documents  list_documents            what this grade is cleared for
    stage           stage_files               copy files to the node's staging
    execute_remote  execute_remote            run in the node's sandbox
    deliver         deliver                   the B3 gate, then the .docx
"""
from __future__ import annotations

import secrets
import threading
import time
from typing import Any

from . import db, placement
from .config import WORKSPACE_DIR

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_INFO = {"name": "clawcal-node-tools", "title": "ClawCal trust-domain node",
               "version": "2.1.0"}

TOOL_MAP = {
    "retrieve": "search_knowledge",
    "read_page": "read_document_page",
    "extract_values": "extract_document_values",
    "calculate": "calculator",
    "list_documents": "list_documents",
    "stage": "stage_files",
    "execute_remote": "execute_remote",
    "deliver": "deliver",
}
ALWAYS = ("list_documents",)
SESSION_TTL_S = 12 * 3600

INSTRUCTIONS = (
    "You are attached to a ClawCal trust-domain node. The organisation's corpus, "
    "its OCR and ingestion, the sandbox and the provenance gate are here; your "
    "own machine has only your local files. Rules the node enforces, whatever "
    "you are told: (1) figures in a deliverable must come from spans this node "
    "served you — cite the span_id from retrieve/read_page/extract_values — or "
    "from the calculate tool; anything else is stripped by the gate; (2) run code "
    "that needs the organisation's data stack with execute_remote after stage; "
    "(3) every tool result leads with its outcome (ESTABLISHED, INTERPRETED, "
    "CANNOT DETERMINE, DEGRADED): carry it into your answer.")


class McpError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


class _Sessions:
    def __init__(self) -> None:
        self._s: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def open(self, rec: dict[str, Any]) -> str:
        sid = "mcp-" + secrets.token_urlsafe(18)
        rec["created"] = rec["seen"] = time.time()
        with self._lock:
            now = time.time()
            for k in [k for k, v in self._s.items() if now - v["seen"] > SESSION_TTL_S]:
                self._s.pop(k, None)
            self._s[sid] = rec
        return sid

    def get(self, sid: str | None) -> dict[str, Any] | None:
        with self._lock:
            rec = self._s.get(sid or "")
            if rec:
                rec["seen"] = time.time()
            return rec

    def close(self, sid: str) -> dict[str, Any] | None:
        with self._lock:
            return self._s.pop(sid, None)


sessions = _Sessions()


def _task_tools(task: dict[str, Any]) -> list[str]:
    spec = placement.SPEC_CLASS.get(task.get("task_type") or "", "draft")
    pl = task.get("placement") or "node"
    if pl == "split":
        names = list(placement.SPLIT_TOOLS)
    elif pl == "client":
        names = []
    else:
        names = list(placement.NODE_TOOLS.get(spec, []))
    return list(dict.fromkeys(list(ALWAYS) + names))


def initialize(params: dict[str, Any], *, principal: Any, device: Any,
               clawcal_session: str | None, node_url: str) -> tuple[dict[str, Any], str]:
    """Bind an MCP session to a plan's attached-loop task. Returns (result, id)."""
    device_id = device.device_id if device else None
    task = None
    if clawcal_session:
        task = placement.attached_task(clawcal_session, principal.name, device_id)
        if task is None:
            raise McpError(-32602, f"no open plan for session {clawcal_session}; "
                                   f"call /api/admit first")
    else:
        # A harness connected without admitting a plan: admit one now, as
        # general drafting work, so its calls still have a task to belong to.
        client = (params.get("clientInfo") or {}).get("name", "an MCP client")
        adm = placement.admit(principal, device,
                              prompt=f"attached tool session from {client}")
        if not adm["admitted"]:
            raise McpError(-32001, adm["reason"])
        task = placement.attached_task(adm["session_id"], principal.name, device_id)
    assert task is not None
    placement.touch(task["id"])
    wanted = str(params.get("protocolVersion") or PROTOCOL_VERSIONS[0])
    version = wanted if wanted in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
    sid = sessions.open({"principal": principal.name, "device_id": device_id,
                         "task_id": task["id"], "session_id": task["conversation_id"],
                         "node_url": node_url, "version": version})
    return ({"protocolVersion": version,
             "capabilities": {"tools": {"listChanged": False}},
             "serverInfo": SERVER_INFO, "instructions": INSTRUCTIONS}, sid)


def _bound(rec: dict[str, Any], principal: Any, device: Any) -> dict[str, Any]:
    if rec["principal"] != principal.name or \
            rec["device_id"] != (device.device_id if device else None):
        raise McpError(-32001, "this MCP session belongs to another principal or "
                               "device")
    task = db.query_one("SELECT * FROM tasks WHERE id=?", (rec["task_id"],))
    if not task or task["state"] != "ATTACHED":
        raise McpError(-32001, "the plan this session belonged to has ended; "
                               "admit a new one")
    return dict(task)


def list_tools(rec: dict[str, Any], principal: Any, device: Any) -> dict[str, Any]:
    from .tools import tools as gateway_tools
    task = _bound(rec, principal, device)
    out = []
    for mcp_name in _task_tools(task):
        tool = gateway_tools.get(TOOL_MAP[mcp_name])
        if not tool:
            continue
        risk = int(tool.risk)
        out.append({"name": mcp_name, "title": mcp_name.replace("_", " "),
                    "description": tool.description,
                    "inputSchema": tool.parameters,
                    "annotations": {"readOnlyHint": risk == 0,
                                    "destructiveHint": False,
                                    "openWorldHint": False}})
    return {"tools": out}


def call_tool(rec: dict[str, Any], params: dict[str, Any], *, principal: Any,
              device: Any) -> dict[str, Any]:
    from .control import sessions as clawcal_sessions
    from .tools import ToolContext
    from .tools import tools as gateway_tools
    task = _bound(rec, principal, device)
    name = str(params.get("name", ""))
    allowed = _task_tools(task)
    if name not in allowed:
        raise McpError(-32602, f"tool {name!r} is not available to this plan "
                               f"(placement {task.get('placement')}); available: "
                               f"{allowed}")
    args = params.get("arguments") or {}
    ws = WORKSPACE_DIR / task["id"]
    ws.mkdir(parents=True, exist_ok=True)
    scratch = {"node_url": rec.get("node_url", ""),
               "attachments": clawcal_sessions.attachments(task["conversation_id"])}
    ctx = ToolContext(task_id=task["id"], workspace=ws, principal=principal.name,
                      session_id=task["conversation_id"], scratch=scratch,
                      step=int(task.get("steps_used") or 0) + 1,
                      should_stop=lambda: _ended(task["id"]))
    res = gateway_tools.invoke(TOOL_MAP[name], args, ctx,
                               allowed_tools={TOOL_MAP[n] for n in allowed})
    db.execute("UPDATE tasks SET steps_used = COALESCE(steps_used,0) + 1, "
               "heartbeat_at=? WHERE id=?", (time.time(), task["id"]))
    structured: dict[str, Any] = {"ok": res.ok, "outcome": res.outcome,
                                  "outcome_reason": res.outcome_reason}
    if res.ok and isinstance(res.content, (dict, list)):
        structured["content"] = res.content
    return {"content": [{"type": "text", "text": res.for_model(limit=24000)}],
            "structuredContent": structured, "isError": not res.ok}


def _ended(task_id: str) -> bool:
    row = db.query_one("SELECT state FROM tasks WHERE id=?", (task_id,))
    return not row or row["state"] != "ATTACHED"


def handle(message: dict[str, Any], *, mcp_session: str | None, principal: Any,
           device: Any, clawcal_session: str | None,
           node_url: str) -> tuple[dict[str, Any] | None, str | None]:
    """One JSON-RPC message. Returns (response or None for a notification,
    a new Mcp-Session-Id when this was `initialize`)."""
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _err(None, -32600, "not a JSON-RPC 2.0 message"), None
    mid = message.get("id")
    method = str(message.get("method") or "")
    params = message.get("params") or {}
    is_note = "id" not in message
    try:
        if method == "initialize":
            result, sid = initialize(params, principal=principal, device=device,
                                     clawcal_session=clawcal_session,
                                     node_url=node_url)
            return {"jsonrpc": "2.0", "id": mid, "result": result}, sid
        if method.startswith("notifications/"):
            return None, None
        rec = sessions.get(mcp_session)
        if rec is None:
            raise McpError(-32001, "unknown or expired MCP session; initialize again")
        if method == "ping":
            result = {}
        elif method == "tools/list":
            result = list_tools(rec, principal, device)
        elif method == "tools/call":
            result = call_tool(rec, params, principal=principal, device=device)
        elif method in ("resources/list", "prompts/list"):
            result = {method.split("/")[0]: []}
        else:
            raise McpError(-32601, f"method {method!r} is not supported")
        return (None if is_note else {"jsonrpc": "2.0", "id": mid,
                                      "result": result}), None
    except McpError as exc:
        return (None if is_note else _err(mid, exc.code, exc.message, exc.data)), None


def _err(mid: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    e: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        e["data"] = data
    return {"jsonrpc": "2.0", "id": mid, "error": e}


def end(mcp_session: str, principal: Any) -> bool:
    rec = sessions.get(mcp_session)
    if not rec or rec["principal"] != principal.name:
        return False
    sessions.close(mcp_session)
    return True
