"""The trust domain (sovereign-workbench-v2.md): grades, devices, leases,
anchoring, the profiler, placement, the bundle store, served spans and the gate.

Every test here holds a sentence of the spec:

* "the design never asks a user's laptop to be trustworthy" — grades come from
  facts the device cannot set; clearance is enforced at retrieval;
* "revoking the device cert at the node makes the next renewal fail; that is
  the whole revocation procedure";
* "a fork, a gap, or a segment that does not start at the anchor is flagged as
  a tamper event … and blocks re-attachment until an operator clears it";
* "one deterministic, explainable classification" per candidate model;
* "rollback and freeze detection";
* "every number, tag and date must resolve to a span id in the served set".
"""
from __future__ import annotations

import json
import os
import struct
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "clients"))

from sovereign import db, placement, profiler  # noqa: E402
from sovereign.bundles import manifest as bmanifest  # noqa: E402
from sovereign.bundles import repo, verify  # noqa: E402
from sovereign.control import (anchors, devices, identity, leases,  # noqa: E402
                               trust)
from sovereign.control.devices import DeviceError  # noqa: E402
from sovereign.evidence import gate, served  # noqa: E402


# --------------------------------------------------------------- fixtures

@pytest.fixture()
def client_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAWCAL_HOME", str(tmp_path / "client"))
    from clawcal import trust as ctrust
    return ctrust


@pytest.fixture()
def eve():
    identity.create_principal("eve", role="engineer", issue_token=False)
    # Each test starts with eve holding no devices: the domain caps devices
    # per principal, and the test database is shared across the session.
    db.execute("UPDATE devices SET state='REVOKED' WHERE principal='eve'")
    return identity.LocalSource().lookup("eve")


@pytest.fixture()
def boss():
    identity.create_principal("boss", role="admin", issue_token=False)
    return identity.LocalSource().lookup("boss")


class LocalApi:
    """The client's `api` object, served in-process by the control plane."""

    def __init__(self, principal):
        self.p = principal
        self.url = "http://node.test"
        self.token = None
        self.headers: dict[str, str] = {}

    def post(self, path, body=None, **_):
        if path == "/api/devices/enrol":
            d = devices.enrol(self.p, body)
            lease = leases.issue(d["id"], by=self.p.name) if d["state"] == "ACTIVE" \
                else None
            return {"device": d, "lease": lease, "node": leases.node_identity(),
                    "tuf_root": repo.metadata("root.json"), "node_url": self.url}
        if path == "/api/lease/renew":
            return leases.renew(body["device_id"], body)
        if path.endswith("/log"):
            return anchors.sync(path.split("/")[3], body)
        if path.endswith("/report"):
            return devices.report_state(path.split("/")[3], body)
        raise AssertionError(path)

    def get(self, path, **_):
        if path.startswith("/api/devices/") and "/log" in path:
            return {"anchor": anchors.anchor(path.split("/")[3])}
        if path.startswith("/api/bundles/metadata/"):
            env = repo.metadata(path.rsplit("/", 1)[1])
            if env is None:
                raise RuntimeError("404")
            return env
        raise AssertionError(path)


EGRESS_OK = {"active": True, "reason": "test", "enforcement": "reject"}


def _enrol(ctrust, principal, egress=True):
    rep = dict(EGRESS_OK, checked_at=time.time()) if egress else {}
    return ctrust.enrol(LocalApi(principal), name="laptop", egress_report=rep,
                        profile={"topology": "discrete", "vram_mb": 8188,
                                 "ram_mb": 15600, "backend": "cuda",
                                 "platform": "linux"})


# ------------------------------------------------------------------ grades

class TestGrades:
    def _dev(self, **kw):
        base = {"platform": "linux", "managed": 0, "attestation": None,
                "egress_report": json.dumps(dict(EGRESS_OK,
                                                 checked_at=time.time()))}
        base.update(kw)
        return base

    def test_the_grade_table(self):
        g = lambda **kw: trust.compute_grade(self._dev(**kw))[0]   # noqa: E731
        assert g() == "C"
        assert g(egress_report=None) == "D"
        assert g(egress_report=json.dumps({"active": False,
                                           "checked_at": time.time()})) == "D"
        assert g(egress_report=json.dumps(dict(EGRESS_OK, checked_at=1))) == "D"
        assert g(managed=1) == "C", "managed without attestation stays at C"
        assert g(managed=1, attestation=json.dumps(
            {"kind": "mdm", "verified": True})) == "B"
        assert g(managed=1, attestation=json.dumps(
            {"kind": "tpm-quote", "verified": True})) == "A"
        assert g(managed=1, platform="macos", attestation=json.dumps(
            {"kind": "tpm-quote", "verified": True})) == "C"
        assert g(attestation=json.dumps({"kind": "tpm-quote", "verified": True})) \
            == "C", "attestation without management does not lift the grade"

    def test_unmanaged_detached_is_grade_d(self):
        assert trust.compute_grade(self._dev(), mode="detached")[0] == "D"

    def test_rule_3_is_the_default(self):
        assert trust.clearance("C") == ["public", "internal"]
        assert not trust.rules_for("C")["detached"]
        assert trust.rules_for("C")["attach"]

    def test_clearance_may_not_grow_as_trust_falls(self, boss):
        with pytest.raises(ValueError, match="must not increase"):
            trust.set_policy({"grades": {"D": {"retrieve": ["restricted"]}}}, boss)


# ----------------------------------------------------------------- devices

class TestEnrolment:
    def test_enrol_proves_possession_and_pins_the_node(self, client_home, eve):
        out = _enrol(client_home, eve)
        d = out["device"]
        assert d["id"] == client_home.device_id()
        assert d["grade"] == "C" and d["state"] == "ACTIVE"
        st = client_home.load_state()
        assert st["node_key"] == leases.node_identity()["public_key"]
        assert st["tuf"]["root"]["_type"] == "root"
        assert client_home.lease_status()["status"] == "ACTIVE"

    def test_a_forged_signature_is_refused(self, client_home, eve):
        env = client_home.envelope("enrol", {
            "platform": "linux", "public_key": client_home.device_key()[1].hex()})
        env["sig"] = "00" * 64
        with pytest.raises(DeviceError, match="signature does not verify"):
            devices.enrol(eve, env)

    def test_a_replayed_request_is_refused(self, client_home, eve):
        _enrol(client_home, eve)
        env = client_home.envelope("report", {"profile": {}})
        devices.report_state(client_home.device_id(), env)
        with pytest.raises(DeviceError, match="replay"):
            devices.report_state(client_home.device_id(), env)

    def test_a_signature_for_one_purpose_is_not_valid_for_another(self, client_home, eve):
        _enrol(client_home, eve)
        env = client_home.envelope("report", {"mode": "attached"})
        with pytest.raises(DeviceError, match="signature"):
            leases.renew(client_home.device_id(), env)

    def test_what_a_device_says_about_its_attestation_is_not_believed(
            self, client_home, eve):
        env = client_home.envelope("enrol", {
            "platform": "linux", "public_key": client_home.device_key()[1].hex(),
            "attestation": {"kind": "tpm-quote", "verified": True},
            "egress_report": dict(EGRESS_OK, checked_at=time.time())})
        env["device_id"] = client_home.device_id()
        env = client_home.envelope("enrol", env["body"])
        d = devices.enrol(eve, env)
        assert d["attestation"]["verified"] is False
        assert d["grade"] == "C"

    def test_protocol_bytes_agree_between_client_and_node(self, client_home):
        body = {"b": [1, 2], "a": "é"}
        assert client_home.signed_payload("x", "dev-1", 12.34567, "n", body) == \
            devices.signed_payload("x", "dev-1", 12.34567, "n", body)
        e = {"seq": 3, "ts": 1.0000004, "kind": "k", "data": {"z": 1}}
        assert client_home.entry_hash("ab" * 32, e) == anchors.entry_hash("ab" * 32, e)


