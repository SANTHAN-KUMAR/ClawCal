"""Workflow runners.

A runner is what the scheduler executes once a task has been admitted. Each one
receives the task row, a `TaskControl` handle and any resume state, and must call
`ctl.checkpoint_barrier(...)` at every safe boundary so it can be preempted.

`general` delegates straight to the agent harness. The others shape the
trajectory: they restrict the tool set, add task-specific instructions, and -- in
the batch case -- structure the work as a sequence of independently resumable
units, which is what makes the pause/resume demonstration real rather than
staged.
"""
from __future__ import annotations

import time
from typing import Any

from .. import audit, db, outcomes
from ..agent.harness import run_agent
from ..evidence import provenance
from ..gateway import gateway
from ..gateway.base import ChatMessage, GenRequest
from ..policy import egress
from ..runtime.control import TaskControl
from ..tools import register_all
from ..tools.base import ToolContext
from ..tools.calculator import task_calculations
from ..tools.sandbox import egress_probe

# Tool sets per workflow. Least privilege: a summarisation batch has no reason to
# be able to execute code or write documents.
TOOLSETS: dict[str, list[str]] = {
    "general": ["calculator", "search_knowledge", "list_documents",
                "read_document_page", "extract_document_values",
                "list_files", "read_file", "write_file",
                "run_code", "analyse_drawing", "trace_drawing_connection",
                "generate_approval_note", "generate_report",
                "generate_spreadsheet", "generate_presentation",
                "spreadsheet_read", "spreadsheet_edit",
                "request_human_approval"],
    "inspection_to_approval": ["extract_document_values", "calculator",
                               "search_knowledge", "list_documents",
                               "read_document_page", "spreadsheet_read",
                               "generate_approval_note",
                               "generate_spreadsheet", "request_human_approval"],
    "drawing_review": ["analyse_drawing", "trace_drawing_connection",
                       "search_knowledge", "read_document_page", "calculator",
                       "generate_report"],
    "coding": ["run_code", "write_file", "read_file", "list_files", "calculator"],
    "batch_summarize": ["search_knowledge", "read_document_page",
                        "list_documents"],
    "engineering_qa": ["extract_document_values", "calculator",
                       "search_knowledge", "list_documents",
                       "read_document_page", "spreadsheet_read",
                       "generate_report"],
    "sovereignty_proof": ["sovereignty_selftest", "run_code"],
}


def _bind(task: dict[str, Any]) -> None:
    """Attribute any in-process egress attempt on this thread to this task."""
    egress.bind_task(task["id"])


# --------------------------------------------------------------------- general

def general_runner(task: dict[str, Any], ctl: TaskControl,
                   resume: dict[str, Any] | None) -> dict[str, Any]:
    _bind(task)
    tools = list(TOOLSETS.get(task.get("workflow"), TOOLSETS["general"]))
    if db.jload(task.get("attachments"), []):
        # The document is already chosen. Leaving the workspace browser in the
        # tool set invites the model to go looking for it instead of opening it.
        tools = [t for t in tools if t not in ("list_files", "read_file")]
    result = run_agent(task, ctl, resume, allowed_tools=tools)
    return _finalise(task, ctl, result)


# ------------------------------------------------- inspection -> approval note

def inspection_to_approval_runner(task: dict[str, Any], ctl: TaskControl,
                                  resume: dict[str, Any] | None) -> dict[str, Any]:
    _bind(task)
    result = run_agent(
        task, ctl, resume,
        allowed_tools=TOOLSETS["inspection_to_approval"],
        extra_instructions=(
            "Finish by calling generate_approval_note. Populate observed_values "
            "from the inspection report with the document and page for each, "
            "derived_values from your calculator results, compliance with one "
            "entry per SOP clause you checked, and unresolved with anything the "
            "evidence did not establish."),
        max_steps=18)
    return _finalise(task, ctl, result)


# ---------------------------------------------------------------- drawing review

def drawing_review_runner(task: dict[str, Any], ctl: TaskControl,
                          resume: dict[str, Any] | None) -> dict[str, Any]:
    _bind(task)
    result = run_agent(task, ctl, resume,
                       allowed_tools=TOOLSETS["drawing_review"], max_steps=14)
    return _finalise(task, ctl, result)


# ------------------------------------------------------------------------ coding

def coding_runner(task: dict[str, Any], ctl: TaskControl,
                  resume: dict[str, Any] | None) -> dict[str, Any]:
    _bind(task)
    result = run_agent(
        task, ctl, resume, allowed_tools=TOOLSETS["coding"],
        extra_instructions=(
            "You must actually execute the code with run_code and read the "
            "output before reporting a result. Save the finished code to the "
            "workspace with write_file so it can be delivered, and state the "
            "exit code and the observed output in your final answer."),
        max_steps=16)
    _register_workspace_code(task, ctl)
    return _finalise(task, ctl, result)


