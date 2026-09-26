"""The v2 control plane: identity, decisions, per-session policy, contract, basis.

These hold the gates of ARCHITECTURE-v2 §8 (B1, B2, B5) as tests:

* every authority writes a decision row, and the record is tamper-evident;
* the API layer reaches the authorities only through `sovereign.control`;
* permission modes are per session and behave as the §5.2 table says;
* every tool result carries one of the four outcomes;
* a performance figure always carries its basis, and a bad row is ignored.
"""
from __future__ import annotations

import re
import time
from pathlib import Path

import pytest

from sovereign import audit, control, db, outcomes
from sovereign.control import decisions, identity, sessions
from sovereign.policy.tool_policy import Risk, policy

ROOT = Path(__file__).resolve().parents[1]


def _task(owner: str = "alice", session: str | None = None, mode: str = "review"):
    tid = db.new_id("task")
    db.insert("tasks", {"id": tid, "conversation_id": session or tid, "title": "t",
                        "prompt": "p", "owner": owner, "state": "RUNNING",
                        "policy_mode": mode, "created_at": time.time()})
    return tid


@pytest.fixture()
def alice():
    identity.create_principal("alice", role="engineer", issue_token=False)
    return identity.LocalSource().lookup("alice")


# ------------------------------------------------------------------ boundary

class TestBoundary:
    FORBIDDEN = re.compile(r"^\s*(from|import)\s+(\.\.|sovereign\.)"
                           r"(runtime|router|policy|evidence)\b", re.M)

    @pytest.mark.parametrize("path", [
        "backend/sovereign/api/app.py", "backend/sovereign/server.py",
        "clients/clawcal/cli.py"])
    def test_clients_and_api_reach_authorities_only_through_the_facade(self, path):
        p = ROOT / path
        if not p.exists():
            pytest.skip(f"{path} not present")
        hits = self.FORBIDDEN.findall(p.read_text())
        assert not hits, f"{path} imports an authority directly: {hits}"

    def test_the_facade_exposes_all_seven_authorities(self):
        for name in control.AUTHORITIES:
            assert getattr(control, name) is not None

    def test_no_placeholder_actor_remains_in_code(self):
        offenders = []
        for p in (ROOT / "backend" / "sovereign").rglob("*.py"):
            for i, line in enumerate(p.read_text().splitlines(), 1):
                if re.search(r"""["']operator["']""", line):
                    offenders.append(f"{p.name}:{i}")
        assert not offenders, offenders


# ------------------------------------------------------------------ decisions

class TestDecisions:
    def test_every_authority_can_record_and_unknown_is_refused(self):
        for a in decisions.AUTHORITIES:
            decisions.record(a, "TEST", "unit", subject_kind="t", subject_id="x")
        with pytest.raises(ValueError):
            decisions.record("astrology", "TEST")
        assert decisions.verify()["ok"]

    def test_editing_a_decision_breaks_the_chain(self):
        did = decisions.record("routing", "m1", "reason")
        db.execute("UPDATE decisions SET reason='rewritten' WHERE id=?", (did,))
        v = decisions.verify()
        assert not v["ok"] and "edited" in v["reason"]
        db.execute("UPDATE decisions SET reason='reason' WHERE id=?", (did,))
        assert decisions.verify()["ok"]

    def test_truncating_decisions_is_caught_by_the_audit_cross_reference(self):
        decisions.record("admission", "ADMIT", "a")
        last = db.query_one("SELECT * FROM decisions ORDER BY seq DESC LIMIT 1")
        saved = dict(last)
        db.execute("DELETE FROM decisions WHERE seq=?", (last["seq"],))
        v = decisions.verify()
        assert not v["ok"] and "missing" in v["reason"]
        db.insert("decisions", saved)
        assert decisions.verify()["ok"]

    def test_policy_evaluations_and_approvals_are_decisions(self, alice, task_id):
        from sovereign.tools import register_all
        from sovereign.tools.base import ToolContext
        tid = _task(owner="alice", mode="trusted")
        register_all().invoke("calculator", {"expression": "1+1"},
                              ToolContext(task_id=tid, principal="alice"))
        rows = decisions.for_task(tid)
        assert any(r["authority"] == "tool_policy" and r["outcome"] == "ALLOW"
                   and r["principal"] == "alice" for r in rows)