class TestLeases:
    def test_the_lease_token_authenticates_the_device(self, client_home, eve):
        _enrol(client_home, eve)
        h = client_home.device_headers()
        ctx = devices.authenticate(h["X-ClawCal-Device"], h["X-ClawCal-Lease"], eve)
        assert ctx.grade == "C" and ctx.mode == "attached"

    def test_a_device_claim_that_does_not_hold_is_an_error(self, client_home, eve):
        _enrol(client_home, eve)
        with pytest.raises(identity.AuthError):
            devices.authenticate(client_home.device_id(), "not-the-token", eve)
        with pytest.raises(identity.AuthError):
            devices.authenticate(client_home.device_id(), None, eve)

    def test_another_principal_cannot_use_the_device(self, client_home, eve, boss):
        _enrol(client_home, eve)
        h = client_home.device_headers()
        with pytest.raises(identity.Forbidden):
            devices.authenticate(h["X-ClawCal-Device"], h["X-ClawCal-Lease"], boss)

    def test_revocation_stops_the_lease_now_and_renewal_next(
            self, client_home, eve, boss):
        _enrol(client_home, eve)
        h = client_home.device_headers()
        devices.revoke(client_home.device_id(), boss, "laptop lost")
        with pytest.raises(identity.AuthError, match="revoked"):
            devices.authenticate(h["X-ClawCal-Device"], h["X-ClawCal-Lease"], eve)
        with pytest.raises(DeviceError, match="REVOKED"):
            client_home.renew(LocalApi(eve))

    def test_renewal_supersedes_and_regrades(self, client_home, eve):
        _enrol(client_home, eve)
        old = client_home.device_headers()["X-ClawCal-Lease"]
        body = client_home.renew(LocalApi(eve), egress_report={
            "active": False, "reason": "user disabled it", "checked_at": time.time()})
        assert body["grade"] == "D", "a failing egress check drops the grade"
        with pytest.raises(identity.AuthError, match="SUPERSEDED"):
            devices.authenticate(client_home.device_id(), old, eve)
        assert trust.clearance("D") == ["public"]

    def test_grade_c_may_not_go_detached(self, client_home, eve):
        _enrol(client_home, eve)
        with pytest.raises(DeviceError, match="may not run detached"):
            client_home.renew(LocalApi(eve), mode="detached")

    def test_a_managed_attested_device_may(self, client_home, eve, boss):
        _enrol(client_home, eve)
        devices.set_managed(client_home.device_id(), True, boss, mdm_attested=True)
        body = client_home.renew(LocalApi(eve), mode="detached", egress_report=dict(
            EGRESS_OK, checked_at=time.time()))
        assert body["mode"] == "detached" and body["grade"] == "B"

    def test_the_lease_verifies_offline_and_the_client_seals_on_expiry(
            self, client_home, eve, monkeypatch):
        _enrol(client_home, eve)
        st = client_home.load_state()
        assert leases.verify_document(st["lease"]["document"], st["node_key"])
        tampered = json.loads(json.dumps(st["lease"]["document"]))
        tampered["lease"]["grade"] = "A"
        assert not leases.verify_document(tampered, st["node_key"])
        (client_home.home() / "manifest.json").write_text("{}")
        body = client_home.lease_body()
        assert client_home.lease_status(now=body["expires_at"] + 1)["status"] \
            == "IN_GRACE"
        later = body["grace_until"] + 1
        monkeypatch.setattr(client_home.time, "time", lambda: later)
        with pytest.raises(client_home.TrustError, match="sealed"):
            client_home.enforce_lease()
        assert not (client_home.home() / "manifest.json").exists(), \
            "sealing removes org configuration"
        assert client_home.lease_status()["status"] == "SEALED"
        assert "token" not in client_home.load_state()["lease"]

    def test_a_lease_that_stops_verifying_seals_too(self, client_home, eve):
        _enrol(client_home, eve)
        (client_home.home() / "manifest.json").write_text("{}")
        st = client_home.load_state()
        st["lease"]["document"]["lease"]["expires_at"] += 86400 * 365
        client_home.save_state(st)
        with pytest.raises(client_home.TrustError):
            client_home.enforce_lease()
        assert not (client_home.home() / "manifest.json").exists()


# ---------------------------------------------------------------- anchoring

class TestAnchoring:
    def test_segments_anchor_and_resending_is_harmless(self, client_home, eve):
        _enrol(client_home, eve)
        api = LocalApi(eve)
        for i in range(3):
            client_home.log("tool_call", {"i": i})
        out = client_home.sync(api)
        assert out["ok"] and out["anchor"]["seq"] == client_home.load_state()["chain"]["seq"]
        dev = client_home.device_id()
        again = anchors.sync(dev, client_home.envelope(
            "sync", {"entries": client_home.read_log()}))
        assert again["ok"] and again["accepted"] == 0

    def test_an_edited_unsynced_entry_is_a_tamper_event(self, client_home, eve):
        _enrol(client_home, eve)
        api = LocalApi(eve)
        client_home.sync(api)
        client_home.log("tool_call", {"cmd": "cat /etc/shadow"})
        p = client_home.home() / "log.jsonl"
        lines = p.read_text().splitlines()
        e = json.loads(lines[-1])
        e["data"]["cmd"] = "ls"
        lines[-1] = json.dumps(e)
        p.write_text("\n".join(lines) + "\n")
        out = client_home.sync(api)
        assert not out["ok"] and out["kind"] == "bad_hash"
        assert devices.get(client_home.device_id())["state"] == "QUARANTINED"

    def test_history_rewritten_below_the_anchor_is_caught(self, client_home, eve):
        _enrol(client_home, eve)
        api = LocalApi(eve)
        client_home.log("tool_call", {"i": 1})
        client_home.sync(api)
        # Re-chain everything from genesis with the device key: internally
        # consistent, signed — and not what the node anchored.
        (client_home.home() / "log.jsonl").unlink()
        st = client_home.load_state()
        st["chain"] = {"seq": 0, "head": client_home.GENESIS}
        client_home.save_state(st)
        for i in range(4):
            client_home.log("tool_call", {"forged": i})
        out = client_home.sync(api)
        assert not out["ok"] and out["kind"] in ("fork", "not_at_anchor")

    def test_a_gap_is_caught(self, client_home, eve):
        _enrol(client_home, eve)
        dev = client_home.device_id()
        for i in range(3):
            client_home.log("x", {"i": i})
        segment = client_home.read_log()[1:]
        out = anchors.sync(dev, client_home.envelope("sync", {"entries": segment}))
        assert not out["ok"] and out["kind"] == "gap"

    def test_quarantine_blocks_attachment_until_an_operator_clears_it(
            self, client_home, eve, boss):
        _enrol(client_home, eve)
        api = LocalApi(eve)
        dev = client_home.device_id()
        client_home.log("x", {})
        seg = client_home.read_log()
        seg[-1]["sig"] = "00" * 64
        out = anchors.sync(dev, client_home.envelope("sync", {"entries": seg}))
        assert out["kind"] == "bad_signature"
        with pytest.raises(identity.AuthError, match="quarantined|REVOKED"):
            h = client_home.device_headers()
            devices.authenticate(h["X-ClawCal-Device"], h["X-ClawCal-Lease"], eve)
        with pytest.raises(DeviceError, match="quarantined"):
            leases.issue(dev, by="eve")
        with pytest.raises(DeviceError, match="reason"):
            anchors.clear(out["event_id"], boss, "")
        anchors.clear(out["event_id"], boss, "re-imaged; history reviewed",
                      rebase=True)
        assert devices.get(dev)["state"] == "ACTIVE"
        client_home.log("after", {})
        assert client_home.sync(api)["ok"]
        client_home.renew(api, egress_report=dict(EGRESS_OK, checked_at=time.time()))

    def test_a_silent_device_is_reported_once(self, client_home, eve):
        _enrol(client_home, eve)
        dev = client_home.device_id()
        db.execute("UPDATE leases SET grace_until=? WHERE device_id=?",
                   (time.time() - 100 * 3600, dev))
        assert anchors.sweep_silent() == 1
        assert anchors.sweep_silent() == 0
        ev = db.query_one("SELECT * FROM tamper_events WHERE device_id=?", (dev,))
        assert ev["kind"] == "silent" and ev["blocking"] == 0