CODE_SUFFIXES = {".py", ".sh", ".js", ".sql", ".c", ".cpp", ".java", ".go",
                 ".rs", ".rb", ".pl", ".ts", ".yaml", ".yml", ".json", ".toml",
                 ".md", ".txt", ".csv"}


def _register_workspace_code(task: dict[str, Any], ctl: TaskControl) -> None:
    """Publish code the agent wrote as a downloadable deliverable.

    The problem statement asks for working code as an output alongside the
    documents. Without this the code exists only inside the task workspace,
    which is a directory an operator has no reason to know about.
    """
    import hashlib
    import shutil

    from ..config import ARTIFACT_DIR, WORKSPACE_DIR

    ws = WORKSPACE_DIR / task["id"]
    if not ws.is_dir():
        return
    published = 0
    for p in sorted(ws.iterdir()):
        if not p.is_file() or p.name.startswith(("_sandbox_init", "snippet_")):
            continue
        if p.suffix.lower() not in CODE_SUFFIXES or p.stat().st_size > 2_000_000:
            continue
        dest = ARTIFACT_DIR / f"{task['id']}-{p.name}"
        shutil.copyfile(p, dest)
        data = dest.read_bytes()
        db.insert("artifacts", {
            "id": db.new_id("art"), "task_id": task["id"], "name": p.name,
            "kind": "source_code", "path": str(dest), "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "meta": db.jdump({"workspace": str(ws), "language": p.suffix.lstrip(".")}),
            "created_at": time.time()})
        published += 1
    if published:
        ctl.emit("deliverable", f"Published {published} source file(s)",
                 "code written by the agent is downloadable from Deliverables")


# ------------------------------------------------------------------------- batch

def batch_summarize_runner(task: dict[str, Any], ctl: TaskControl,
                           resume: dict[str, Any] | None) -> dict[str, Any]:
    """Summarise a set of documents, one resumable unit at a time.

    The checkpoint is taken *between documents*, so a preemption costs at most
    one document's work and the resumed run does not redo what is already done.
    This is the workload the resource-governance demonstration displaces.
    """
    _bind(task)
    state = dict(resume or {})
    done: dict[str, Any] = state.get("done", {})
    params = db.jload(task.get("attachments"), []) or []
    doc_ids = [a["doc_id"] for a in params if a.get("doc_id")]

    if not doc_ids:
        rows = db.query("SELECT id FROM documents WHERE status='READY' "
                        "ORDER BY created_at")
        doc_ids = [r["id"] for r in rows]
    if not doc_ids:
        return {"summary": "No documents are indexed, so there is nothing to "
                           "summarise.", "documents": 0}

    ctl.emit("plan", "Batch summarisation",
             f"{len(doc_ids)} documents, {len(done)} already complete")

    for idx, doc_id in enumerate(doc_ids, 1):
        if doc_id in done:
            continue
        ctl.step = idx
        # Safe boundary between units of work.
        ctl.checkpoint_barrier({"done": done, "cursor": idx},
                               label=f"before document {idx}/{len(doc_ids)}")

        row = db.query_one("SELECT title, doc_class, pages, status FROM documents "
                           "WHERE id=?", (doc_id,))
        if not row:
            continue
        if row["status"] != "READY":
            done[doc_id] = {"title": row["title"],
                            "summary": "EXTRACTION FAILED - not summarised",
                            "ok": False}
            continue

        pages = db.query("SELECT page_no, text FROM pages WHERE doc_id=? "
                         "ORDER BY page_no LIMIT 6", (doc_id,))
        body = "\n\n".join(f"[page {p['page_no']}]\n{p['text']}"
                           for p in pages if (p["text"] or "").strip())[:9000]

        res = gateway.generate(GenRequest(
            messages=[
                ChatMessage("system",
                            "Summarise this industrial document in at most six "
                            "bullet points. Preserve every equipment tag, "
                            "reference number, date and measured value exactly "
                            "as written. Do not round or convert numbers. Do not "
                            "add anything not present in the text."),
                ChatMessage("user", f"Document: {row['title']}\n\n{body}"),
            ],
            model=task.get("selected_model") or "qwen3-8b",
            temperature=0.1, max_tokens=420, reasoning="low",
            ctx_tokens=int(task.get("context_budget") or 8192),
            timeout_s=300, task_id=task["id"]), task_id=task["id"])

        done[doc_id] = {"title": row["title"], "doc_class": row["doc_class"],
                        "summary": res.text.strip() if res.ok
                        else f"SUMMARISATION FAILED: {res.error}",
                        "ok": res.ok}
        ctl.emit("batch_item", f"Summarised {row['title']}",
                 f"{idx}/{len(doc_ids)}", {"doc_id": doc_id, "ok": res.ok})

    ctl.save_checkpoint({"done": done, "cursor": len(doc_ids)}, reason="complete")
    ok = sum(1 for v in done.values() if v.get("ok"))
    return {"summary": f"Summarised {ok} of {len(doc_ids)} documents.",
            "documents": len(doc_ids), "succeeded": ok,
            "results": list(done.values())}