# --------------------------------------------------------------- permission modes

class TestPermissionModes:
    @pytest.mark.parametrize("mode,risk,allowed,ask", [
        ("review", Risk.READ_ONLY, True, False),
        ("review", Risk.LOCAL_WRITE, True, True),
        ("review", Risk.COMPUTE, True, True),
        ("review", Risk.DELIVERABLE, True, True),
        ("trusted", Risk.LOCAL_WRITE, True, False),
        ("trusted", Risk.COMPUTE, True, False),
        ("trusted", Risk.DELIVERABLE, True, False),
        ("trusted", Risk.PRIVILEGED, True, True),
        ("locked", Risk.READ_ONLY, True, False),
        ("locked", Risk.LOCAL_WRITE, False, False),
        ("locked", Risk.DELIVERABLE, False, False),
    ])
    def test_the_mode_table(self, mode, risk, allowed, ask):
        d = policy.evaluate("t", risk, {}, mode=mode)
        assert (d.allowed, d.requires_approval) == (allowed, ask), d.reason

    def test_legacy_names_still_mean_something(self):
        assert policy.evaluate("t", Risk.COMPUTE, {}, mode="standard").mode == "trusted"
        assert policy.evaluate("t", Risk.COMPUTE, {}, mode="controlled").mode == "review"

    def test_mode_is_per_session_and_read_live(self, alice):
        s = sessions.create(alice, mode="locked")
        tid = _task(owner="alice", session=s["id"], mode="review")
        assert not policy.evaluate("write_file", Risk.LOCAL_WRITE, {},
                                   task_id=tid).allowed
        sessions.set_mode(s["id"], "trusted", alice)
        d = policy.evaluate("write_file", Risk.LOCAL_WRITE, {}, task_id=tid)
        assert d.allowed and not d.requires_approval
        assert any(r["outcome"] == "MODE_SET" for r in decisions.for_session(s["id"]))


# ------------------------------------------------------------------ identity

class TestIdentity:
    def test_token_authenticates_and_a_bad_one_does_not(self):
        out = identity.create_principal("bob", role="approver")
        p = identity.authenticate(f"Bearer {out['token']}", "10.0.0.5")
        assert p.name == "bob" and p.can("approver") and not p.can("admin")
        with pytest.raises(identity.AuthError):
            identity.authenticate("Bearer nope", "10.0.0.5")

    def test_remote_and_proxied_requests_need_a_token(self):
        with pytest.raises(identity.AuthError):
            identity.authenticate(None, "10.0.0.5")
        with pytest.raises(identity.AuthError):
            identity.authenticate(None, "127.0.0.1", forwarded=True)
        assert identity.authenticate(None, "127.0.0.1").can("admin")

    def test_a_network_bind_without_token_auth_refuses_to_start(self, monkeypatch):
        monkeypatch.setenv("SOVEREIGN_AUTH", "local")
        with pytest.raises(SystemExit):
            identity.check_bind_safety("0.0.0.0")
        monkeypatch.setenv("SOVEREIGN_AUTH", "token")
        identity.check_bind_safety("0.0.0.0")

    def test_approvals_need_a_named_human(self, alice):
        from sovereign.policy import tool_policy
        tid = _task(owner="alice")
        aid = tool_policy.request_approval(tid, "write_file", {}, "write")
        with pytest.raises(tool_policy.ApprovalRefused):
            tool_policy.decide_approval(aid, True, by="system")
        assert tool_policy.decide_approval(aid, True, by="carol")
        assert not tool_policy.decide_approval(aid, False, by="carol")  # once only

    def test_separation_of_duties(self, alice, monkeypatch):
        from sovereign.policy import tool_policy
        monkeypatch.setenv("SOVEREIGN_SEPARATION_OF_DUTIES", "1")
        tid = _task(owner="alice")
        aid = tool_policy.request_approval(tid, "run_code", {}, "run")
        with pytest.raises(tool_policy.ApprovalRefused):
            tool_policy.decide_approval(aid, True, by="alice")

    def test_a_cancelled_task_stops_waiting_for_its_approval(self):
        from sovereign.policy import tool_policy
        tid = _task()
        aid = tool_policy.request_approval(tid, "run_code", {}, "run")
        t0 = time.time()
        state = tool_policy.wait_for_approval(aid, timeout_s=30, poll_s=0.05,
                                              should_stop=lambda: True)
        assert state == "CANCELLED" and time.time() - t0 < 2