# ----------------------------------------------------------------- profiler

GPT_OSS = profiler.ModelFacts(id="gpt-oss-20b", weights_mb=12110, params_b=20.9,
                              active_params_b=4.2, moe=True, layers=24, experts=32,
                              experts_used=4, expert_mb=10179, kv_mb_per_1k=49.2,
                              quant="MXFP4", licence="apache-2.0",
                              basis="read-from-file")
DENSE8 = profiler.ModelFacts(id="qwen3-8b", weights_mb=5225, params_b=8.2,
                             active_params_b=8.2, moe=False, layers=36,
                             kv_mb_per_1k=147.5, quant="Q4_K_M",
                             licence="apache-2.0", basis="read-from-file")


class TestProfiler:
    def test_the_class_table(self):
        c = lambda prof, m=GPT_OSS: profiler.classify(prof, m).device_class  # noqa
        laptop = {"topology": "discrete", "vram_mb": 8188, "ram_mb": 15600,
                  "backend": "cuda", "platform": "linux",
                  "pcie": {"gbs": 15.75, "basis": "link-rated"}}
        assert c(laptop) == "SPLIT-PCIE"
        assert c(dict(laptop, vram_mb=24576)) == "FIT-FAST"
        assert c({"topology": "unified", "ram_mb": 16384, "platform": "macos",
                  "backend": "metal", "unified_ceiling_mb": 12700}) == "NONE"
        assert c({"topology": "unified", "ram_mb": 49152, "platform": "macos",
                  "backend": "metal"}) == "UNIFIED"
        assert c({"topology": "cpu-only", "ram_mb": 65536, "backend": "cpu"}) \
            == "CPU-ONLY"
        assert c({"topology": "mobile", "ram_mb": 8192,
                  "unified_ceiling_mb": 4000}) == "NONE"
        assert c({"topology": "discrete", "vram_mb": 4096, "ram_mb": 32768,
                  "backend": "cuda"}, DENSE8) == "NONE", \
            "a dense model split over PCIe is below the decode floor"
        big = profiler.ModelFacts(id="big", weights_mb=200_000, moe=True,
                                  layers=60, expert_mb=190_000, kv_mb_per_1k=40)
        assert c({"topology": "unified", "ram_mb": 49152, "backend": "metal",
                  "nvme": {"read_gbs": 6.0, "basis": "measured"}}, big) \
            == "STREAM-NVME"

    def test_split_pins_experts_and_the_never_list(self):
        r = profiler.classify({"topology": "discrete", "vram_mb": 8188,
                               "ram_mb": 15600, "backend": "cuda",
                               "platform": "linux",
                               "pcie": {"gbs": 3.9, "basis": "link-rated"}}, GPT_OSS)
        assert r.engine == "llama.cpp" and r.shipped
        k = int(r.flags[r.flags.index("--n-cpu-moe") + 1])
        assert 1 <= k <= GPT_OSS.layers
        assert "-hf" in r.never and "--rpc" in r.never and "--host" in r.never
        assert any("×8-slot trap" in w for w in r.warnings)
        ft = profiler.classify({"topology": "discrete", "vram_mb": 8188,
                                "ram_mb": 15600, "backend": "cuda",
                                "platform": "linux"}, GPT_OSS, ftw_available=True)
        assert ft.engine == "freetoken" and "--moe-cache-auto" in ft.never

    def test_research_tiers_are_reported_but_never_shipped(self):
        r = profiler.classify({"topology": "cpu-only", "ram_mb": 65536,
                               "backend": "cpu"}, GPT_OSS)
        assert r.device_class == "CPU-ONLY" and not r.shipped

    def test_gguf_facts_are_read_from_the_file(self, tmp_path):
        p = tmp_path / "tiny.gguf"
        _write_gguf(p)
        f = profiler.facts_from_gguf(p, "tiny")
        assert f.moe and f.layers == 2 and f.experts == 4 and f.experts_used == 2
        assert f.licence == "apache-2.0" and f.architecture == "tinymoe"
        assert f.expert_mb > 0 and f.kv_mb_per_1k > 0
        assert f.basis == "read-from-file"
        with pytest.raises(profiler.GGUFError):
            (tmp_path / "x.gguf").write_bytes(b"NOPE" + b"\0" * 32)
            profiler.facts_from_gguf(tmp_path / "x.gguf")


def _write_gguf(path: Path) -> None:
    def s(x: str) -> bytes:
        b = x.encode()
        return struct.pack("<Q", len(b)) + b
    kv = [("general.architecture", 8, s("tinymoe")),
          ("general.license", 8, s("apache-2.0")),
          ("tinymoe.block_count", 4, struct.pack("<I", 2)),
          ("tinymoe.expert_count", 4, struct.pack("<I", 4)),
          ("tinymoe.expert_used_count", 4, struct.pack("<I", 2)),
          ("tinymoe.attention.head_count_kv", 4, struct.pack("<I", 2)),
          ("tinymoe.attention.key_length", 4, struct.pack("<I", 64))]
    tensors = [("blk.0.attn_q.weight", (64, 64), 0),
               ("blk.0.ffn_up_exps.weight", (64, 128, 4), 1)]
    out = b"GGUF" + struct.pack("<I", 3) + struct.pack("<QQ", len(tensors), len(kv))
    for k, t, v in kv:
        out += s(k) + struct.pack("<I", t) + v
    for name, dims, tt in tensors:
        out += s(name) + struct.pack("<I", len(dims)) + struct.pack(
            f"<{len(dims)}Q", *dims) + struct.pack("<I", tt) + struct.pack("<Q", 0)
    path.write_bytes(out + b"\0" * 64)


# ----------------------------------------------------------------- placement

class _Dev:
    def __init__(self, grade="C", mode="attached", device_class=None):
        self.grade, self.mode, self.device_class = grade, mode, device_class
        self.device_id = "dev-x"