# ------------------------------------------------------------- sovereignty proof

def sovereignty_proof_runner(task: dict[str, Any], ctl: TaskControl,
                             resume: dict[str, Any] | None) -> dict[str, Any]:
    """Attack the sovereign boundary from every layer and record what happened."""
    _bind(task)
    findings: list[dict[str, Any]] = []

    ctl.emit("plan", "Sovereignty self-test",
             "attempting egress from the control plane, the sandbox and the "
             "tool layer")
    ctl.checkpoint_barrier({"phase": "start"}, label="sovereignty proof start")

    # Layer 2: in-process guard.
    import requests
    for target in ("https://api.openai.com/v1/chat/completions",
                   "https://vendor-portal-sync.example.com/upload",
                   "http://8.8.8.8:53"):
        try:
            requests.get(target, timeout=4)
            findings.append({"layer": "app-guard", "target": target,
                             "result": "CONNECTED", "ok": False})
        except Exception as exc:
            findings.append({"layer": "app-guard", "target": target,
                             "result": "BLOCKED", "ok": True,
                             "error": type(exc).__name__})
    ctl.emit("sovereignty", "Control-plane egress attempts",
             f"{sum(1 for f in findings if f['ok'])}/{len(findings)} blocked")

    ctl.checkpoint_barrier({"phase": "sandbox"}, label="before sandbox probe")

    # Layer 3: sandbox network namespace.
    probe = egress_probe(task["id"])
    if not probe.get("attempts"):
        # A probe that never ran blocked nothing: it proves nothing either way.
        findings.append({"layer": "sandbox-netns", "target": "(probe did not run)",
                         "result": "CANNOT DETERMINE", "ok": False,
                         "error": (probe.get("raw") or {}).get("stderr", "")[:200]})
    for a in probe.get("attempts", []):
        findings.append({"layer": "sandbox-netns", "target": a.get("target"),
                         "result": a.get("result"),
                         "ok": a.get("result") == "BLOCKED"})
    ctl.emit("sovereignty", "Sandbox egress attempts",
             "all blocked" if probe.get("all_blocked") else "LEAK DETECTED",
             probe)

    # Layer 4: host firewall state.
    nft = egress.nftables_status()

    # Layer 5: the audit record itself.
    chain = audit.verify_chain()
    recent = db.rows_to_dicts(db.query(
        "SELECT destination, port, layer, result, ts FROM network_events "
        "WHERE task_id=? ORDER BY ts DESC LIMIT 40", (task["id"],)))

    all_blocked = all(f["ok"] for f in findings) and bool(findings)
    summary = (
        f"Sovereignty self-test: {sum(1 for f in findings if f['ok'])} of "
        f"{len(findings)} outbound attempts blocked across the application guard "
        f"and the sandbox network namespace. "
        f"Host nftables default-deny table: "
        f"{'loaded' if nft.get('loaded') else 'could not be read without root privilege (check with: sudo ops/egress-policy.sh status)' if nft.get('loaded') is None else 'NOT loaded'}. "
        f"Audit chain: {'verified' if chain['ok'] else 'BROKEN'} over "
        f"{chain.get('entries', 0)} entries. "
        f"{len(recent)} denial events were recorded and attributed to this task.")

    ctl.emit("sovereignty", "Self-test complete", summary,
             {"findings": findings, "nftables": nft, "audit": chain})
    audit.record("sovereignty", "self_test", task_id=task["id"],
                 outcome="BLOCKED" if all_blocked else "LEAK",
                 detail={"findings": findings, "nftables": nft})
    from ..control import decisions
    decisions.record("sovereignty", "SELFTEST_PASS" if all_blocked else "SELFTEST_LEAK",
                     summary, subject_kind="selftest", subject_id=task["id"],
                     task_id=task["id"], principal=task.get("owner"),
                     basis={"blocked": sum(1 for f in findings if f["ok"]),
                            "attempts": len(findings),
                            "nftables_loaded": bool(nft.get("loaded")),
                            "audit_ok": chain.get("ok")})
    # The refusal contract applies here too. Every attempt refused is an
    # established fact; a host firewall this process cannot confirm is a layer
    # running unverified, which the reader must be told, not left to infer.
    degraded = []
    if not nft.get("loaded"):
        degraded.append("sovereignty: the host firewall layer is "
                        + ("not applied" if nft.get("loaded") is False
                           else "unverified without root privilege"))
    headline = ("CANNOT_DETERMINE" if not all_blocked else
                "DEGRADED" if degraded else "ESTABLISHED")
    return {"summary": summary, "all_blocked": all_blocked,
            "findings": findings, "nftables": nft, "audit_chain": chain,
            "denial_events": recent,
            "outcome": {"headline": headline,
                        "label": headline.replace("_", " "),
                        "degraded": degraded, "tool_outcomes": {},
                        "value_outcomes": {}}}