# ------------------------------------------------------------ sessions / transcript

class TestSessions:
    def test_working_set_kind_comes_from_the_file_not_the_client(self, alice, tmp_path):
        did = db.new_id("doc")
        db.insert("documents", {"id": did, "title": "nameplate.jpg", "kind": "jpg",
                                "path": str(tmp_path / "x.jpg"), "status": "READY",
                                "pages": 1, "created_at": time.time()})
        s = sessions.create(alice)
        sessions.attach(s["id"], did, alice)
        att = sessions.attachments(s["id"])
        assert att[0]["kind"] == "image"

    def test_another_engineer_cannot_read_a_session(self, alice):
        identity.create_principal("dave", role="engineer", issue_token=False)
        dave = identity.LocalSource().lookup("dave")
        s = sessions.create(alice)
        with pytest.raises(sessions.SessionError):
            sessions.check_access(s["id"], dave)

    def test_transcript_orders_request_decisions_and_answer(self, alice):
        s = sessions.create(alice)
        tid = _task(owner="alice", session=s["id"])
        decisions.record("admission", "ADMIT", "slot free", task_id=tid)
        db.update("tasks", "id", tid, {
            "state": "COMPLETED", "finished_at": time.time() + 1,
            "result": db.jdump({"summary": "V-204 is fit.",
                                "outcome": {"headline": "ESTABLISHED"}})})
        t = control.transcript.for_session(s["id"])
        kinds = [e["kind"] for e in t["entries"]]
        assert kinds[0] == "decision"                       # session MODE_SET
        assert kinds.index("user") < kinds.index("answer")
        text = control.transcript.render_text(t)
        assert "> p" in text and "[ESTABLISHED]" in text


# ------------------------------------------------------------ refusal contract

class TestOutcomes:
    def test_every_tool_result_carries_an_outcome(self, task_id):
        from sovereign.tools import register_all
        from sovereign.tools.base import ToolContext
        gw = register_all()
        ctx = ToolContext(task_id=task_id)
        ok = gw.invoke("list_documents", {}, ctx)
        # Arithmetic on literals nobody sourced is not an established value:
        # the calculator must not launder a number into Class B.
        unsourced = gw.invoke("calculator", {"expression": "2.5*3.1"}, ctx)
        bad = gw.invoke("calculator", {"expression": "import os"}, ctx)
        nosuch = gw.invoke("no_such_tool", {}, ctx)
        junk = gw.invoke("calculator", "not an object", ctx)
        for r in (ok, unsourced, bad, nosuch, junk):
            assert r.outcome in outcomes.OUTCOMES
        assert ok.outcome == outcomes.ESTABLISHED
        assert unsourced.ok and unsourced.outcome == outcomes.CANNOT_DETERMINE
        assert bad.outcome == nosuch.outcome == outcomes.CANNOT_DETERMINE
        row = db.query_one("SELECT outcome FROM tool_calls WHERE task_id=? "
                           "ORDER BY created_at LIMIT 1", (task_id,))
        assert row["outcome"] in outcomes.OUTCOMES

    def test_degraded_leads_the_answer(self):
        s = outcomes.summarise(
            [{"tool": "run_code", "outcome": "DEGRADED", "reason": "no netns"},
             {"tool": "calculator", "outcome": "ESTABLISHED"}], {"A": 3})
        assert s["headline"] == "DEGRADED" and "no netns" in s["degraded"][0]
        assert outcomes.summarise([], {"A": 2, "D": 1})["headline"] == \
            "CANNOT_DETERMINE"

    def test_the_observation_names_a_non_established_outcome(self):
        from sovereign.tools.base import ToolResult
        r = ToolResult(True, content="x", outcome="DEGRADED",
                       outcome_reason="raster only")
        assert r.for_model().startswith("[OUTCOME: DEGRADED — raster only]")


# ------------------------------------------------------------------ basis