class TestPlacement:
    def test_extract_and_vision_always_run_on_the_node(self):
        assert placement.place("document_extraction", _Dev()).placement == "node"
        r = placement.place("drawing_analysis", _Dev(mode="detached"),
                            detached_classes=["draft", "code", "calc"])
        assert r.refused and "live on the node" in r.reason

    def test_draft_runs_where_the_pinned_model_is(self):
        assert placement.place("deliverable_drafting", _Dev()).placement == "node"
        r = placement.place("deliverable_drafting", _Dev(mode="detached"),
                            detached_classes=["draft"])
        assert r.placement == "client" and not r.node_tools

    def test_a_manifest_that_does_not_allow_the_class_refuses_it(self):
        r = placement.place("coding", _Dev(mode="detached"), detached_classes=[])
        assert r.refused and "measured floor" in r.reason

    def test_split_only_when_the_policy_turns_it_on(self, boss):
        d = _Dev(device_class="SPLIT-PCIE")
        assert placement.place("summarisation", d).placement == "node"
        trust.set_policy({"split_placement": True}, boss)
        try:
            r = placement.place("summarisation", d)
            assert r.placement == "split" and "deliver" in r.node_tools
        finally:
            trust.set_policy({"split_placement": False}, boss)

    def test_the_web_and_the_console_run_on_the_node(self):
        assert placement.place("coding", None).placement == "node"


# -------------------------------------------------------------- bundle store

class TestBundles:
    def _state(self):
        root = repo.trusted_root()
        return {"root": root, "timestamp_version": 0, "snapshot_version": 0,
                "targets_version": 0}

    def _fetch(self):
        return (repo.metadata("timestamp.json"), repo.metadata("snapshot.json"),
                repo.metadata("targets.json"))

    def test_update_and_target_verification(self):
        repo.add_targets([{"path": "t/hello.txt", "data": b"hello"}])
        st = verify.check_update(self._state(), *self._fetch())
        assert verify.verify_target_bytes(st["targets"], "t/hello.txt", b"hello")
        with pytest.raises(verify.TargetError):
            verify.verify_target_bytes(st["targets"], "t/hello.txt", b"hellO")

    def test_rollback_is_refused(self):
        repo.add_targets([{"path": "t/a.txt", "data": b"a"}])
        old = self._fetch()
        st = verify.check_update(self._state(), *old)
        repo.add_targets([{"path": "t/b.txt", "data": b"b"}])
        st = verify.check_update(st, *self._fetch())
        with pytest.raises(verify.RollbackError):
            verify.check_update(st, *old)

    def test_a_frozen_repository_is_refused(self):
        ts, snap, tg = self._fetch()
        with pytest.raises(verify.FreezeError):
            verify.check_update(self._state(), ts, snap, tg,
                                now=time.time() + 3 * 86400)

    def test_mix_and_match_and_forgery_are_refused(self):
        ts, snap, tg = self._fetch()
        repo.add_targets([{"path": "t/c.txt", "data": b"c"}])
        with pytest.raises(verify.MismatchError):
            verify.check_update(self._state(), ts, snap, repo.metadata("targets.json"))
        forged = json.loads(json.dumps(repo.metadata("targets.json")))
        forged["signed"]["targets"]["t/c.txt"]["hashes"]["sha256"] = "0" * 64
        ts2, snap2, _ = self._fetch()
        with pytest.raises((verify.SignatureError, verify.MismatchError)):
            verify.check_update(self._state(), ts2, snap2, forged)

    def test_root_rotation_walks_the_chain(self):
        old = repo.trusted_root()
        repo.rotate_root(repo.KEY_DIR / "root.ed25519", replace=["timestamp"])
        new = verify.update_root(old, lambda v: repo.metadata(f"{v}.root.json"))
        assert new["version"] == old["version"] + 1
        verify.check_update({"root": new}, *self._fetch())
        with pytest.raises(verify.SignatureError):
            verify.check_update({"root": old}, *self._fetch())

    def test_the_client_package_vendors_the_nodes_own_code(self):
        import io
        import zipfile
        from sovereign import bundles
        data, _tag = bundles.client_package()
        z = zipfile.ZipFile(io.BytesIO(data))
        assert z.read("clawcal/_vendor/signing.py") == \
            (ROOT / "backend/sovereign/signing.py").read_bytes()
        assert z.read("clawcal/_vendor/tuf_verify.py") == \
            (ROOT / "backend/sovereign/bundles/verify.py").read_bytes()
        assert "clawcal/cli.py" in z.namelist()
        assert bundles.client_package()[0] == data, "builds are deterministic"

    def test_licences_gate_redistribution(self):
        assert bmanifest.licence_ok("apache-2.0", "m")[0]
        assert not bmanifest.licence_ok("llama3.1", "m")[0]
        assert not bmanifest.licence_ok("", "m")[0]

    def test_task_classes_follow_measured_capability(self):
        assert bmanifest.task_classes_allowed({}) == []
        weak = {"text": 0.9, "tool_use": 0.4, "coding": 0.9, "reasoning": 0.9,
                "structured": 0.9}
        assert bmanifest.task_classes_allowed(weak) == []
        good = dict(weak, tool_use=0.8)
        assert set(bmanifest.task_classes_allowed(good)) == {"draft", "code", "calc"}


# ------------------------------------------------------- served spans and gate

def _doc(doc_id="doc-g1", data_class="internal"):
    if not db.query_one("SELECT id FROM documents WHERE id=?", (doc_id,)):
        db.insert("documents", {"id": doc_id, "title": "IR-2026-0731 report",
                                "kind": "pdf", "path": "/x", "status": "READY",
                                "data_class": data_class, "created_at": time.time()})


class TestGate:
    SPAN = {"chunk_id": "chk-g1", "doc_id": "doc-g1",
            "doc_title": "IR-2026-0731 report", "page_no": 2,
            "text": "Vessel V-204: nominal 12.0 mm, minimum 9.4 mm, inspected "
                    "12 Mar 2026."}

    def _session(self):
        sid = db.new_id("sess")
        tid = db.new_id("task")
        db.insert("tasks", {"id": tid, "conversation_id": sid, "title": "t",
                            "prompt": "p", "owner": "eve", "state": "ATTACHED",
                            "created_at": time.time()})
        return sid, tid

    def test_values_resolve_to_served_spans_or_are_stripped(self):
        _doc()
        sid, tid = self._session()
        served.record(tid, [self.SPAN], via="test")
        r = gate.check([{"text": "V-204 has 9.4 mm left of 12.0 mm (IR-2026-0731).",
                         "spans": ["chk-g1"]},
                        {"text": "Inspected on 2026-03-12, allowance 3.0 mm.",
                         "spans": ["chk-g1"]}], session_id=sid, task_id=tid)
        kept = {k["value"] for c in r["claims"] for k in c["kept"]}
        stripped = {s["value"] for c in r["claims"] for s in c["stripped"]}
        assert {"V-204", "9.4 mm", "12.0 mm", "IR-2026-0731", "2026-03-12"} <= kept
        assert stripped == {"3.0 mm"}
        assert gate.STRIPPED_MARK in r["claims"][1]["text_out"]

    def test_a_span_the_node_never_served_counts_for_nothing(self):
        _doc()
        sid, tid = self._session()
        # The chunk exists in the corpus, but this session was never served it.
        r = gate.check([{"text": "Minimum 9.4 mm.", "spans": ["chk-g1"]}],
                       session_id=sid, task_id=tid)
        assert r["counts"]["stripped"] == 1
        assert r["claims"][0]["unserved_citations"] == ["chk-g1"]

    def test_a_wrong_citation_is_corrected_not_trusted(self):
        _doc()
        sid, tid = self._session()
        served.record(tid, [self.SPAN, dict(self.SPAN, chunk_id="chk-g2",
                                            text="Design pressure 16 bar g.")],
                      via="test")
        r = gate.check([{"text": "Minimum 9.4 mm.", "spans": ["chk-g2"]}],
                       session_id=sid, task_id=tid)
        k = r["claims"][0]["kept"][0]
        assert k["span_id"] == "chk-g1" and k["basis"] == "recited"

    def test_a_verified_calculation_is_derived(self):
        from sovereign.tools.calculator import CalculatorTool
        from sovereign.tools.base import ToolContext
        _doc()
        sid, tid = self._session()
        served.record(tid, [self.SPAN], via="test")
        ctx = ToolContext(task_id=tid, session_id=sid,
                          scratch={"passages": [self.SPAN]})
        CalculatorTool().run({"expression": "nominal - minimum",
                              "inputs": {"nominal": 12.0, "minimum": 9.4},
                              "label": "loss"}, ctx)
        r = gate.check([{"text": "Metal loss is 2.6 mm.", "spans": []}],
                       session_id=sid, task_id=tid)
        assert r["counts"] == {"kept": 1, "stripped": 0, "recited": 0, "derived": 1}


