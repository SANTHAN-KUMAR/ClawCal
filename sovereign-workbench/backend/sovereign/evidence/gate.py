"""B3 — the provenance gate, checking against *served* spans (spec §13).

A client harness drafts a deliverable on a laptop and submits claims with the
span ids it cites. The gate, on the node, holds each claim to one rule:

    every number, equipment tag and date must resolve to a span id IN THE
    SERVED SET for this session, and the value must appear in that span's
    text; otherwise it is STRIPPED and reported.

Two refinements keep the rule strict without making it brittle:

* a value found in a served span the claim did not cite is kept, and the
  citation is corrected to the span that holds it (reported as `recited`) —
  the document then cites the right page;
* a number the node's calculator produced in this session, from inputs that
  were themselves established (D-07), is kept as DERIVED.

The gate is ordinary code, not a model; that is why it can be trusted, and why
it can sit on the node while the loop sits on a laptop.
"""
from __future__ import annotations

from typing import Any

from .. import audit, db
from . import provenance, served
from .gatecore import (STRIPPED_MARK, ClaimResult, Value,  # noqa: F401
                       evaluate, values_in)


def check(claims: list[dict[str, Any]], *, session_id: str | None,
          task_id: str | None = None, persist: bool = True) -> dict[str, Any]:
    """Gate a list of claims `{"text": ..., "spans": [span ids]}`."""
    idx = served.index(session_id=session_id, task_id=task_id)
    claims_out, counts = evaluate(claims, idx, _calculations(session_id, task_id))
    report = {"ok": counts["stripped"] == 0, "counts": counts,
              "served_spans": len(idx), "session_id": session_id,
              "claims": claims_out}
    if persist and task_id:
        _persist(task_id, claims_out)
        from ..control import decisions
        decisions.record(
            "evidence", "GATE_PASSED" if report["ok"] else "GATE_STRIPPED",
            f"{counts['kept']} value(s) resolved to served spans or verified "
            f"calculations ({counts['recited']} re-cited, {counts['derived']} "
            f"derived); {counts['stripped']} stripped; {len(idx)} span(s) served "
            f"to this session", subject_kind="gate", subject_id=task_id,
            task_id=task_id, session_id=session_id, basis=counts)
        audit.record("evidence", "gate", task_id=task_id,
                     outcome="PASS" if report["ok"] else "STRIPPED", detail=counts)
    return report


def _calculations(session_id: str | None, task_id: str | None) -> dict[str, Any]:
    """Verified calculator results in this session, by normalised value."""
    from ..tools.calculator import task_calculations
    if session_id:
        tids = [r["id"] for r in db.query(
            "SELECT id FROM tasks WHERE conversation_id=?", (session_id,))]
    else:
        tids = [task_id] if task_id else []
    ctx = provenance.EvidenceContext(
        calculations=[c for t in tids for c in task_calculations(t)])
    return {k: v[0] for k, v in ctx.calc_index().items()}


def _persist(task_id: str, results: list[dict[str, Any]]) -> None:
    for r in results:
        for k in r["kept"]:
            provenance.record_claim(
                task_id, entity="", attribute=k["kind"], value=k["value"],
                statement=r["text_in"][:600], ev_class=k["class"],
                evidence_ids=[k["span_id"]] if k.get("span_id") else [],
                calc_id=k.get("calc_id"),
                rationale=(f"resolved to served span {k['span_id']} "
                           f"({k.get('doc_title')} p.{k.get('page_no')}; {k['basis']})"
                           if k.get("span_id") else
                           f"derived by calculation {k.get('calc_id')}"))
        for s in r["stripped"]:
            provenance.record_claim(task_id, entity="", attribute=s["kind"],
                                    value=s["value"], statement=r["text_in"][:600],
                                    ev_class="D", rationale=f"stripped: {s['reason']}")