# ---------------------------------------------------------------------- shared

def _finalise(task: dict[str, Any], ctl: TaskControl,
              result: dict[str, Any]) -> dict[str, Any]:
    """Provenance-check the final answer and attach the evidence record."""
    text = result.get("summary", "") or ""
    passages = (result.get("scratch") or {}).get("passages", []) \
        if isinstance(result.get("scratch"), dict) else []
    if not passages:
        rows = db.query(
            "SELECT e.snippet, e.doc_id, e.page_no, e.region, "
            "COALESCE(d.title, '') AS title FROM evidence e "
            "LEFT JOIN documents d ON d.id = e.doc_id WHERE e.task_id=?",
            (task["id"],))
        passages = [{"text": r["snippet"], "doc_id": r["doc_id"],
                     "page_no": r["page_no"], "doc_title": r["title"],
                     "region": db.jload(r["region"])} for r in rows]

    ctx = provenance.EvidenceContext(
        passages=passages, calculations=task_calculations(task["id"]),
        working_set={a["doc_id"] for a in (db.jload(task.get("attachments"), []) or [])
                     if isinstance(a, dict) and a.get("doc_id")})
    report = provenance.classify_text(text, ctx, task_id=task["id"])
    counts = report.counts

    ctl.emit("provenance", "Provenance check",
             f"{counts['A']} source, {counts['B']} derived, "
             f"{counts['C']} interpretation, {counts['D']} unsupported",
             report.to_dict())

    # The answer's outcome under the refusal contract: per-value classes from
    # provenance, plus every tool outcome in the trajectory, so an answer
    # produced while a capability was degraded is marked degraded.
    tool_rows = db.rows_to_dicts(db.query(
        "SELECT tool, outcome, outcome_reason AS reason FROM tool_calls "
        "WHERE task_id=? ORDER BY created_at", (task["id"],)))
    stopped = str(result.get("stopped_because") or "")
    if stopped and stopped != "completed":
        tool_rows.append({"tool": "agent", "outcome": "DEGRADED",
                          "reason": f"the trajectory ended early: {stopped}"})
    answer_outcome = outcomes.summarise(tool_rows, counts)
    from ..control import decisions
    decisions.record("evidence", answer_outcome["headline"],
                     f"final answer: {counts['A']} source, {counts['B']} derived, "
                     f"{counts['C']} interpretation, {counts['D']} unsupported"
                     + (f"; degraded: {'; '.join(answer_outcome['degraded'])}"
                        if answer_outcome["degraded"] else ""),
                     subject_kind="answer", subject_id=task["id"],
                     task_id=task["id"], principal=task.get("owner"),
                     basis={"counts": counts,
                            "tool_outcomes": answer_outcome["tool_outcomes"]})

    artifacts = db.rows_to_dicts(db.query(
        "SELECT id, name, kind, bytes, sha256 FROM artifacts WHERE task_id=?",
        (task["id"],)))

    result.pop("scratch", None)
    # The delivered answer never states an unsupported number as fact. The
    # model's own wording survives in `model_text` and, marked, in `annotated`
    # (the web shows it with a red D badge); `summary` — what every client
    # prints as the answer — carries the refusal in place of the value.
    delivered = provenance.redact_unsupported(text, report)
    return {**result,
            "summary": delivered,
            "model_text": text,
            "outcome": answer_outcome,
            "annotated": provenance.annotate(text, report),
            "provenance": report.to_dict(),
            "artifacts": artifacts,
            "calculations": task_calculations(task["id"]),
            "evidence_count": len(passages)}


RUNNERS = {
    "general": general_runner,
    "inspection_to_approval": inspection_to_approval_runner,
    "drawing_review": drawing_review_runner,
    "coding": coding_runner,
    "engineering_qa": general_runner,
    "batch_summarize": batch_summarize_runner,
    "sovereignty_proof": sovereignty_proof_runner,
}


def register(scheduler: Any) -> None:
    for name, fn in RUNNERS.items():
        scheduler.register_runner(name, fn)
    audit.record("runtime", "runners_registered",
                 detail={"workflows": sorted(RUNNERS)})