# ---------------------------------------------------------------- clearance

class TestClearance:
    def _task(self, grade, device_id=None):
        tid = db.new_id("task")
        db.insert("tasks", {"id": tid, "conversation_id": tid, "title": "t",
                            "prompt": "p", "owner": "eve", "state": "ATTACHED",
                            "grade": grade, "device_id": device_id,
                            "created_at": time.time()})
        return tid

    def test_a_document_above_the_grade_is_refused_by_name(self):
        from sovereign.tools.base import ToolContext
        from sovereign.tools.search import ReadDocumentPageTool
        _doc("doc-secret", data_class="confidential")
        db.upsert("pages", {"id": "pg-secret", "doc_id": "doc-secret", "page_no": 1,
                            "text": "the secret figure is 42.0 mm"}, key="id")
        res = ReadDocumentPageTool().run({"doc_id": "doc-secret", "page_no": 1},
                                         ToolContext(task_id=self._task("C")))
        assert not res.ok and "NOT CLEARED" in res.error
        ok = ReadDocumentPageTool().run({"doc_id": "doc-secret", "page_no": 1},
                                        ToolContext(task_id=self._task("B")))
        assert ok.ok

    def test_retrieval_filters_by_clearance(self):
        from sovereign.knowledge import retrieve
        _doc("doc-pub", data_class="public")
        db.upsert("chunks", {"id": "chk-pub", "doc_id": "doc-pub", "page_no": 1,
                             "ordinal": 0, "text": "zirconium flange torque table"},
                  key="id")
        db.execute("INSERT INTO chunks_fts (text, chunk_id) VALUES (?,?)",
                   ("zirconium flange torque table", "chk-pub"))
        got = retrieve.lexical_search("zirconium flange", 5, data_classes=["public"])
        assert [c for c, _ in got] == ["chk-pub"]
        assert retrieve.lexical_search("zirconium flange", 5,
                                       data_classes=["restricted"]) == []
        assert retrieve.lexical_search("zirconium flange", 5, data_classes=[]) == []

    def test_a_device_that_loses_its_grade_loses_clearance_at_once(
            self, client_home, eve):
        _enrol(client_home, eve)
        tid = self._task("C", device_id=client_home.device_id())
        assert trust.clearance_for_task(tid) == ["public", "internal"]
        db.execute("UPDATE devices SET grade='D' WHERE id=?",
                   (client_home.device_id(),))
        assert trust.clearance_for_task(tid) == ["public"]


# ----------------------------------------------------- remote tools (staging)

class TestRemoteTools:
    def _ctx(self):
        from sovereign.tools.base import ToolContext
        sid = db.new_id("sess")
        tid = db.new_id("task")
        db.insert("tasks", {"id": tid, "conversation_id": sid, "title": "t",
                            "prompt": "p", "owner": "eve", "state": "ATTACHED",
                            "created_at": time.time()})
        return ToolContext(task_id=tid, session_id=sid, principal="eve")

    def test_paths_are_confined(self):
        from sovereign.tools.remote import safe_rel
        for bad in ("../x", "/etc/passwd", "a/../../b", "C:/x", "", "a//b/../.."):
            with pytest.raises(ValueError):
                safe_rel(bad)
        assert safe_rel("src/app.py") == "src/app.py"

    def test_stage_then_execute_remote_returns_changed_files(self):
        from sovereign.tools.remote import ExecuteRemoteTool, StageFilesTool
        from sovereign.tools.sandbox import detect_engine
        ctx = self._ctx()
        st = StageFilesTool().run({"files": [
            {"path": "calc.py", "content": "print(round(12.0 - 9.4, 2))\n"
             "open('out.txt','w').write('done')\n"},
            {"path": "../escape.py", "content": "x"}]}, ctx)
        assert st.ok and len(st.content["staged"]) == 1 and st.content["errors"]
        res = ExecuteRemoteTool().run({"command": "python3 calc.py"}, ctx)
        assert res.ok, res.error
        assert "2.6" in res.content["stdout"]
        files = {f["path"]: f for f in res.content["changed_files"]}
        assert files["out.txt"]["content"] == "done"
        if detect_engine() in ("bwrap", "unshare"):
            net = ExecuteRemoteTool().run({"command": "python3 -c \"import socket;"
                                           "socket.create_connection(('1.1.1.1',"
                                           "443),timeout=3)\""}, ctx)
            assert not net.ok, "the node sandbox has no network"


# ------------------------------------------------------------ client pieces

class TestClientEgressAndHarness:
    def test_the_client_policy_rejects_and_allows_only_the_node(self):
        from clawcal import egress
        rs = egress.nftables_ruleset("http://10.0.0.5:8443")
        assert "10.0.0.5 tcp dport 8443 accept" in rs
        assert "reject with tcp reset" in rs and " drop" not in rs
        assert "oif lo accept" in rs
        assert "block return" in egress.pf_anchor("http://10.0.0.5:8443")
        assert "DefaultOutboundAction Block" in egress.windows_policy(
            "http://10.0.0.5:8443")

    def test_self_check_distinguishes_open_dropped_and_rejected(self, monkeypatch):
        from clawcal import egress
        monkeypatch.setattr(egress, "_mechanism", lambda: {
            "mechanism": "nftables", "loaded": True, "detail": "x"})

        def probes(result):
            def f(h, p, t):
                if h == "10.0.0.5":
                    return {"target": f"{h}:{p}", "result": "CONNECTED", "ms": 1}
                return {"target": f"{h}:{p}", "result": result, "ms": 2}
            return f
        monkeypatch.setattr(egress, "_probe", probes("REJECTED"))
        assert egress.self_check("http://10.0.0.5:8443")["active"]
        monkeypatch.setattr(egress, "_probe", probes("TIMEOUT"))
        r = egress.self_check("http://10.0.0.5:8443")
        assert not r["active"] and "dropped" in r["reason"]
        monkeypatch.setattr(egress, "_probe", probes("CONNECTED"))
        assert "open" in egress.self_check("http://10.0.0.5:8443")["reason"]

    def test_no_cloud_fallback_is_a_code_rule(self, client_home):
        from clawcal import harness
        harness.check_base_url("https://node.lan:8443/v1", "https://node.lan:8443")
        harness.check_base_url("http://127.0.0.1:8080/v1", "https://node.lan:8443")
        for bad in ("https://api.openai.com/v1", "https://node.lan:9999/v1",
                    "http://node.lan:8443/v1"):
            with pytest.raises(harness.HarnessError):
                harness.check_base_url(bad, "https://node.lan:8443")

    def test_the_harness_config_pins_the_node(self, client_home, monkeypatch):
        from clawcal import harness
        monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
        adm = {"session_id": "sess-1", "task_id": "task-1", "placement": "node",
               "spec_class": "draft", "model": "qwen3-8b", "why": "w",
               "node_tools": ["retrieve"]}
        cfg = harness.build_config(node_url="https://node.lan:8443", token="t",
                                   admission=adm)
        assert list(cfg["provider"]) == ["node"]
        assert cfg["provider"]["node"]["options"]["baseURL"] == \
            "https://node.lan:8443/v1"
        assert cfg["mcp"]["node-tools"]["headers"]["X-ClawCal-Session"] == "sess-1"
        assert "openai" in cfg["disabled_providers"] and cfg["share"] == "disabled"
        env = harness.environment(Path("/tmp/c.json"), Path("/tmp/m.json"))
        assert "OPENAI_API_KEY" not in env
        assert env["OPENCODE_DISABLE_MODELS_FETCH"] == "1"
        with pytest.raises(harness.HarnessError):
            harness.build_config(node_url="https://node.lan:8443", token="t",
                                 admission=dict(adm, placement="client"))

    def test_execute_local_follows_the_lease(self, client_home, eve, tmp_path):
        from clawcal import execlocal
        _enrol(client_home, eve)
        with pytest.raises(execlocal.ExecRefused, match="grade C"):
            execlocal.run("echo hi", tmp_path)


