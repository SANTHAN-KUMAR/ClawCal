"""The device's own MCP tool server, for detached work (spec §6.1).

"Tool bridge: MCP client to the node's tool server (attached) or to the local
tool server (detached)." Attached, the harness reaches the node's `/mcp`.
Detached, it starts this — `python -m clawcal.localtools` over stdio — and gets
the same kind of tools, served from the exported slice and gated by the node's
own rules:

    slice_documents   what the slices on this device contain
    retrieve          full-text search over them, with span ids
    deliver           gate a draft against the slice; write it; queue the .docx

MCP stdio transport: newline-delimited JSON-RPC 2.0 on stdin/stdout. Nothing
here opens a socket.
"""
from __future__ import annotations

import json
import sys
from typing import Any

from . import slices

VERSION = "2025-06-18"
TOOLS = [
    {"name": "slice_documents", "description":
        "List the documents in the instrument slices exported to this device.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "retrieve", "description":
        "Search the exported documents; returns passages with span_id, document "
        "and page. Cite the span_id of every figure you use.",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"}, "k": {"type": "integer"}},
         "required": ["query"]}},
    {"name": "deliver", "description":
        "Produce the deliverable from your draft. Every number, equipment tag and "
        "date is checked against the slice's spans: kept and cited, or stripped "
        "and reported. Writes a report now; the organisation's .docx is written "
        "by the node when this device re-attaches.",
     "inputSchema": {"type": "object", "properties": {
         "title": {"type": "string"}, "kind": {"type": "string"},
         "sections": {"type": "array", "items": {"type": "object"}}},
         "required": ["title", "sections"]}},
]


def _call(name: str, args: dict[str, Any]) -> tuple[str, bool]:
    try:
        if name == "slice_documents":
            docs = slices.documents()
            return json.dumps([{k: d[k] for k in ("id", "title", "data_class",
                                                  "slice_id")} for d in docs]), False
        if name == "retrieve":
            hits = slices.search(str(args.get("query", "")), int(args.get("k") or 6))
            if not hits:
                return ("[OUTCOME: CANNOT DETERMINE — nothing in the exported "
                        "documents matched]"), False
            return "\n\n".join(f"[{i}] span_id={h['span_id']} SOURCE: {h['doc_title']}, "
                               f"page {h['page_no']}\n{h['text']}"
                               for i, h in enumerate(hits, 1)), False
        if name == "deliver":
            out = slices.deliver(str(args.get("title") or "Report"),
                                 args.get("sections"),
                                 kind=str(args.get("kind") or "report"))
            c = out["counts"]
            lines = [f"Written: {out['report']}",
                     f"Gate: {c['kept']} kept, {c['stripped']} stripped "
                     f"({c['recited']} re-cited). The .docx is queued for the node."]
            lines += [f"STRIPPED {s['value']}: {s['reason']}" for s in out["stripped"]]
            return "\n".join(lines), False
        return f"no tool {name!r}", True
    except slices.SliceError as exc:
        return f"[OUTCOME: CANNOT DETERMINE — {exc}]", True


def handle(msg: dict[str, Any]) -> dict[str, Any] | None:
    mid, method = msg.get("id"), msg.get("method", "")
    if "id" not in msg:
        return None
    if method == "initialize":
        result: Any = {"protocolVersion": VERSION,
                       "capabilities": {"tools": {"listChanged": False}},
                       "serverInfo": {"name": "clawcal-local-tools", "version": "1.0"}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        p = msg.get("params") or {}
        text, err = _call(str(p.get("name")), p.get("arguments") or {})
        result = {"content": [{"type": "text", "text": text}], "isError": err}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": f"no method {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            reply = handle(json.loads(line))
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32700, "message": "parse error"}}
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
