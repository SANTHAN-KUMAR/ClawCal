#!/usr/bin/env python3
"""End-to-end acceptance verification.

Runs the seventeen acceptance criteria from GOAL.md against the live system and
reports PASS / FAIL / SKIP with the evidence for each. Nothing here is mocked:
models are loaded, documents are OCR'd, code executes in the sandbox, network
connections are genuinely attempted and genuinely refused.

    python3 scripts/verify_e2e.py                 # everything
    python3 scripts/verify_e2e.py --fast          # skip the slow agent trajectories
    python3 scripts/verify_e2e.py --only A13,A16  # named criteria
    python3 scripts/verify_e2e.py --reset         # wipe state and re-ingest first
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from sovereign import audit, db, hardware                       # noqa: E402
from sovereign.config import ARTIFACT_DIR, DATA_DIR             # noqa: E402
from sovereign.evidence import provenance                       # noqa: E402
from sovereign.gateway import gateway                           # noqa: E402
from sovereign.gateway.base import ChatMessage, GenRequest      # noqa: E402
from sovereign.gateway.registry import registry                 # noqa: E402
from sovereign.knowledge import extract, ingest, retrieve       # noqa: E402
from sovereign.policy import egress, injection, tool_policy     # noqa: E402
from sovereign.policy.tool_policy import policy                 # noqa: E402
from sovereign.runtime import residency, scheduler              # noqa: E402
from sovereign.tools import register_all                        # noqa: E402
from sovereign.tools.sandbox import egress_probe, run_code      # noqa: E402
from sovereign.workflows import register as register_runners    # noqa: E402

RESULTS: list[dict] = []
FAST = False


class Skip(Exception):
    pass


def check(cid: str, title: str, slow: bool = False):
    def deco(fn):
        def run():
            if FAST and slow:
                RESULTS.append({"id": cid, "title": title, "status": "SKIP",
                                "detail": "skipped by --fast"})
                return
            t0 = time.time()
            try:
                detail = fn()
                RESULTS.append({"id": cid, "title": title, "status": "PASS",
                                "detail": detail, "seconds": round(time.time() - t0, 1)})
            except Skip as exc:
                RESULTS.append({"id": cid, "title": title, "status": "SKIP",
                                "detail": str(exc)})
            except AssertionError as exc:
                RESULTS.append({"id": cid, "title": title, "status": "FAIL",
                                "detail": str(exc),
                                "seconds": round(time.time() - t0, 1)})
            except Exception:
                RESULTS.append({"id": cid, "title": title, "status": "FAIL",
                                "detail": "unhandled: "
                                          + traceback.format_exc()[-600:],
                                "seconds": round(time.time() - t0, 1)})
        run.cid = cid
        return run
    return deco


# --------------------------------------------------------------------- helpers

def wait_task(tid: str, timeout: float = 900.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = db.query_one("SELECT * FROM tasks WHERE id=?", (tid,))
        if row and row["state"] in ("COMPLETED", "FAILED", "TERMINATED", "REJECTED"):
            return dict(row)
        time.sleep(2)
    return dict(db.query_one("SELECT * FROM tasks WHERE id=?", (tid,)) or {})


_approver_stop = threading.Event()


def start_auto_approver() -> None:
    """Stand in for the operator so the approval gate is exercised, not bypassed."""
    def loop():
        while not _approver_stop.wait(1.0):
            for a in tool_policy.pending_approvals():
                tool_policy.decide_approval(a["id"], True, by="verification-operator")
    threading.Thread(target=loop, daemon=True).start()


def doc_id_for(fragment: str) -> str:
    row = db.query_one("SELECT id FROM documents WHERE title LIKE ?",
                       (f"%{fragment}%",))
    assert row, f"no indexed document matching {fragment!r}"
    return row["id"]


# ------------------------------------------------------------------ criteria

@check("A1", "Local inference: two or more model capabilities served")
def a1():
    cards = registry.all()
    assert len(cards) >= 2, f"only {len(cards)} models enabled"
    text = [c for c in cards if c.cap("text") >= 0.5]
    vision = [c for c in cards if c.cap("vision") >= 0.4]
    assert text, "no text-capable model is served"
    assert vision, "no vision-capable model is served"

    # gpt-oss is the model the earlier prototype could not use at all; prove it
    # returns real text through the harmony adapter.
    oss = registry.get("gpt-oss-20b")
    harmony_note = "gpt-oss not served on this host"
    if oss and oss.enabled:
        res = gateway.generate(GenRequest(
            messages=[ChatMessage("user", "Reply with the single word ACKNOWLEDGED.")],
            model="gpt-oss-20b", max_tokens=24, temperature=0.0,
            reasoning="low", timeout_s=600), allow_fallback=False)
        assert res.ok, f"gpt-oss generation failed: {res.error}"
        assert res.text.strip(), ("gpt-oss returned an empty completion — the "
                                  "harmony adapter is not working")
        harmony_note = (f"gpt-oss via harmony adapter returned "
                        f"{res.text.strip()[:40]!r} at {res.decode_tps:.1f} tok/s")
    return (f"{len(cards)} models enabled ({len(text)} text, {len(vision)} vision): "
            f"{', '.join(c.name for c in cards)}. {harmony_note}")


@check("A2", "Model selection differs across task types, with a recorded reason")
def a2():
    from sovereign import router
    cases = {
        "Check whether vessel V-204 exceeds its permitted operating pressure. Urgent.": None,
        "Fix this internal Python tool; the pytest suite fails": None,
        "Read this scanned handwritten field note": [{"kind": "image", "pages": 1}],
        "Summarise these inspection reports for the weekly digest": None,
    }
    chosen = {}
    for prompt, att in cases.items():
        cls = router.classify(prompt, attachments=att)
        d = router.select_model(cls, resident=[],
                                budget_for=residency.context_budget_tokens)
        assert d.model, f"no model selected for {cls.task_type}"
        assert d.reason and len(d.reason) > 30, "routing decision carries no reason"
        assert d.considered, "routing recorded no candidate comparison"
        chosen[cls.task_type] = d.model
    assert len(set(chosen.values())) >= 2, (
        f"every task type routed to the same model: {chosen}")
    return "; ".join(f"{k} -> {v}" for k, v in chosen.items())


@check("A3", "Agentic workflow: plan, call tools, observe, iterate", slow=True)
def a3():
    tid = scheduler.submit(
        title="Verification: engineering question",
        prompt=("What is the operating margin of vessel V-204, and does it satisfy "
                "clause 5.2 of SOP-MECH-014? Extract the values from inspection "
                "report IR-2026-0731 first."),
        workflow="engineering_qa", priority="HIGH")
    row = wait_task(tid)
    assert row.get("state") == "COMPLETED", (
        f"task ended {row.get('state')}: {row.get('state_reason')}")
    calls = db.query("SELECT tool FROM tool_calls WHERE task_id=?", (tid,))
    kinds = {r["tool"] for r in calls}
    assert len(calls) >= 2, f"only {len(calls)} tool calls made"
    assert len(kinds) >= 2, f"only one kind of tool used: {kinds}"
    events = db.query("SELECT kind FROM task_events WHERE task_id=?", (tid,))
    ek = {e["kind"] for e in events}
    assert "observation" in ek, "no observations recorded"
    globals()["_A3_TASK"] = tid
    return (f"{len(calls)} tool calls across {len(kinds)} tools ({', '.join(sorted(kinds))}), "
            f"{row.get('steps_used')} steps, {row.get('runtime_s', 0):.0f}s")


@check("A4", "Multimodal: a scanned document is processed locally")
def a4():
    did = doc_id_for("0731")
    doc = db.query_one("SELECT * FROM documents WHERE id=?", (did,))
    pages = db.query("SELECT page_no, extractor, ocr_conf FROM pages WHERE doc_id=?",
                     (did,))
    assert doc["status"] == "READY", f"document status {doc['status']}"
    extractors = {p["extractor"] for p in pages}
    assert "tesseract" in extractors or "vlm" in extractors, (
        f"the scanned report was not processed by OCR or a vision model: {extractors}")
    # Prove the source really had no text layer, so OCR was genuinely required.
    import fitz
    src = fitz.open(doc["path"])
    layer_chars = len(src[0].get_text("text").strip())
    src.close()
    assert layer_chars == 0, (f"the 'scanned' PDF has a {layer_chars}-character text "
                              f"layer, so OCR was not exercised")
    conf = sum(p["ocr_conf"] or 0 for p in pages) / max(1, len(pages))
    vals = extract.extract_document(did)
    assert vals["field_count"] >= 6, f"only {vals['field_count']} values extracted"
    for field, expected in (("design_pressure", "16"), ("operating_pressure", "12"),
                            ("nominal_thickness", "12.0"),
                            ("minimum_required_thickness", "7.5"),
                            ("previous_thickness", "10.4")):
        got = vals["fields"].get(field, {}).get("value")
        assert got == expected, f"{field} read as {got!r}, expected {expected!r}"
    survey = extract.extract_thickness_survey(did)
    assert survey["governing"]["reading_mm"] == 9.2, (
        f"governing reading {survey['governing']} != 9.2 mm")
    return (f"0-char text layer -> OCR at {conf:.0f}% mean confidence; "
            f"{vals['field_count']} labelled values and {survey['count']} thickness "
            f"readings extracted with page references; governing reading "
            f"{survey['governing']['reading_mm']} mm at "
            f"{survey['governing']['location']}")


@check("A5", "Knowledge grounding: local SOP retrieved and used")
def a5():
    hits = retrieve.search("remaining life below five years engineering review "
                           "committee referral", k=6)
    assert hits, "no passages retrieved"
    sops = [h for h in hits if h.doc_class == "sop"]
    assert sops, f"no SOP retrieved; got {[h.doc_title for h in hits]}"
    top = sops[0]
    assert "5.0 years" in top.text or "Remaining Life" in top.text or \
           "remaining life" in top.text.lower(), \
           f"retrieved SOP passage does not contain the threshold: {top.text[:200]}"
    # Hybrid retrieval must actually be hybrid. Dense search fails open to
    # lexical-only, which still returns plausible passages, so the degradation is
    # invisible unless it is asserted. It has silently regressed once already,
    # when the egress guard refused the embedder's hub metadata check.
    from sovereign.knowledge import embed
    st = embed.status()
    assert st["available"], f"dense retrieval is unavailable: {st['error']}"
    both = [h for h in hits if h.dense_score and h.lexical_score]
    assert both, ("no passage matched both semantically and lexically — retrieval "
                  "has degraded to a single strategy")
    return (f"{len(hits)} passages, {len(sops)} from SOPs; top: {top.citation()}; "
            f"{len(both)} matched both semantically and lexically, so hybrid "
            f"retrieval is genuinely active ({st['model']}, {st['dim']}-dim)")


@check("A6", "Provenance: important facts resolve to evidence classes")
def a6():
    ctx = provenance.EvidenceContext(
        passages=[{"doc_title": "IR-2026-0731", "page_no": 1, "chunk_id": "c1",
                   "text": "Design Pressure : 16 bar g  Operating Pressure : 12 bar g "
                           "Minimum Required Thick : 7.5 mm"}],
        calculations=[{"id": "CALC-T", "expression": "16 - 12", "result": 4.0,
                       "steps": {"inputs_verified": True}}])
    text = ("The design pressure is 16 bar and the operating pressure is 12 bar, a "
            "margin of 4 bar. The findings may indicate a remaining life of about "
            "6.1 years. The required corrosion allowance is 3.7 mm.")
    rep = provenance.classify_text(text, ctx, persist=False)
    c = rep.counts
    assert c["A"] >= 2, f"expected source facts, got {c}"
    assert c["B"] >= 1, f"expected a derived value, got {c}"
    assert c["C"] >= 1, f"expected an interpretation, got {c}"
    assert c["D"] >= 1, f"expected an unsupported claim, got {c}"
    return (f"A(source)={c['A']} B(derived)={c['B']} C(interpretation)={c['C']} "
            f"D(unsupported)={c['D']}; all four classes exercised")


@check("A7", "Calculation traces are reproducible, and inputs are themselves checked")
def a7():
    from sovereign.tools.calculator import evaluate
    tid = "verify-calc"
    for t in ("evidence", "calculations"):
        db.execute(f"DELETE FROM {t} WHERE task_id=?", (tid,))
    db.insert("evidence", {
        "id": db.new_id("ev"), "task_id": tid, "doc_id": "d", "page_no": 1,
        "snippet": "Nominal Thickness 12.0 mm  Thickness Recorded 10.4 mm  years 3",
        "score": 1.0, "created_at": db.now()})

    good = evaluate("(t_prev - t_cur) / years",
                    {"t_prev": 12.0, "t_cur": 10.4, "years": 3}, label="corrosion rate",
                    unit="mm/yr", task_id=tid,
                    input_provenance={"t_prev": "IR p.1", "t_cur": "IR p.1"})
    assert good["ok"] and good["inputs_verified"], f"verified calculation failed: {good}"
    assert good["steps"], "no intermediate steps recorded"
    assert abs(good["result"] - 0.533333333) < 1e-6, good["result"]

    # A calculation over an invented input must NOT confer Class B.
    bad = evaluate("(12.4 - 10.4) / 3", {}, label="hallucinated nominal",
                   unit="mm/yr", task_id=tid)
    assert bad["ok"], "the calculation itself should still evaluate"
    assert not bad["inputs_verified"], "an unestablished input was accepted as verified"
    assert bad["unverified_inputs"], "the offending input was not named"

    from sovereign.tools.calculator import task_calculations
    ctx = provenance.EvidenceContext(
        passages=[{"doc_title": "IR", "page_no": 1, "chunk_id": "c",
                   "text": "Nominal Thickness 12.0 mm Thickness Recorded 10.4 mm"}],
        calculations=task_calculations(tid))
    rep = provenance.classify_text("Rates of 0.533333 mm/yr and 0.666667 mm/yr.",
                                   ctx, persist=False)
    by_val = {v.mention.raw.split()[0]: v.ev_class for v in rep.verdicts}
    assert by_val.get("0.533333") == "B", f"verified result not Class B: {by_val}"
    assert by_val.get("0.666667") == "D", f"tainted result not Class D: {by_val}"

    # Rejects code execution dressed up as arithmetic.
    evil = evaluate('__import__("os").system("id")', {}, task_id=tid)
    assert not evil["ok"], "the calculator evaluated a non-arithmetic expression"
    return (f"{good['expression']} = {good['result']:.6f} via {good['steps']}; "
            f"a result computed from the unestablished input "
            f"{bad['unverified_inputs'][0]} classified D, not B; "
            f"code injection refused")


@check("A8", "Unsupported claims are refused rather than invented")
def a8():
    ctx = provenance.EvidenceContext(
        passages=[{"doc_title": "IR", "page_no": 1, "chunk_id": "c",
                   "text": "Operating Pressure : 12 bar g"}],
        calculations=[])
    text = "The required corrosion allowance is 3.7 mm."
    rep = provenance.classify_text(text, ctx, persist=False)
    redacted = provenance.redact_unsupported(text, rep)
    assert "CANNOT DETERMINE" in redacted, f"unsupported value not refused: {redacted}"
    assert not rep.ok, "report should not be clean"

    # And the enforcement path: the deliverable tool refuses outright.
    from sovereign.tools.base import ToolContext
    from sovereign.tools.docgen import _enforce_provenance
    ctxt = ToolContext(task_id="verify-refuse")
    refusal = _enforce_provenance([{"class": "D", "value": "3.7 mm",
                                    "context": "corrosion allowance"}], ctxt,
                                  kind="approval note")
    assert refusal is not None and not refusal.ok, "the document tool did not refuse"
    assert "3.7" in refusal.error, "the refusal does not name the offending value"
    return (f"redaction: {redacted.strip()}; the approval-note generator refuses "
            f"outright and names the offending value")


@check("A9", "Resource governance: workloads queue and prioritise under constraint",
       slow=True)
def a9():
    batch = scheduler.submit(
        title="Verification: batch summaries",
        prompt="Process all inspection reports and prepare summaries for the digest",
        workflow="batch_summarize", priority="BATCH")
    time.sleep(3)
    urgent = scheduler.submit(
        title="Verification: urgent pressure check",
        prompt=("Check whether vessel V-204 exceeds its permitted operating "
                "pressure. Urgent safety query."),
        workflow="engineering_qa", priority="CRITICAL")
    wait_task(urgent, timeout=900)
    wait_task(batch, timeout=900)

    b = db.query_one("SELECT * FROM tasks WHERE id=?", (batch,))
    u = db.query_one("SELECT * FROM tasks WHERE id=?", (urgent,))
    assert u["priority"] == "CRITICAL" and b["priority"] == "BATCH"
    snap = scheduler.snapshot()
    admissions = [a for a in snap["admissions"] if a["task_id"] in (batch, urgent)]
    assert admissions, "no admission decisions were recorded"
    assert all(a.get("reason") for a in admissions), "an admission carried no reason"
    globals()["_A9_BATCH"] = batch
    return (f"CRITICAL task {u['state']} in {u['runtime_s'] or 0:.0f}s on "
            f"{u['selected_model']}; BATCH task {b['state']} on {b['selected_model']}; "
            f"{len(admissions)} admission decisions recorded, each with a reason")


@check("A10", "Safe interruption: a workflow pauses at a checkpoint and resumes",
       slow=True)
def a10():
    """Pause and resume, tested deterministically.

    Racing a real batch job to pause it mid-flight is unreliable — a six-document
    batch on a warm model finishes before the pause lands, and the criterion then
    reports SKIP without having tested anything. So the machinery is driven
    directly with a runner that yields at a known barrier, which is the same
    `checkpoint_barrier` call every real workflow makes. The live batch path is
    then exercised opportunistically and reported.
    """
    marker = {"units_done": [], "resumed_from": None}

    def slow_units(task, ctl, resume):
        state = dict(resume or {})
        done = list(state.get("done", []))
        if done:
            marker["resumed_from"] = len(done)
        for i in range(1, 7):
            if i in done:
                continue
            ctl.step = i
            ctl.checkpoint_barrier({"done": done}, label=f"unit {i}")
            time.sleep(1.2)
            done.append(i)
            marker["units_done"] = list(done)
        return {"summary": f"completed {len(done)} units", "done": done}

    scheduler.register_runner("verify_preemption", slow_units)
    tid = scheduler.submit(title="Verification: preemptible units",
                           prompt="process units", workflow="verify_preemption",
                           priority="BATCH")

    deadline = time.time() + 180
    while time.time() < deadline:
        row = db.query_one("SELECT state FROM tasks WHERE id=?", (tid,))
        if row and row["state"] == "RUNNING" and marker["units_done"]:
            break
        time.sleep(0.4)
    assert marker["units_done"], "the task never began doing work"
    completed_before = len(marker["units_done"])

    assert scheduler.pause(tid, "verification preemption"), "pause was not accepted"
    deadline = time.time() + 120
    while time.time() < deadline:
        row = db.query_one("SELECT state FROM tasks WHERE id=?", (tid,))
        if row["state"] in ("PAUSED", "COMPLETED"):
            break
        time.sleep(0.3)
    row = db.query_one("SELECT state, state_reason FROM tasks WHERE id=?", (tid,))
    assert row["state"] == "PAUSED", f"task is {row['state']}, not PAUSED"

    from sovereign.runtime.control import latest_checkpoint
    snap = latest_checkpoint(tid)
    assert snap and snap["state"], "the checkpoint holds no resumable state"
    ck_count = len(db.query("SELECT id FROM checkpoints WHERE task_id=?", (tid,)))

    assert scheduler.resume(tid), "resume was not accepted"
    final = wait_task(tid, timeout=300)
    assert final["state"] == "COMPLETED", (
        f"the resumed task ended {final['state']}: {final['state_reason']}")
    assert marker["resumed_from"] is not None, (
        "the runner did not receive its checkpoint state on resume")
    assert marker["resumed_from"] >= completed_before, (
        f"work was redone: resumed with {marker['resumed_from']} units done but "
        f"{completed_before} were already complete")
    res = db.jload(final["result"], {}) or {}
    assert len(res.get("done", [])) == 6, f"units lost across the pause: {res}"

    # Now the live path, opportunistically: a real batch displaced by a CRITICAL task.
    live = "not exercised (the batch completed before preemption landed)"
    batch = scheduler.submit(
        title="Verification: live batch", prompt="Summarise all documents",
        workflow="batch_summarize", priority="BATCH")
    urgent = scheduler.submit(
        title="Verification: live urgent",
        prompt="Emergency: check whether V-204 exceeds its permitted pressure",
        workflow="engineering_qa", priority="CRITICAL")
    wait_task(urgent, timeout=600)
    b = db.query_one("SELECT state FROM tasks WHERE id=?", (batch,))
    if b and b["state"] == "PAUSED":
        scheduler.resume(batch)
    wait_task(batch, timeout=600)

    # Look at the trace, not the current state_reason: by the time the urgent
    # task finishes the batch has usually resumed and overwritten it.
    ev = db.query(
        "SELECT kind, label, detail FROM task_events WHERE task_id=? "
        "AND kind IN ('preempt_requested','paused','resumed') ORDER BY seq",
        (batch,))
    if ev:
        kinds = [e["kind"] for e in ev]
        detail = next((e["detail"] for e in ev if e["kind"] == "paused"), "")
        live = (f"the live batch was displaced by the CRITICAL task and resumed "
                f"({' -> '.join(kinds)}): {(detail or '')[:110]}")

    return (f"paused after {completed_before} of 6 units at checkpoint step "
            f"{snap['step']}, {ck_count} checkpoints written; resumed with "
            f"{marker['resumed_from']} units already done and completed all 6 "
            f"without redoing work. {live}")


@check("A11", "Sandbox: code executes with no network and no host access")
def a11():
    ok = run_code("print('sandbox ok')", task_id="verify-sandbox")
    assert ok["ok"], f"basic execution failed: {ok['stderr'][:300]}"
    assert ok["engine"] in ("bwrap", "unshare"), (
        f"sandbox is running in DEGRADED mode ({ok['engine']}) — no namespace "
        f"isolation is available on this host")

    shadow = run_code("print(open('/etc/shadow').read())", task_id="verify-sandbox")
    assert not shadow["ok"], "the sandbox read /etc/shadow"
    home = run_code("import os; print(os.listdir('/home'))", task_id="verify-sandbox")
    assert not home["ok"], "the sandbox listed /home"
    forks = run_code("import os\nwhile True: os.fork()", task_id="verify-sandbox")
    assert not forks["ok"], "a fork bomb was not contained"
    cpu = run_code("x=0\nwhile True: x+=1", task_id="verify-sandbox")
    assert not cpu["ok"], "a CPU bomb was not stopped"

    probe = egress_probe("verify-sandbox")
    assert probe["all_blocked"], f"the sandbox reached the network: {probe['leaked']}"
    return (f"engine {ok['engine']}, network: {ok['network']}; /etc/shadow and /home "
            f"absent; fork bomb contained; CPU bomb killed at the "
            f"{ok['limits']['cpu_s']}s limit; "
            f"{len(probe['attempts'])}/{len(probe['attempts'])} egress attempts blocked")


@check("A12", "Deliverable: a real Word document on the organisation's template")
def a12():
    from sovereign.deliverables import ApprovalNoteData, build_approval_note
    data = ApprovalNoteData(
        subject="Verification: continued operation of V-204",
        equipment_tag="V-204", equipment_desc="Overhead Reflux Accumulator",
        inspection_ref="IR-2026-0731", inspection_date="31 July 2026",
        background="Periodic in-service inspection under SOP-MECH-014 Rev 4.",
        sop_refs=["SOP-MECH-014 Rev 4"],
        observed_values=[{"parameter": "Design pressure", "value": "16",
                          "unit": "bar g", "source": "IR-2026-0731 p.1"},
                         {"parameter": "Operating pressure", "value": "12",
                          "unit": "bar g", "source": "IR-2026-0731 p.1"}],
        derived_values=[{"label": "Operating margin", "expression": "design - operating",
                         "substituted": "16 - 12", "result": 4, "unit": "bar"}],
        assessment=["The operating margin of 4 bar exceeds the 2.0 bar threshold."],
        compliance=[{"clause": "SOP-MECH-014 clause 5.2", "verdict": "COMPLIANT",
                     "detail": "Operating margin 4 bar exceeds 2.0 bar."}],
        unresolved=[{"item": "Coating specification", "needed": "PS-07"}],
        recommendation="Continued operation approved at 12 bar g.",
        provenance={"verdicts": [
            {"value": "16 bar", "class": "A", "rationale": "IR-2026-0731 p.1",
             "context": "Design Pressure"},
            {"value": "4 bar", "class": "B", "rationale": "CALC-T", "context": "margin"}]},
    )
    art = build_approval_note(data, task_id="verify-docx")
    p = Path(art["path"])
    assert p.exists() and art["bytes"] > 20000, f"document too small: {art}"

    from docx import Document
    doc = Document(str(p))
    text = "\n".join(par.text for par in doc.paragraphs)
    for needed in ("APPROVAL NOTE", "BHARAT REFINERIES", "COMPLIANCE STATEMENT",
                   "APPENDIX A"):
        assert needed.lower() in text.lower(), f"the document lacks a {needed} section"
    assert len(doc.tables) >= 4, f"only {len(doc.tables)} tables; the template needs " \
                                 f"particulars, observed, derived and evidence tables"
    assert doc.sections[0].footer.paragraphs[0].text.strip(), "no footer / retention mark"
    globals()["_A12_ART"] = art
    return (f"{art['name']}, {art['bytes']} bytes, {len(doc.tables)} tables, "
            f"sha256 {art['sha256'][:16]}; letterhead, numbered clauses, compliance "
            f"statement, signature block and evidence appendix all present")


@check("A13", "Sovereignty: outbound access is blocked, attributed and logged")
def a13():
    before = db.query_one("SELECT COUNT(*) n FROM network_events")["n"]
    egress.install_guard()
    egress.bind_task("verify-egress")

    import requests
    blocked = 0
    targets = ["https://api.openai.com/v1/chat/completions",
               "https://vendor-portal-sync.example.com/upload",
               "http://8.8.8.8:53"]
    for t in targets:
        try:
            requests.get(t, timeout=5)
        except Exception:
            blocked += 1
    assert blocked == len(targets), f"only {blocked}/{len(targets)} attempts refused"

    # Loopback to the local backend must still work, or the guard is useless.
    r = requests.get("http://127.0.0.1:11434/api/version", timeout=5)
    assert r.status_code == 200, "the guard also blocked the local inference backend"

    probe = egress_probe("verify-egress")
    assert probe["all_blocked"], f"the sandbox reached the network: {probe['leaked']}"

    events = db.rows_to_dicts(db.query(
        "SELECT * FROM network_events WHERE task_id='verify-egress' ORDER BY ts DESC"))
    after = db.query_one("SELECT COUNT(*) n FROM network_events")["n"]
    assert after > before, "no denial was recorded"
    named = [e for e in events if "example.com" in (e["destination"] or "")
             or "openai" in (e["destination"] or "")]
    assert named, "the denial log does not name the hostname that was attempted"
    layers = {e["layer"] for e in events}
    assert "app-guard" in layers and "sandbox-netns" in layers, (
        f"denials were recorded from only one layer: {layers}")

    nft = egress.nftables_status()
    return (f"{blocked} control-plane attempts refused and "
            f"{len(probe['attempts'])} sandbox attempts blocked; "
            f"{after - before} denial events recorded across layers {sorted(layers)}, "
            f"attributed to the task and naming the destination "
            f"({named[0]['destination']}); loopback to the inference backend still "
            f"works; host nftables table "
            f"{'loaded' if nft.get('loaded') else 'NOT loaded'}")


@check("A14", "Audit: the record is complete and tamper-evident")
def a14():
    v = audit.verify_chain()
    assert v["ok"], f"the audit chain does not verify: {v}"
    assert v["entries"] > 20, f"only {v['entries']} audit entries"
    cats = {r["category"] for r in db.query("SELECT DISTINCT category FROM audit_log")}
    for needed in ("task", "tool", "sovereignty", "runtime"):
        assert needed in cats, f"no audit entries in category {needed!r}"

    # Prove tamper-evidence by editing a row and re-verifying.
    row = db.query_one("SELECT seq, detail FROM audit_log ORDER BY seq LIMIT 1")
    original = row["detail"]
    db.execute("UPDATE audit_log SET detail='tampered' WHERE seq=?", (row["seq"],))
    broken = audit.verify_chain()
    db.execute("UPDATE audit_log SET detail=? WHERE seq=?", (original, row["seq"]))
    restored = audit.verify_chain()
    assert not broken["ok"], "editing an audit row did NOT break verification"
    assert restored["ok"], "the chain did not verify after the row was restored"
    return (f"{v['entries']} entries across {len(cats)} categories verify; editing "
            f"entry {row['seq']} broke verification at {broken['broken_at']} "
            f"({broken['reason']}) and restoring it repaired the chain")


@check("A15", "Backend independence: a new backend is configuration, not code")
def a15():
    from sovereign.gateway.base import ModelBackend
    from sovereign.gateway.openai_backend import OpenAICompatBackend
    from sovereign.gateway.ollama_backend import OllamaBackend
    for cls in (OllamaBackend, OpenAICompatBackend):
        assert issubclass(cls, ModelBackend)
        for m in ("health", "list_models", "generate"):
            assert hasattr(cls, m), f"{cls.__name__} lacks {m}"

    # Register a model on a different backend at runtime and confirm the router
    # will consider it without any code change.
    from sovereign.gateway.registry import ModelCard
    probe = ModelCard(
        name="verify-external", backend="openai_compat", backend_ref="some/model",
        role="generalist", prompt_adapter="chat", ctx_max=8192, weights_mb=4000,
        est_vram_mb=4000, kv_mb_per_1k=50.0,
        caps={"text": 1.0, "reasoning": 0.7, "coding": 0.7, "extraction": 0.7,
              "tool_use": 0.7, "speed": 0.7, "structured": 0.7},
        notes="registered by the verification suite")
    db.upsert("model_registry", probe.to_row(), key="name")
    registry.invalidate()
    try:
        card = registry.get("verify-external")
        assert card and card.backend == "openai_compat"
        from sovereign import router
        cls = router.classify("Summarise this document")
        d = router.select_model(cls, resident=[])
        considered = {c["model"] for c in d.considered}
        assert "verify-external" in considered, (
            "a newly registered backend model was not considered by the router")
    finally:
        db.execute("DELETE FROM model_registry WHERE name='verify-external'")
        registry.invalidate()
    adapters = {c.prompt_adapter for c in registry.all()}
    return (f"backends implement a common contract; a model registered on the "
            f"openai_compat backend at runtime was considered by the router without "
            f"a code change; prompt adapters in use: {sorted(adapters)}")


@check("A16", "Engineering drawings: symbols, tags and connectivity with confidence")
def a16():
    from sovereign.drawings import analyze
    truth_path = ROOT / "corpus/drawings/PID-204-01.truth.json"
    pdf = ROOT / "corpus/drawings/PID-204-01.pdf"
    assert pdf.exists(), "the corpus P&ID is missing"
    truth = json.loads(truth_path.read_text())

    a = analyze.analyse_pdf(pdf, title="PID-204-01", use_vlm=False)
    score = analyze.score_against_truth(a, truth)
    s = a.summary()
    assert s["source_kind"] == "vector", "the vector path was not taken"
    assert score["tags"]["f1"] >= 0.9, f"tag extraction F1 {score['tags']['f1']}"
    assert score["connectivity"]["f1"] >= 0.9, (
        f"connectivity F1 {score['connectivity']['f1']}: "
        f"missed {score['connectivity']['missed']}, "
        f"spurious {score['connectivity']['spurious']}")

    # Honesty properties: not everything is asserted as fact.
    statuses = {e.status for e in a.edges}
    assert "CONFIRMED" in statuses, "nothing was confirmed"
    assert a.warnings or "UNRESOLVED" in statuses, (
        "the analysis claims complete certainty, which it should not")
    # Instrument leads must not be reported as process connections.
    sig = [e for e in a.edges if e.line_type == "instrument_signal"]
    assert sig, "instrument signal leads were not distinguished from process lines"

    from sovereign.drawings import graph as dgraph

    # A real multi-hop path must be traceable and reported as confirmed.
    real = dgraph.trace_path(a.symbols, a.edges, "C-201", "P-101B")
    assert real["found"] and real["status"] == "CONFIRMED", (
        f"a genuine process path was not traced: {real}")
    assert "E-301" in real["path"] and "V-204" in real["path"], (
        f"the traced path skips equipment that lies on it: {real['path']}")

    # Equipment that is not on this sheet must produce a refusal, not a guess.
    for missing in ("P-999", "V-100"):
        absent = dgraph.trace_path(a.symbols, a.edges, "C-201", missing)
        assert not absent["found"], f"a path to absent equipment {missing} was asserted"
        assert absent["status"] == "CANNOT DETERMINE", absent
        assert missing in absent["reason"], "the refusal does not say what is missing"

    raster = analyze.analyse_image(ROOT / "corpus/drawings/PID-204-01-scan.png",
                                   title="PID-204-01 scan", use_vlm=False)
    rs = raster.summary()
    assert rs["confirmed_connectivity"] == 0, (
        "the raster path asserted CONFIRMED connectivity, which it must not")
    assert any("raster" in w for w in rs["warnings"]), (
        "the raster analysis does not warn about its own reliability")
    return (f"vector: {s['symbols']} symbols ({s['symbols_tagged']} tagged), "
            f"traced C-201 -> {' -> '.join(real['path'][1:])} as CONFIRMED, "
            f"refused a path to absent equipment; "
            f"tags F1 {score['tags']['f1']:.2f}, connectivity F1 "
            f"{score['connectivity']['f1']:.2f}, statuses {sorted(statuses)}, "
            f"{len(sig)} instrument leads separated from process flow; "
            f"raster: {rs['symbols']} symbols but 0 CONFIRMED connections and an "
            f"explicit reliability warning")


@check("A17", "Residency-aware scheduling beats naive per-task routing")
def a17():
    import subprocess
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts/benchmark_scheduling.py"),
         "--dry-run", "--json"],
        capture_output=True, text=True, timeout=600, cwd=str(ROOT))
    assert r.returncode == 0, f"benchmark failed: {r.stderr[-400:]}"
    payload = json.loads(r.stdout[r.stdout.index("{"):])
    d = payload["delta"]
    assert d["loads_avoided"] > 0, (
        f"residency scheduling avoided no model loads: {payload}")
    assert d["capability_fit_delta"] >= -0.05, (
        f"residency scheduling gave up too much capability: "
        f"{d['capability_fit_delta']}")
    return (f"naive: {payload['naive']['model_loads']} model loads, "
            f"{payload['naive']['load_seconds']}s loading. "
            f"residency-aware: {payload['residency_aware']['model_loads']} loads, "
            f"{payload['residency_aware']['load_seconds']}s. "
            f"{d['loads_avoided']} loads and {d['load_seconds_saved']}s avoided at a "
            f"capability-fit change of {d['capability_fit_delta']:+.4f}")


@check("A18", "Untrusted documents cannot issue instructions")
def a18():
    row = db.query_one("SELECT id, title FROM documents WHERE title LIKE '%MAT-007%'")
    assert row, "the injection-carrying SOP is not indexed"
    page = db.query_one("SELECT text FROM pages WHERE doc_id=? AND page_no=1",
                        (row["id"],))
    scan = injection.scan(page["text"], source=row["title"])
    assert scan.detected, "the embedded injection was not detected"
    assert scan.max_severity == "high", f"severity {scan.max_severity}"
    kinds = {f.kind for f in scan.findings}

    wrapped = injection.wrap_untrusted(page["text"], label=row["title"])
    assert injection.UNTRUSTED_OPEN in wrapped, "content is not framed as untrusted"
    neutralised = injection.neutralise(page["text"])
    assert "NEUTRALISED" in neutralised, "high-severity constructs were not defanged"

    # The decisive property: the document names an external host, and reaching it
    # is refused by policy regardless of anything the document says.
    ok, reason = egress.is_allowed("vendor-portal-sync.example.com", 443)
    assert not ok, "the host named in the injected instruction is reachable"
    return (f"detected {sorted(kinds)} at {scan.max_severity} severity in "
            f"{row['title']}; content is delimiter-framed as data; the exfiltration "
            f"host it names is refused by policy ({reason[:60]}…)")


@check("A19", "Tool policy gates sensitive actions by organisational posture")
def a19():
    from sovereign.policy.tool_policy import Risk
    original = policy.mode
    try:
        outcomes = {}
        for mode in ("standard", "controlled", "strict"):
            policy.set_mode(mode)
            outcomes[mode] = {
                "read": policy.evaluate("calculator", Risk.READ_ONLY, {}).requires_approval,
                "write": policy.evaluate("write_file", Risk.LOCAL_WRITE, {}).requires_approval,
                "docgen": policy.evaluate("generate_approval_note", Risk.DELIVERABLE,
                                          {}).requires_approval,
            }
        assert outcomes["standard"]["docgen"] is False
        assert outcomes["controlled"]["docgen"] is True
        assert outcomes["strict"]["write"] is True
        assert outcomes["standard"]["read"] is False

        # A tool outside the task's granted set is refused outright.
        policy.set_mode("controlled")
        d = policy.evaluate("run_code", Risk.COMPUTE, {},
                            allowed_tools={"calculator"})
        assert not d.allowed, "a tool outside the granted set was permitted"
    finally:
        policy.set_mode(original)
    return (f"standard: deliverables auto-execute; controlled: deliverables need "
            f"approval; strict: even a workspace write needs approval; a tool "
            f"outside the task's granted set is refused with a reason")


@check("A20", "The control plane recovers tasks orphaned by a restart")
def a20():
    tid = db.new_id("task")
    db.insert("tasks", {
        "id": tid, "title": "Verification: orphan", "prompt": "orphan",
        "owner": "operator", "workflow": "general", "task_type": "general",
        "priority": "LOW", "state": "RUNNING", "created_at": db.now()})
    db.insert("checkpoints", {
        "id": db.new_id("ckpt"), "task_id": tid, "step": 3,
        "reason": "verification", "state_blob": db.jdump({"done": {"a": 1}}),
        "ts": db.now()})

    orphan2 = db.new_id("task")
    db.insert("tasks", {
        "id": orphan2, "title": "Verification: orphan without checkpoint",
        "prompt": "orphan", "owner": "operator", "workflow": "general",
        "task_type": "general", "priority": "LOW", "state": "RUNNING",
        "created_at": db.now()})

    scheduler._recover_orphans()
    a = db.query_one("SELECT state, state_reason FROM tasks WHERE id=?", (tid,))
    b = db.query_one("SELECT state FROM tasks WHERE id=?", (orphan2,))
    assert a["state"] == "PAUSED", f"a checkpointed orphan became {a['state']}"
    assert b["state"] == "FAILED", f"an uncheckpointed orphan became {b['state']}"
    db.execute("DELETE FROM tasks WHERE id IN (?,?)", (tid, orphan2))
    return ("a task orphaned with a checkpoint is recovered as PAUSED and remains "
            "resumable; one without a checkpoint is failed explicitly rather than "
            "silently holding a quota slot")


@check("A21", "Handwriting and photographs go through the vision tier", slow=True)
def a21():
    """The problem statement names handwritten notes and photographs explicitly.

    The interesting property is not that OCR runs — it is that OCR is *not
    believed* on these inputs. Tesseract reports 68% mean confidence on the
    corpus field note while turning the thickness readings into nonsense, so
    accepting it would put confidently wrong numbers into the evidence store.
    """
    from sovereign.gateway.registry import registry
    from sovereign.knowledge import ocr

    if not registry.vision_models():
        raise Skip("no vision model is served on this host")

    note = ROOT / "corpus/photos/field-note-V-204-handwritten.jpg"
    photo = ROOT / "corpus/photos/nameplate-V-204-photo.jpg"
    assert note.exists() and photo.exists(), "the corpus photographs are missing"

    # OCR alone must fail the bar on both, or the test proves nothing.
    _t1, _w1, c1 = ocr._tesseract_tsv(note)
    _t2, _w2, c2 = ocr._tesseract_tsv(photo)
    assert c1 < ocr.MIN_TESS_CONF_IMAGE, (
        f"OCR confidence {c1:.0f} on handwriting clears the image bar; this test "
        f"would not exercise the vision tier")

    # Ingestion deduplicates on content hash, so a previous run would make this
    # a no-op and the criterion would report on nothing. Purge first.
    for f in (note, photo):
        digest = ingest.sha256_file(f)
        for row in db.query("SELECT id FROM documents WHERE sha256=?", (digest,)):
            for table in ("chunks", "pages"):
                db.execute(f"DELETE FROM {table} WHERE doc_id=?", (row["id"],))
            db.execute("DELETE FROM documents WHERE id=?", (row["id"],))

    hand = ingest.ingest_file(note, title="Field note V-204 (handwritten)",
                              doc_class="inspection_report", use_vlm=True)
    plate = ingest.ingest_file(photo, title="Nameplate V-204 (photograph)",
                               doc_class="specification", use_vlm=True)
    for r, label in ((hand, "handwritten note"), (plate, "photograph")):
        assert not r.get("reused"), f"the {label} was not re-ingested"

    for r, label in ((hand, "handwritten note"), (plate, "photograph")):
        assert r["status"] == "READY", f"{label} was not extracted: {r}"
        assert "vlm" in r["extractors"], (
            f"the {label} was accepted from OCR ({r['extractors']}) instead of "
            f"being routed to the vision model")

    hand_text = db.query_one("SELECT text FROM pages WHERE doc_id=?",
                             (hand["doc_id"],))["text"]
    plate_text = db.query_one("SELECT text FROM pages WHERE doc_id=?",
                              (plate["doc_id"],))["text"]
    assert "V-204" in hand_text.upper() or "V 204" in hand_text.upper(), (
        f"the vision model did not recover the equipment tag: {hand_text[:200]}")
    assert "V-204" in plate_text.upper() or "V 204" in plate_text.upper(), (
        f"the nameplate tag was not recovered: {plate_text[:200]}")

    return (f"OCR scored {c1:.0f}% on the handwriting and {c2:.0f}% on the "
            f"photograph, both below the {ocr.MIN_TESS_CONF_IMAGE:.0f}% bar for "
            f"images, so both were routed to the local vision model; the "
            f"equipment tag V-204 was recovered from each")


CHECKS = [a1, a2, a3, a4, a5, a6, a7, a8, a9, a10, a11, a12, a13, a14, a15,
          a16, a17, a18, a19, a20, a21]


# ------------------------------------------------------------------------ main

def bootstrap(reset: bool) -> None:
    if reset:
        for p in (DATA_DIR / "db", DATA_DIR / "evidence", DATA_DIR / "workspaces",
                  DATA_DIR / "checkpoints"):
            shutil.rmtree(p, ignore_errors=True)
            p.mkdir(parents=True, exist_ok=True)
    db.init_db()
    egress.install_guard()
    register_all()
    register_runners(scheduler)
    gateway.sync_registry()
    hardware.probe()
    if not hardware.sampler.is_alive():
        hardware.sampler.start()

    if not db.query_one("SELECT COUNT(*) n FROM documents")["n"]:
        print("Ingesting the corpus …")
        for sub in ("sops", "reports"):
            ingest.ingest_directory(ROOT / "corpus" / sub, use_vlm=False)
    scheduler.start()
    start_auto_approver()


def main() -> int:
    global FAST
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true",
                    help="skip criteria that run full agent trajectories")
    ap.add_argument("--only", default="", help="comma-separated criterion ids")
    ap.add_argument("--reset", action="store_true", help="wipe state and re-ingest")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    FAST = args.fast

    bootstrap(args.reset)
    wanted = {x.strip().upper() for x in args.only.split(",") if x.strip()}

    print("=" * 78)
    print(" SOVEREIGN WORKBENCH — ACCEPTANCE VERIFICATION")
    gpu = hardware.gpu_state()
    print(f" {gpu.name} · {gpu.total_mb:.0f} MB VRAM · "
          f"{hardware.mem_state().total_mb / 1024:.1f} GB RAM")
    print(f" models: {', '.join(c.name for c in registry.all())}")
    print("=" * 78)

    t0 = time.time()
    for fn in CHECKS:
        if wanted and fn.cid not in wanted:
            continue
        print(f"  {fn.cid} …", end=" ", flush=True)
        fn()
        r = RESULTS[-1]
        mark = {"PASS": "PASS", "FAIL": "FAIL", "SKIP": "skip"}[r["status"]]
        print(f"{mark}  ({r.get('seconds', 0)}s)")

    _approver_stop.set()
    scheduler.stop()

    print()
    print("=" * 78)
    for r in RESULTS:
        print(f"[{r['status']:4s}] {r['id']}  {r['title']}")
        for line in str(r["detail"]).split("\n"):
            print(f"         {line}")
    print("=" * 78)
    passed = sum(1 for r in RESULTS if r["status"] == "PASS")
    failed = sum(1 for r in RESULTS if r["status"] == "FAIL")
    skipped = sum(1 for r in RESULTS if r["status"] == "SKIP")
    print(f" {passed} passed · {failed} failed · {skipped} skipped "
          f"· {time.time() - t0:.0f}s")
    print("=" * 78)

    db.insert("benchmarks", {
        "id": db.new_id("verify"), "name": "acceptance",
        "variant": "fast" if FAST else "full",
        "payload": db.jdump({"results": RESULTS, "passed": passed,
                             "failed": failed, "skipped": skipped}),
        "ts": time.time()})
    if args.json:
        print(json.dumps(RESULTS, indent=2, default=str))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