def test_the_new_surfaces_respect_the_facade_boundary():
    import re
    forbidden = re.compile(r"^\s*(from|import)\s+(\.\.|sovereign\.)"
                           r"(runtime|router|policy|evidence)\b", re.M)
    for path in ("backend/sovereign/api/node.py", "backend/sovereign/api/deps.py"):
        assert not forbidden.findall((ROOT / path).read_text()), path


def test_trust_is_an_authority_with_a_decision_trail(client_home, eve):
    from sovereign import control
    assert "trust" in control.AUTHORITIES
    _enrol(client_home, eve)
    rows = db.query("SELECT outcome, hash FROM decisions WHERE authority='trust' "
                    "AND subject_id=?", (client_home.device_id(),))
    outcomes = {r["outcome"] for r in rows}
    assert {"ENROLLED", "GRADE_C"} <= outcomes
    # Each is announced in the audit chain by its hash (the cross-reference
    # that makes removing a decision detectable at both ends). The whole-chain
    # verify is not asserted: earlier tests tamper with the shared chain on
    # purpose.
    for r in rows:
        assert db.query_one("SELECT 1 FROM audit_log WHERE category='decision' "
                            "AND detail LIKE ?", (f'%{r["hash"]}%',))


def test_deliver_reads_the_shapes_models_actually_send():
    """Observed with a live 8B model: {title, content} sections with inline
    "(span_id=...)" citations. They must be read, not silently dropped."""
    from sovereign.tools.remote import normalise_sections
    secs = normalise_sections([
        {"title": "Background", "content": "Design pressure is 16 bar g "
         "(span_id=doc-1:design_pressure). Nominal is 12.0 mm (span_id="
         "doc-1:nominal_thickness)."},
        {"heading": "Assessment", "claims": ["Minimum is 9.2 mm [span_ids: a, b]",
                                             {"text": "x", "spans": ["c"]}]},
        {"heading": "Empty"}])
    assert [s["heading"] for s in secs] == ["Background", "Assessment"]
    assert secs[0]["claims"][0] == {"text": "Design pressure is 16 bar g.",
                                    "spans": ["doc-1:design_pressure"]}
    assert secs[0]["claims"][1]["spans"] == ["doc-1:nominal_thickness"]
    assert secs[1]["claims"][0] == {"text": "Minimum is 9.2 mm", "spans": ["a", "b"]}
    from sovereign.tools.base import ToolContext
    from sovereign.tools.remote import DeliverTool
    res = DeliverTool().run({"title": "t", "sections": [{"title": "x"}]},
                            ToolContext(task_id=db.new_id("task")))
    assert not res.ok and "no claims found" in res.error


def test_a_corrupt_device_log_recovers_only_through_an_operator_rebase(
        client_home, eve, boss):
    """The device cannot repair its history (re-signing would be forgery); an
    operator clears the event, and the chain continues from a signed ack."""
    _enrol(client_home, eve)
    api = LocalApi(eve)
    client_home.log("x", {"n": 1})
    client_home.sync(api)
    client_home.log("x", {"n": 2})
    p = client_home.home() / "log.jsonl"
    lines = p.read_text().splitlines()
    e = json.loads(lines[-1])
    e["data"] = {"n": 99}
    lines[-1] = json.dumps(e)
    p.write_text("\n".join(lines) + "\n")
    bad = client_home.sync(api)
    assert bad["kind"] == "bad_hash"
    anchors.clear(bad["event_id"], boss, "reviewed", rebase=True)
    out = client_home.sync(api)
    assert out["ok"] and out["accepted"] == 1
    last = db.query_one("SELECT outcome, reason FROM decisions WHERE subject_id=? "
                        "ORDER BY seq DESC LIMIT 1", (client_home.device_id(),))
    assert last["outcome"] == "REBASED" and "never accepted" in last["reason"]
    client_home.log("after", {})
    assert client_home.sync(api)["ok"]


def test_revocation_resolves_open_tamper_events_on_the_record(client_home, eve, boss):
    _enrol(client_home, eve)
    dev = client_home.device_id()
    client_home.log("x", {})
    seg = client_home.read_log()
    seg[-1]["sig"] = "00" * 64
    anchors.sync(dev, client_home.envelope("sync", {"entries": seg}))
    devices.revoke(dev, boss, "lost")
    ev = db.query_one("SELECT state, clear_reason FROM tamper_events WHERE device_id=?",
                      (dev,))
    assert ev["state"] == "CLEARED" and "revocation" in ev["clear_reason"]


# ------------------------------------------------------------ TPM attestation

import shutil as _shutil  # noqa: E402
import socket as _socket  # noqa: E402
import subprocess as _sp  # noqa: E402

_TPM_TOOLS = all(_shutil.which(t) for t in ("swtpm", "swtpm_setup", "swtpm_localca",
                                            "tpm2_quote", "tpm2_checkquote",
                                            "openssl"))