class TestBasis:
    def _card(self, **profile):
        from sovereign.gateway.registry import ModelCard
        return ModelCard(name="m", backend="ollama", backend_ref="m",
                         weights_mb=3300, est_vram_mb=4200, profile=profile)

    def test_an_impossible_row_is_flagged_and_ignored(self):
        from sovereign.runtime import perfmodel
        prof = {"cold_load_s": 53.1, "vram_resident_mb": 0.0, "decode_tps": 18.6,
                "samples": 9}
        why = perfmodel.validate(prof)
        assert "vram_resident_mb" in why
        card = self._card(**prof, invalid_reason=why)
        assert card.residency_mb == 4200            # fell back to the estimate
        m = perfmodel.metric(card, "vram_resident_mb")
        assert m["basis"] == "prior" and m["range"]

    def test_basis_follows_sample_count(self):
        from sovereign.runtime import perfmodel
        few = perfmodel.metric(self._card(decode_tps=20.0, decode_samples=2),
                               "decode_tps")
        many = perfmodel.metric(self._card(decode_tps=20.0, decode_samples=40),
                                "decode_tps")
        none = perfmodel.metric(self._card(), "decode_tps")
        assert (few["basis"], many["basis"], none["basis"]) == \
            ("calibrated", "measured", "prior")

    def test_a_load_never_writes_a_time_into_the_vram_column(self):
        from sovereign.runtime import perfmodel
        db.upsert("model_profiles", {"model": "mx", "cold_load_s": 10.0,
                                     "vram_resident_mb": 5000.0,
                                     "load_samples": 3}, key="model")
        perfmodel.record_load("mx", 12.0, resident_mb=0.0)
        row = db.query_one("SELECT * FROM model_profiles WHERE model='mx'")
        assert row["vram_resident_mb"] == 5000.0 and row["load_samples"] == 4

    def test_wait_is_unknown_on_a_first_run(self):
        from sovereign.runtime import perfmodel
        db.execute("UPDATE tasks SET state='COMPLETED' WHERE state IN "
                   "('RUNNING','ADMITTED','QUEUED','PAUSED')")
        tid = _task()
        db.update("tasks", "id", tid, {"state": "QUEUED", "selected_model":
                                       "never-run-model", "task_type": "coding"})
        other = _task()
        db.update("tasks", "id", other, {"state": "RUNNING", "selected_model":
                                         "never-run-model", "task_type": "coding"})
        w = perfmodel.wait_estimate(tid)
        assert w["basis"] == "unknown" and "first run" in w["text"]


# ------------------------------------------------------------------ schema

def test_a_fresh_database_has_every_migrated_column(tmp_path):
    import sqlite3
    c = sqlite3.connect(str(tmp_path / "fresh.db"))
    c.row_factory = sqlite3.Row
    db._migrate(c)
    c.executescript(db.SCHEMA)
    db._migrate(c)
    for table, column, _ in db._ADDED_COLUMNS:
        cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        assert column in cols, f"{table}.{column} missing on a fresh install"


def test_a_measured_capability_replaces_the_catalogue_claim():
    from sovereign.gateway.registry import ModelCard
    claim = ModelCard(name="v", backend="ollama", backend_ref="v", caps={"vision": 0.95})
    assert claim.cap("vision") == 0.95 and claim.cap_basis("vision") == "claim"
    few = ModelCard(name="v", backend="ollama", backend_ref="v", caps={"vision": 0.95},
                    profile={"measured_caps": '{"vision": {"value": 0.2, "samples": 3}}'})
    assert few.cap("vision") == 0.95               # three items is not a measurement
    measured = ModelCard(name="v", backend="ollama", backend_ref="v",
                         caps={"vision": 0.95},
                         profile={"measured_caps": '{"vision": {"value": 0.31, "samples": 49}}'})
    assert measured.cap("vision") == 0.31 and measured.cap_basis("vision") == "measured"


def test_a_waiting_task_does_not_record_a_decision_per_tick():
    from sovereign.runtime.scheduler import AdmissionDecision, scheduler
    tid = _task()
    task = {"id": tid, "title": "t", "priority": "MEDIUM", "owner": "alice"}
    for free in (2853, 2288, 2301, 2799):
        scheduler._record_admission(task, AdmissionDecision(
            "QUEUE", f"waiting for host memory: only {free} MB is available"))
    assert len([d for d in decisions.for_task(tid) if d["authority"] == "admission"]) == 1
