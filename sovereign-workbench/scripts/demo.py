#!/usr/bin/env python3
"""The coherent industrial demonstration, run end to end.

One story rather than a collection of disconnected features, following the
sequence the architecture specifies:

  Act 1  a low-priority batch of confidential work begins
  Act 2  an urgent engineering question arrives and displaces it
  Act 3  the urgent answer resolves to evidence and a reproducible calculation
  Act 4  an approval note is produced on the organisation's template
  Act 5  the batch resumes from its checkpoint, without redoing work
  Act 6  the sovereign boundary is attacked and the refusal is recorded

Run with the workbench stopped; this drives the control plane directly so the
narration lines up with what is happening.

    python3 scripts/demo.py
    python3 scripts/demo.py --act 6      # just the sovereignty proof
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from sovereign import audit, db, hardware                      # noqa: E402
from sovereign.gateway import gateway                          # noqa: E402
from sovereign.knowledge import ingest                         # noqa: E402
from sovereign.policy import egress, tool_policy               # noqa: E402
from sovereign.runtime import residency, scheduler             # noqa: E402
from sovereign.tools import register_all                       # noqa: E402
from sovereign.workflows import register as register_runners   # noqa: E402

BOLD, DIM, OFF = "\033[1m", "\033[2m", "\033[0m"


def act(n: int, title: str) -> None:
    print(f"\n{BOLD}── ACT {n} · {title} {'─' * max(0, 56 - len(title))}{OFF}")


def note(msg: str) -> None:
    print(f"   {DIM}{msg}{OFF}")


def follow(task_id: str, since: int = 0, timeout: float = 900.0) -> int:
    """Print an agent trace as it happens."""
    deadline = time.time() + timeout
    seq = since
    while time.time() < deadline:
        for e in db.query("SELECT seq,kind,label,detail FROM task_events "
                          "WHERE task_id=? AND seq>? ORDER BY seq", (task_id, seq)):
            seq = e["seq"]
            detail = (e["detail"] or "").replace("\n", " ")[:96]
            print(f"   {e['kind']:<18} {e['label'][:46]:<48} {DIM}{detail}{OFF}")
        row = db.query_one("SELECT state FROM tasks WHERE id=?", (task_id,))
        if row and row["state"] in ("COMPLETED", "FAILED", "TERMINATED", "REJECTED",
                                    "PAUSED"):
            return seq
        time.sleep(1.5)
    return seq


def approver() -> threading.Event:
    stop = threading.Event()

    def loop():
        while not stop.wait(1.0):
            for a in tool_policy.pending_approvals():
                print(f"   {BOLD}[operator]{OFF} approving {a['tool']}")
                tool_policy.decide_approval(a["id"], True, by="demo-operator")
    threading.Thread(target=loop, daemon=True).start()
    return stop


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--act", type=int, default=0, help="run a single act")
    args = ap.parse_args()

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
    stop = approver()

    gpu = hardware.gpu_state()
    limit, why = residency.concurrency_limit()
    print(f"{BOLD}Sovereign On-Premise AI Workbench — demonstration{OFF}")
    print(f"   {gpu.name}, {gpu.total_mb:.0f} MB VRAM")
    print(f"   {why}")

    only = args.act
    batch = None

    if only in (0, 1, 5):
        act(1, "confidential batch work begins")
        batch = scheduler.submit(
            title="Weekly inspection digest",
            prompt="Process all inspection reports and procedures and prepare "
                   "summaries for the weekly digest",
            workflow="batch_summarize", priority="BATCH")
        note("a LOW/BATCH priority agent takes the single available slot")
        time.sleep(14)
        follow(batch, timeout=25)

    if only in (0, 2, 3, 4):
        act(2, "an urgent engineering question arrives")
        urgent = scheduler.submit(
            title="V-204 pressure check",
            prompt=("Urgent: check whether vessel V-204 exceeds its permitted "
                    "operating pressure. Extract the values from inspection report "
                    "IR-2026-0731, compute the operating margin, and state the "
                    "verdict against clause 5.2 of SOP-MECH-014."),
            workflow="engineering_qa", priority="CRITICAL")
        note("classified CRITICAL; it outranks the batch, which is asked to "
             "checkpoint and yield")
        follow(urgent)

        act(3, "the answer resolves to evidence")
        row = db.query_one("SELECT result FROM tasks WHERE id=?", (urgent,))
        res = db.jload(row["result"], {}) if row else {}
        print(f"\n{res.get('summary', '(no answer)')[:1400]}\n")
        counts = (res.get("provenance") or {}).get("counts", {})
        note(f"evidence classes — A(source) {counts.get('A', 0)}, "
             f"B(derived) {counts.get('B', 0)}, C(interpretation) {counts.get('C', 0)}, "
             f"D(unsupported, refused) {counts.get('D', 0)}")
        for c in res.get("calculations", []):
            sub = (c.get("steps") or {}).get("substituted", "")
            note(f"{c['id']}: {c.get('label')} = {sub} = {c.get('result')} "
                 f"{c.get('unit', '')}")

    if only in (0, 4):
        act(4, "a deliverable on the organisation's template")
        note_task = scheduler.submit(
            title="V-204 approval note",
            prompt=("Prepare an approval note for pressure vessel V-204 from "
                    "inspection report IR-2026-0731 and SOP-MECH-014. Extract the "
                    "values first, compute the operating margin, the corrosion rate "
                    "and the remaining life, and state compliance for each clause."),
            workflow="inspection_to_approval", priority="HIGH")
        follow(note_task)
        for a in db.query("SELECT name, kind, bytes FROM artifacts WHERE task_id=?",
                          (note_task,)):
            note(f"deliverable: {a['name']} ({a['kind']}, {a['bytes']} bytes)")

    if only in (0, 5) and batch:
        act(5, "the batch resumes from its checkpoint")
        row = db.query_one("SELECT state FROM tasks WHERE id=?", (batch,))
        if row and row["state"] == "PAUSED":
            ck = db.query_one("SELECT step FROM checkpoints WHERE task_id=? "
                              "ORDER BY step DESC LIMIT 1", (batch,))
            note(f"resuming from checkpoint step {ck['step'] if ck else '?'} — "
                 f"work already done is not repeated")
            scheduler.resume(batch)
        follow(batch)
        row = db.query_one("SELECT result FROM tasks WHERE id=?", (batch,))
        res = db.jload(row["result"], {}) if row else {}
        note(f"batch complete: {res.get('summary', '')}")

    if only in (0, 6):
        act(6, "the sovereign boundary is attacked")
        proof = scheduler.submit(
            title="Sovereignty self-test",
            prompt="Attempt outbound network access from every layer",
            workflow="sovereignty_proof", priority="HIGH")
        follow(proof)
        row = db.query_one("SELECT result FROM tasks WHERE id=?", (proof,))
        res = db.jload(row["result"], {}) if row else {}
        print()
        print(f"   {res.get('summary', '')}")
        print()
        for e in db.query("SELECT destination, port, layer, result FROM "
                          "network_events ORDER BY ts DESC LIMIT 8"):
            print(f"   {e['result']:<8} {str(e['destination'])[:38]:<40} "
                  f":{e['port'] or '':<6} {e['layer']}")
        chain = audit.verify_chain()
        note(f"audit chain: {'VERIFIED' if chain['ok'] else 'BROKEN'} over "
             f"{chain['entries']} entries — the denial record is tamper-evident")

    stop.set()
    scheduler.stop()
    print(f"\n{BOLD}Demonstration complete.{OFF} "
          f"Open http://127.0.0.1:8794 to inspect every step.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