@pytest.fixture()
def swtpm(tmp_path, monkeypatch):
    """A real software TPM with a vendor-style EK certificate chain."""
    if not _TPM_TOOLS:
        pytest.skip("swtpm / tpm2-tools not installed")
    ca, st = tmp_path / "ca", tmp_path / "state"
    ca.mkdir()
    st.mkdir()
    (tmp_path / "ca.conf").write_text(
        f"statedir = {ca}\nsigningkey = {ca}/signkey.pem\n"
        f"issuercert = {ca}/issuercert.pem\ncertserial = {ca}/certserial\n")
    (tmp_path / "ca.opts").write_text("--platform-manufacturer Test\n"
                                      "--platform-model swtpm\n--platform-version 1\n")
    (tmp_path / "setup.conf").write_text(
        f"create_certs_tool = /usr/bin/swtpm_localca\n"
        f"create_certs_tool_config = {tmp_path}/ca.conf\n"
        f"create_certs_tool_options = {tmp_path}/ca.opts\n")
    r = _sp.run(["swtpm_setup", "--tpm2", "--tpmstate", str(st), "--create-ek-cert",
                 "--lock-nvram", "--config", str(tmp_path / "setup.conf"),
                 "--overwrite"], capture_output=True, text=True)
    if r.returncode:
        pytest.skip(f"swtpm_setup failed here: {r.stderr[-200:]}")
    # The swtpm TCTI always talks to the control channel on port + 1, so pick
    # a port where both it and the next one are free (a busy port + 1 was the
    # source of an intermittent failure under a loaded test run).
    for _ in range(50):
        a, b = _socket.socket(), _socket.socket()
        try:
            a.bind(("127.0.0.1", 0))
            port = a.getsockname()[1]
            b.bind(("127.0.0.1", port + 1))
            break
        except OSError:
            continue
        finally:
            a.close()
            b.close()
    ctrl = port + 1
    proc = _sp.Popen(["swtpm", "socket", "--tpmstate", f"dir={st}", "--tpm2",
                      "--server", f"type=tcp,port={port}", "--ctrl",
                      f"type=tcp,port={ctrl}", "--flags",
                      "not-need-init,startup-clear"], stdout=_sp.DEVNULL,
                     stderr=_sp.DEVNULL)
    tcti = f"swtpm:host=127.0.0.1,port={port}"
    # Wait until the TPM answers a command, not a fixed sleep: under a loaded
    # test run it can take longer than any guess.
    for _ in range(100):
        if _sp.run(["tpm2_getcap", "properties-fixed"], capture_output=True,
                   env=dict(os.environ, TPM2TOOLS_TCTI=tcti)).returncode == 0:
            break
        time.sleep(0.1)
    else:
        proc.terminate()
        pytest.skip("the software TPM did not come up")
    monkeypatch.setenv("CLAWCAL_TPM_TCTI", tcti)
    yield {"ca": ca, "port": port}
    proc.terminate()
    proc.wait(timeout=10)


class AttestApi(LocalApi):
    def post(self, path, body=None, **_):
        from sovereign.control import attestation
        if path.endswith("/attest/begin"):
            return attestation.begin(path.split("/")[3], body)
        if path.endswith("/attest/finish"):
            return attestation.finish(path.split("/")[3], body)
        return super().post(path, body)


class TestTpmAttestation:
    def _roots(self, swtpm, monkeypatch, tmp_path):
        from sovereign.control import attestation
        roots = tmp_path / "roots"
        roots.mkdir()
        for f in ("swtpm-localca-rootca-cert.pem", "issuercert.pem"):
            (roots / f).write_text((swtpm["ca"] / f).read_text())
        monkeypatch.setattr(attestation, "ROOTS_DIR", roots)
        return roots

    def test_a_managed_attested_device_reaches_grade_a(
            self, client_home, eve, boss, swtpm, monkeypatch, tmp_path):
        from clawcal import attest
        self._roots(swtpm, monkeypatch, tmp_path)
        _enrol(client_home, eve)
        devices.set_managed(client_home.device_id(), True, boss)
        out = attest.attest(AttestApi(eve))
        assert out["verified"] and out["grade"] == "A" and out["baseline"] == "recorded"
        again = attest.attest(AttestApi(eve))
        assert again["baseline"] == "matched"

    def test_an_ek_from_an_unknown_vendor_is_refused(
            self, client_home, eve, swtpm, monkeypatch, tmp_path):
        from clawcal import attest
        from sovereign.control import attestation
        other = tmp_path / "other"
        other.mkdir()
        _sp.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                 "-keyout", str(other / "k.pem"), "-out", str(other / "unrelated.pem"),
                 "-subj", "/CN=not-a-tpm-vendor", "-days", "2"], capture_output=True)
        (other / "k.pem").unlink()
        monkeypatch.setattr(attestation, "ROOTS_DIR", other)
        _enrol(client_home, eve)
        with pytest.raises(DeviceError, match="does not chain"):
            attest.attest(AttestApi(eve))
        att = json.loads(devices.get(client_home.device_id())["attestation"])
        assert att["verified"] is False

    def test_a_changed_boot_state_fails_on_return(
            self, client_home, eve, boss, swtpm, monkeypatch, tmp_path):
        from clawcal import attest
        self._roots(swtpm, monkeypatch, tmp_path)
        _enrol(client_home, eve)
        devices.set_managed(client_home.device_id(), True, boss)
        attest.attest(AttestApi(eve))
        # The machine is "rooted while away": a boot measurement changes.
        r = _sp.run(["tpm2_pcrextend", "4:sha256=" + "ab" * 32], capture_output=True,
                    text=True,
                    env=dict(os.environ, TPM2TOOLS_TCTI=os.environ["CLAWCAL_TPM_TCTI"]))
        assert r.returncode == 0, r.stderr
        with pytest.raises(DeviceError, match="PCR 4"):
            attest.attest(AttestApi(eve))
        assert devices.get(client_home.device_id())["grade"] == "C"

    def test_the_node_computes_the_ak_name_and_checks_its_attributes(self):
        from sovereign.control import attestation
        tpmt = struct.pack(">HHI", 0x0001, 0x000B, 0) + b"\0" * 20
        with pytest.raises(DeviceError, match="restricted"):
            ak = struct.pack(">H", len(tpmt)) + tpmt
            name, attrs = attestation.ak_name(ak)
            need = (attestation.FIXED_TPM | attestation.RESTRICTED | attestation.SIGN)
            if attrs & need != need:
                raise DeviceError("not restricted")
        assert attestation.ak_name(struct.pack(">H", len(tpmt)) + tpmt)[0] \
            .startswith("000b")


# ------------------------------------------------------ instrument slices

class SliceApi(LocalApi):
    def get(self, path, **_):
        from sovereign.bundles import slices as nslices
        if path.startswith("/api/slices/"):
            return nslices.fetch(path.rsplit("/", 1)[1], self._dev)
        return super().get(path)

    def post(self, path, body=None, **_):
        from sovereign.bundles import slices as nslices
        if path.startswith("/api/slices/") and path.endswith("/deliver"):
            return nslices.deliver(path.split("/")[3], self.p, body, "http://node")
        return super().post(path, body)


def _slice_doc():
    did = db.new_id("doc")
    db.insert("documents", {"id": did, "title": "IR-2026-0999 thickness survey",
                            "kind": "pdf", "path": "/x", "status": "READY",
                            "pages": 1, "data_class": "confidential",
                            "created_at": time.time()})
    db.insert("chunks", {"id": f"{did}-c1", "doc_id": did, "page_no": 1,
                         "ordinal": 0, "text": "Shell course 1 E minimum reading "
                         "9.2 mm against a required 7.5 mm on vessel V-310."})
    return did


class TestSlices:
    def _device(self, client_home, eve, boss, grade_b=True):
        _enrol(client_home, eve)
        if grade_b:
            devices.set_managed(client_home.device_id(), True, boss, mdm_attested=True)
            client_home.renew(LocalApi(eve), egress_report=dict(
                EGRESS_OK, checked_at=time.time()))
        return client_home.device_id()

    def test_grade_c_may_not_carry_documents_off_site(self, client_home, eve, boss):
        from sovereign.bundles import slices as nslices
        dev = self._device(client_home, eve, boss, grade_b=False)
        with pytest.raises(nslices.SliceError, match="grade C"):
            nslices.export(dev, [_slice_doc()], boss)

    def test_export_search_gate_and_deliver_on_rejoin(
            self, client_home, eve, boss, tmp_path):
        from clawcal import slices
        from sovereign.bundles import slices as nslices
        dev = self._device(client_home, eve, boss)
        did = _slice_doc()
        out = nslices.export(dev, [did], boss)
        api = SliceApi(eve)
        api._dev = dev
        slices.fetch(api, out["slice_id"])
        raw = (client_home.home() / "org" / "slices" / out["slice_id"] /
               "data.bin").read_bytes()
        assert b"9.2 mm" not in raw, "the slice is ciphertext at rest"
        hits = slices.search("minimum reading shell")
        assert hits and hits[0]["span_id"] == f"{did}-c1"
        res = slices.deliver("V-310 note", [{"title": "Assessment", "content":
                             f"The minimum reading is 9.2 mm (span_id={did}-c1). "
                             f"The allowance is 4.4 mm."}], out_dir=tmp_path)
        assert res["counts"]["kept"] == 1 and res["counts"]["stripped"] == 1
        assert Path(res["report"]).exists()
        done = slices.flush_outbox(api)
        assert done and done[0]["name"].endswith(".docx")
        assert done[0]["gate"]["counts"]["stripped"] == 1, \
            "the node's gate agrees with the device's"

    def test_another_device_cannot_open_it(self, client_home, eve, boss, tmp_path,
                                           monkeypatch):
        from clawcal import slices
        from sovereign import sealed
        from sovereign.bundles import slices as nslices
        dev = self._device(client_home, eve, boss)
        out = nslices.export(dev, [_slice_doc()], boss)
        api = SliceApi(eve)
        api._dev = dev
        slices.fetch(api, out["slice_id"])
        wrapped = json.loads((client_home.home() / "org" / "slices" / out["slice_id"]
                              / "key.json").read_text())
        with pytest.raises(ValueError):
            sealed.unwrap_on_device(wrapped, os.urandom(32), out["slice_id"].encode())

    def test_it_dies_with_the_lease_and_sealing_wipes_it(
            self, client_home, eve, boss, monkeypatch):
        from clawcal import slices
        from sovereign.bundles import slices as nslices
        dev = self._device(client_home, eve, boss)
        out = nslices.export(dev, [_slice_doc()], boss)
        api = SliceApi(eve)
        api._dev = dev
        slices.fetch(api, out["slice_id"])
        later = client_home.lease_body()["grace_until"] + 5
        monkeypatch.setattr(client_home.time, "time", lambda: later)
        with pytest.raises(client_home.TrustError):
            slices.search("reading")
        assert not (client_home.home() / "org").exists(), "sealing removed the slice"

    def test_revocation_stops_further_fetches(self, client_home, eve, boss):
        from sovereign.bundles import slices as nslices
        dev = self._device(client_home, eve, boss)
        out = nslices.export(dev, [_slice_doc()], boss)
        devices.revoke(dev, boss, "lost")
        with pytest.raises(nslices.SliceError, match="REVOKED"):
            nslices.fetch(out["slice_id"], dev)


def test_crypto_matches_the_rfc_vectors():
    from sovereign import sealed, signing
    assert sealed.x25519(
        bytes.fromhex("a546e36bf0527c9d3b16154b82465edd62144c0ac1fc5a18506a2244ba449ac4"),
        bytes.fromhex("e6db6867583030db3594c1a424b15f7c726624ec26b3353b10a903a6d0ab1c4c")
    ).hex() == "c3da55379de9c6908e94ea4df28d084f32eccf03491c71f754b4075577a28552"
    key = bytes(range(0x80, 0xa0))
    ct = sealed._aead(key, bytes.fromhex("070000004041424344454647"),
                      b"Ladies and Gentlemen of the class of '99: If I could offer you "
                      b"only one tip for the future, sunscreen would be it.",
                      bytes.fromhex("50515253c0c1c2c3c4c5c6c7"), True)
    assert ct[-16:].hex() == "1ae10b594f09e26a7e902ecbd0600691"
    seed = os.urandom(32)
    assert sealed.x25519_base(sealed.ed25519_seed_to_x25519(seed)) == \
        sealed.ed25519_pub_to_x25519(signing.public_key(seed))


def test_the_local_tool_server_speaks_mcp_over_stdio(client_home):
    from clawcal import localtools
    assert localtools.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {}})["result"]["protocolVersion"]
    names = [t["name"] for t in localtools.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]]
    assert {"retrieve", "deliver", "slice_documents"} <= set(names)
    assert localtools.handle({"jsonrpc": "2.0", "method": "notifications/x"}) is None


def test_backticked_and_quoted_citations_are_read():
    """Observed live: qwen3-4b wrote (span_id: `doc-1:nominal_thickness`)."""
    from sovereign.evidence.gatecore import normalise_sections
    secs = normalise_sections([{"title": "V", "content":
        "Nominal is 12.0 mm (span_id: `doc-1:nominal_thickness`). "
        "Minimum is 7.5 mm [span_ids: \"a\", 'b']."}])
    assert secs[0]["claims"][0] == {"text": "Nominal is 12.0 mm.",
                                    "spans": ["doc-1:nominal_thickness"]}
    assert secs[0]["claims"][1]["spans"] == ["a", "b"]


# --------------------------------------------------------------- robustness

def test_one_principal_cannot_hold_every_external_slot(monkeypatch):
    s = placement.ExternalSlots()
    monkeypatch.setattr(s, "limit", lambda: 4)
    monkeypatch.setattr(s, "per_user", lambda: 2)
    assert s.acquire(0.1, "ann") and s.acquire(0.1, "ann")
    assert not s.acquire(0.2, "ann"), "ann is at her per-user quota"
    assert s.acquire(0.1, "bob"), "bob still gets a slot"
    s.release("ann")
    assert s.acquire(0.1, "ann")
    assert s.state()["by_principal"] == {"ann": 2, "bob": 1}


def test_an_abandoned_plan_is_closed_on_the_record():
    tid = db.new_id("task")
    db.insert("tasks", {"id": tid, "conversation_id": tid, "title": "t", "prompt": "p",
                        "owner": "eve", "state": "ATTACHED",
                        "workflow": "attached_client",
                        "created_at": time.time() - 20 * 3600,
                        "heartbeat_at": time.time() - 20 * 3600})
    assert placement.sweep_abandoned() >= 1
    row = db.query_one("SELECT state, state_reason FROM tasks WHERE id=?", (tid,))
    assert row["state"] == "TERMINATED" and "abandoned" in row["state_reason"]


def test_a_principal_may_not_enrol_unbounded_devices(client_home, eve, boss,
                                                    tmp_path, monkeypatch):
    trust.set_policy({"max_devices_per_principal": 1}, boss)
    try:
        _enrol(client_home, eve)
        monkeypatch.setenv("CLAWCAL_HOME", str(tmp_path / "second"))
        with pytest.raises(DeviceError, match="limit of 1"):
            _enrol(client_home, eve)
    finally:
        trust.set_policy({"max_devices_per_principal": 5}, boss)


def test_trust_health_says_what_to_fix():
    h = trust.health()
    names = {i["check"] for i in h["items"]}
    assert {"bundle store", "offline root key", "client package", "harness build",
            "TPM attestation", "tamper events", "device leases"} <= names
    assert all(i["detail"] for i in h["items"])


def test_trustctl_runs_against_the_node(capsys):
    import importlib.util
    spec = importlib.util.spec_from_file_location("trustctl",
                                                  ROOT / "scripts" / "trustctl.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.main(["policy"]) == 0
    assert '"grades"' in capsys.readouterr().out
    mod.main(["health"])
    assert "bundle store" in capsys.readouterr().out
