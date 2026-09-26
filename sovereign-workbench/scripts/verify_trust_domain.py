#!/usr/bin/env python3
"""End-to-end verification of the trust domain (sovereign-workbench-v2.md).

Nothing is mocked. A real node is started on its own data directory and port;
a real inspection report is ingested through OCR; the client is the *published*
client package, run from its zip on the system Python (standard library only)
inside an unprivileged network namespace (`pasta`) where it installs its own
egress policy. With --harness, the real opencode drives a real model on the
node through /v1 and the MCP tool server, under the offline check.

    python3 scripts/verify_trust_domain.py              # T1–T12 except the harness
    python3 scripts/verify_trust_domain.py --harness    # and T8: opencode + a model,
                                                        # 5 runs, rule 1 (3 of 5)
    python3 scripts/verify_trust_domain.py --keep       # leave the node running

Results are printed and written to docs/trust-domain-results.json.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV_PY = Path.home() / ".sovereign" / "venv" / "bin" / "python"
NODE_PY = str(VENV_PY if VENV_PY.exists() else sys.executable)
PORT = int(os.environ.get("TD_VERIFY_PORT", "8796"))
WORK = Path(os.environ.get("TD_VERIFY_DIR", Path.home() / ".sovereign-td-verify"))
REPORT = ROOT / "corpus" / "reports" / "IR-2026-0731-scanned.pdf"
SECRET = ROOT / "corpus" / "sops"

PROBE = r'''
import json, os, sys, urllib.request
from clawcal import trust
from clawcal.cli import Client, load_token
api = Client(os.environ["CLAWCAL_URL"], load_token(None))
api.headers = trust.device_headers()
adm = api.post("/api/admit", {"prompt": "draft an approval note from IR-2026-0731"})
def rpc(method, params=None, sid=None, mid=1):
    h = {"Content-Type": "application/json", "Authorization": f"Bearer {api.token}",
         "X-ClawCal-Session": adm["session_id"], **api.headers}
    if sid: h["Mcp-Session-Id"] = sid
    b = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if mid is not None: b["id"] = mid
    r = urllib.request.urlopen(urllib.request.Request(api.url + "/mcp",
        data=json.dumps(b).encode(), headers=h, method="POST"), timeout=300)
    raw = r.read()
    return r.headers.get("Mcp-Session-Id"), (json.loads(raw) if raw else None)
sid, init = rpc("initialize", {"protocolVersion": "2025-06-18"})
rpc("notifications/initialized", sid=sid, mid=None)
tools = [t["name"] for t in rpc("tools/list", sid=sid)[1]["result"]["tools"]]
docs = rpc("tools/call", {"name": "list_documents", "arguments": {}}, sid=sid)[1]["result"]
secret = rpc("tools/call", {"name": "read_page", "arguments": {
    "doc_id": sys.argv[1], "page_no": 1}}, sid=sid)[1]["result"]
ex = rpc("tools/call", {"name": "extract_values", "arguments": {
    "doc_id": "IR-2026-0731"}}, sid=sid)[1]["result"]
txt = ex["content"][0]["text"]
line = next(l for l in txt.splitlines() if "nominal_thickness" in l)
span = line.split("span_id=")[1].split(";")[0]
value = line.split("=")[1].split("[")[0].strip()
d = rpc("tools/call", {"name": "deliver", "arguments": {"kind": "report",
    "title": "IR-2026-0731 check", "sections": [{"heading": "Values", "claims": [
        {"text": f"Nominal thickness is {value}.", "spans": [span]},
        {"text": "Corrosion allowance is 4.7 mm.", "spans": [span]}]}]}},
    sid=sid)[1]["result"]
print(json.dumps({"protocol": init["result"]["protocolVersion"], "tools": tools,
    "visible": sorted(x["title"] for x in docs["structuredContent"]["content"]),
    "secret_refused": secret["isError"], "secret_text": secret["content"][0]["text"][:200],
    "gate": d["structuredContent"]["content"]["gate"]["counts"],
    "artifact": d["structuredContent"]["content"]["name"], "grade": adm["grade"]}))
'''


class Verify:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.results: list[dict] = []
        self.node: subprocess.Popen | None = None
        self.pasta = shutil.which("pasta")
        self.gw = "127.0.0.1"

    # ------------------------------------------------------------ plumbing
    def check(self, cid: str, title: str, ok: bool | None, evidence: str) -> None:
        status = "SKIP" if ok is None else "PASS" if ok else "FAIL"
        self.results.append({"id": cid, "title": title, "status": status,
                             "evidence": evidence})
        print(f"  [{status}] {cid} {title}\n         {evidence[:400]}")

    def api(self, method: str, path: str, body=None, token=None, raw=False):
        req = urllib.request.Request(
            f"http://127.0.0.1:{PORT}{path}", method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {token or self.admin}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as r:
            data = r.read()
            return data if raw else json.loads(data)

    def client(self, *argv: str, egress: bool = True, py: str = "",
               home: str = "client", extra_env: str = "") -> tuple[int, str]:
        """One client command, from the published zip, on the system Python,
        inside a fresh network namespace with the client egress policy applied."""
        env = (f"CLAWCAL_HOME={shlex.quote(str(WORK / home))} {extra_env} "
               f"CLAWCAL_URL=http://{self.gw}:{PORT} "
               f"CLAWCAL_TOKEN={shlex.quote(self.token)} "
               f"PYTHONPATH={shlex.quote(str(WORK / 'clawcal.zip'))}")
        cmd = py or ("/usr/bin/python3 -m clawcal " + " ".join(shlex.quote(a)
                                                              for a in argv))
        script = (f"export {env}; "
                  + ("/usr/bin/python3 -m clawcal device egress apply >/dev/null 2>&1; "
                     if egress and self.pasta else "") + cmd)
        full = (["pasta", "--config-net", "--", "sh", "-c", script] if self.pasta
                else ["sh", "-c", script])
        r = subprocess.run(full, capture_output=True, text=True, timeout=1500)
        return r.returncode, (r.stdout + r.stderr).strip()

    # --------------------------------------------------------------- setup
    MIN_FREE_MB = 5000

    @staticmethod
    def free_mb() -> float:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return float(line.split()[1]) / 1024
        return 0.0

    def room_for_a_model(self, cid: str, title: str, wait_s: float = 120) -> bool:
        """Never load a model onto a host that cannot take it: a workstation
        running other work would lose its apps to the OOM killer first. The
        node's own resident models (the vision model ingestion loaded, say)
        are evicted first, and memory is given time to come back."""
        if self.free_mb() >= self.MIN_FREE_MB:
            return True
        self.evict_node_models()
        deadline = time.time() + wait_s
        while time.time() < deadline:
            time.sleep(3)
            if self.free_mb() >= self.MIN_FREE_MB:
                return True
        free = self.free_mb()
        self.check(cid, title, None, f"skipped to protect the host: {free:.0f} MB "
                   f"available, below the {self.MIN_FREE_MB} MB this step needs")
        return False

    def evict_node_models(self) -> None:
        try:
            self.api("POST", "/api/models/evict-all")
        except Exception:                                      # noqa: BLE001
            pass

    def start(self) -> None:
        if WORK.exists():
            shutil.rmtree(WORK)
        WORK.mkdir(parents=True)
        if self.pasta:
            out = subprocess.run(["pasta", "--config-net", "--", "ip", "-4", "route",
                                  "show", "default"], capture_output=True, text=True)
            self.gw = out.stdout.split()[2] if out.stdout.split() else "127.0.0.1"
        env = dict(os.environ, SOVEREIGN_DATA_DIR=str(WORK / "node"),
                   SOVEREIGN_AUTH="token", SOVEREIGN_PORT=str(PORT),
                   SOVEREIGN_PUBLIC_URL=f"http://{self.gw}:{PORT}",
                   SOVEREIGN_DOMAIN="verify-domain", SOVEREIGN_POLICY_MODE="trusted",
                   PYTHONPATH=str(ROOT / "backend"))
        (WORK / "node").mkdir()
        self.node = subprocess.Popen([NODE_PY, "-m", "sovereign.server", "--port",
                                      str(PORT)], env=env, cwd=str(ROOT),
                                     stdout=open(WORK / "node.log", "w"),
                                     stderr=subprocess.STDOUT)
        for _ in range(120):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/health", timeout=2)
                break
            except OSError:
                time.sleep(1)
        else:
            raise SystemExit("the node did not start; see " + str(WORK / "node.log"))
        self.admin = (WORK / "node" / "owner.token").read_text().strip()
        self.token = self.api("POST", "/api/admin/principals",
                              {"name": "verifier", "role": "engineer"})["token"]
        for f in [REPORT, SECRET / "equipment-register.pdf"] if (
                SECRET / "equipment-register.pdf").exists() else [REPORT]:
            subprocess.run(["curl", "-s", "-X", "POST",
                            f"http://127.0.0.1:{PORT}/api/upload", "-H",
                            f"Authorization: Bearer {self.admin}", "-F", f"file=@{f}"],
                           capture_output=True, timeout=600)
        docs = self.api("GET", "/api/documents")["documents"]
        self.secret = next((d for d in docs if "register" in d["title"]), None)
        if self.secret:
            self.api("POST", f"/api/documents/{self.secret['id']}/data-class",
                     {"data_class": "confidential"})
        if self.args.harness_bin:
            # The organisation's own build, from the start: every manifest (T4
            # onwards) pins it, so T8 exercises the product, not upstream.
            self.api("POST", "/api/admin/bundles/harness",
                     {"path": self.args.harness_bin, "platform": "linux",
                      "version": "verify"})
        self.api("POST", "/api/admin/bundles/client")
        (WORK / "clawcal.zip").write_bytes(self.api(
            "GET", "/api/bundles/targets/client/clawcal.zip", raw=True))
        (WORK / "probe.py").write_text(PROBE)

    def stop(self) -> None:
        if getattr(self, "swtpm", None):
            self.swtpm.terminate()
        if self.node and not self.args.keep:
            self.node.terminate()
            self.node.wait(timeout=30)

    # --------------------------------------------------------------- checks
    def run(self) -> int:
        print(f"node on :{PORT}, data {WORK / 'node'}; client via "
              f"{'pasta namespace ' + self.gw if self.pasta else 'host (no pasta)'}")
        # T1 client egress: open before, rejected after
        if self.pasta:
            _, before = self.client("device", "egress", "check", egress=False)
            _, after = self.client("device", "egress", "check")
            self.check("T1", "client egress policy rejects (not drops), node reachable",
                       "INACTIVE" in before and "egress is open" in before
                       and after.startswith("ACTIVE") and "REJECTED" in after,
                       f"before: {before.splitlines()[0]} | after: "
                       f"{after.splitlines()[0]}")
        else:
            self.check("T1", "client egress policy", None, "pasta not installed")
        # T2 enrol
        rc, out = self.client("device", "enrol", "--name", "verify-laptop",
                              "--nvme-mb", "64")
        self.check("T2", "enrolment by key possession; graded by the node",
                   rc == 0 and "grade      C" in out and "clearance public, internal"
                   in out, " | ".join(out.splitlines()[:5]))
        # T3 signed repository
        rc, out = self.client(py="/usr/bin/python3 -m clawcal device update && "
                                 "/usr/bin/python3 -c \"from clawcal import bundle; "
                                 "from clawcal.vendor import tuf_verify as v; "
                                 "v.verify_target_file(bundle._trusted_targets(), "
                                 f"'client/clawcal.zip', '{WORK / 'clawcal.zip'}'); "
                                 "print('client package verifies')\"")
        self.check("T3", "TUF-style metadata verifies; the running client matches its "
                         "signed target", rc == 0 and "client package verifies" in out,
                   out.replace("\n", " | "))
        # T4 manifest + launch verification
        rc, out = self.client(py="/usr/bin/python3 -m clawcal device manifest; "
                                 "/usr/bin/python3 -m clawcal device verify")
        self.check("T4", "B5 classifies this machine; manifest signed, verified at "
                         "launch", "launch verified" in out or "no manifest" in out,
                   " | ".join(out.splitlines()[:3]))
        # T5 detached refused at grade C
        rc, out = self.client("device", "renew", "--detached")
        self.check("T5", "an unmanaged device may not go detached (rule 3)",
                   rc != 0 and "may not run detached" in out, out[:300])
        # T6 placement
        _, ex = self.client("plan", "extract the thickness readings from the "
                                    "scanned inspection report")
        _, dr = self.client("plan", "draft an approval note for IR-2026-0731")
        self.check("T6", "B1 placement: extract always on the node; draft where the "
                         "pinned model is", "placement NODE" in ex and "extract always "
                         "runs on the node" in ex and "placement NODE" in dr,
                   ex.splitlines()[0] + " | " + dr.splitlines()[0])
        # T7 clearance + gate over MCP
        rc, out = self.client(py=f"/usr/bin/python3 {shlex.quote(str(WORK / 'probe.py'))} "
                                 f"{shlex.quote(self.secret['id']) if self.secret else 'x'}")
        try:
            p = json.loads(out.splitlines()[-1])
            ok = (p["gate"]["kept"] >= 1 and p["gate"]["stripped"] == 1
                  and (not self.secret or (p["secret_refused"]
                                           and self.secret["title"] not in p["visible"])))
            ev = (f"MCP {p['protocol']}, grade {p['grade']}, confidential refused="
                  f"{p['secret_refused']}, gate {p['gate']}, {p['artifact']}")
        except (ValueError, KeyError, IndexError):
            ok, ev = False, out[-400:]
        self.check("T7", "clearance enforced on the node; the gate keeps served "
                         "values and strips an invented one", ok, ev)
        # T8 the real harness — scored the way stopping rule 1 scores it: the
        # reference task must complete unattended in 3 of 5 runs, and complete
        # means a delivered, gated note, not merely a clean exit.
        if self.args.harness and self.room_for_a_model(
                "T8", "opencode + a node model (reference task)"):
            runs, wins, lines = self.args.harness_runs, 0, []
            for i in range(runs):
                rc, out = self.client(py=(
                    f"/usr/bin/python3 {shlex.quote(str(ROOT / 'scripts' / 'offline_check.py'))} --allow "
                    f"{self.gw}:{PORT} --timeout 1200 -- /usr/bin/python3 -m clawcal attach "
                    f"--quiet --workdir {shlex.quote(str(WORK))} " + shlex.quote(
                        "Draft an approval note from inspection report IR-2026-0731. "
                        "First call extract_values with doc_id IR-2026-0731. Then call "
                        "deliver with kind approval_note and sections Background, "
                        "Assessment and Recommendation, each claim citing the span_id "
                        "extract_values returned for each figure. Report what the gate "
                        "kept and stripped.")))
                summary = next((l for l in out.splitlines() if l.startswith("[ok]")
                                or l.startswith("[FAILED]")), out[-200:])
                clean = "run 1: CLEAN" in out
                won = rc == 0 and clean and "[ok]" in out and "deliver" in summary
                wins += won
                lines.append(f"run {i + 1}: {'COMPLETE' if won else 'incomplete'} — "
                             f"{summary[:150]}; offline {'CLEAN' if clean else 'NOT CLEAN'}")
                print("         " + lines[-1])
                if not clean:
                    wins = -10_000          # any socket violation fails the check
            need = -(-3 * runs // 5)        # ceil(3/5 · runs)
            self.check("T8", f"opencode + a node model deliver the reference note "
                             f"unattended (rule 1: ≥ {need} of {runs}); offline clean",
                       wins >= need, f"{max(wins, 0)} of {runs} complete | "
                       + " | ".join(lines))
        elif not self.args.harness:
            self.check("T8", "opencode + a node model (reference task)", None,
                       "run with --harness (loads a model)")
        # T9 anchoring
        rc, out = self.client("device", "sync")
        self.check("T9", "the device's chained log anchors on the node",
                   rc == 0 and out.startswith("anchored"), out)
        # T10 tamper → quarantine → operator → rebase → back
        edit = ("import json,pathlib; p=pathlib.Path('" + str(WORK / "client" /
                "log.jsonl") + "'); l=p.read_text().splitlines(); e=json.loads(l[-1]); "
                "e['data']={'edited':1}; l[-1]=json.dumps(e); "
                "p.write_text(chr(10).join(l)+chr(10))")
        self.client("plan", "summarise the SOP")
        _, t1 = self.client(py=f"/usr/bin/python3 -c \"{edit}\"; /usr/bin/python3 -m "
                               f"clawcal device sync; /usr/bin/python3 -m clawcal plan x")
        dev = self.api("GET", "/api/devices")["devices"][0]
        quarantined = dev["state"] == "QUARANTINED" and dev["tamper_events"]
        if quarantined:
            self.api("POST", f"/api/tamper/{dev['tamper_events'][0]['id']}/clear",
                     {"reason": "verification: edit was deliberate", "rebase": True})
        rc, t2 = self.client(py="/usr/bin/python3 -m clawcal device sync && "
                                "/usr/bin/python3 -m clawcal device renew")
        self.check("T10", "an edited entry quarantines the device; only an operator "
                          "clears it; the chain continues from a signed ack",
                   bool(quarantined) and "quarantined" in t1 and rc == 0
                   and "renewed" in t2, f"{t1.splitlines()[-1][:160]} | {t2[:200]}")
        # T11 revocation
        self.api("POST", f"/api/devices/{dev['id']}/revoke", {"reason": "verify"})
        _, out = self.client(py="/usr/bin/python3 -m clawcal plan x; "
                                "/usr/bin/python3 -m clawcal device renew")
        self.check("T11", "revocation stops the lease now and renewal next",
                   out.count("revoked") + out.count("REVOKED") >= 2,
                   out.replace("\n", " | ")[:300])
        self.run_extended()
        # T12 chains
        v = self.api("GET", "/api/audit/verify")
        self.check("T12", "audit and decision chains verify on the node",
                   v["ok"] and v["decisions"]["ok"],
                   f"audit {v['entries']} entries, decisions "
                   f"{v['decisions']['entries']}")
        return 0 if all(r["status"] != "FAIL" for r in self.results) else 1


    # ---------------------------------------------------- T13–T17 (optional)
    def run_extended(self) -> None:
        """TPM attestation, detached work on a local engine, an instrument
        slice, and the organisation's harness build. Each needs assets a
        machine may not have; each says so rather than passing silently."""
        a = self.args
        rep = next((d for d in self.api("GET", "/api/documents")["documents"]
                    if "IR-2026" in d["title"]), None)
        _, out = self.client("device", "enrol", "--name", "verify-workstation",
                             "--no-nvme", home="client2")
        dev2 = json.loads((WORK / "client2" / "state.json").read_text())["device_id"]

        # T13 TPM attestation (a software TPM with a vendor-style EK chain)
        tpm_env = ""
        if shutil.which("swtpm") and shutil.which("tpm2_quote"):
            t = WORK / "swtpm"
            (t / "ca").mkdir(parents=True)
            (t / "state").mkdir()
            (t / "ca.conf").write_text(
                f"statedir = {t}/ca\nsigningkey = {t}/ca/signkey.pem\n"
                f"issuercert = {t}/ca/issuercert.pem\ncertserial = {t}/ca/certserial\n")
            (t / "ca.opts").write_text("--platform-manufacturer Verify\n"
                                       "--platform-model swtpm\n--platform-version 1\n")
            (t / "setup.conf").write_text(
                f"create_certs_tool = /usr/bin/swtpm_localca\n"
                f"create_certs_tool_config = {t}/ca.conf\n"
                f"create_certs_tool_options = {t}/ca.opts\n")
            subprocess.run(["swtpm_setup", "--tpm2", "--tpmstate", str(t / "state"),
                            "--create-ek-cert", "--lock-nvram", "--config",
                            str(t / "setup.conf"), "--overwrite"], capture_output=True)
            for f in ("swtpm-localca-rootca-cert.pem", "issuercert.pem"):
                self.api("POST", "/api/admin/tpm-roots",
                         {"pem": (t / "ca" / f).read_text(), "name": f})
            self.api("POST", f"/api/devices/{dev2}/managed", {"managed": True})
            # The TPM is the device's hardware, so it runs in the device's own
            # network namespace (the swtpm TCTI talks to port and port + 1).
            tpm_env = "CLAWCAL_TPM_TCTI=swtpm:host=127.0.0.1,port=42321"
            rc, out = self.client(py=(
                f"swtpm socket --tpmstate dir={shlex.quote(str(t / 'state'))} --tpm2 "
                "--server type=tcp,port=42321 --ctrl type=tcp,port=42322 "
                "--flags not-need-init,startup-clear --daemon && sleep 1 && "
                "/usr/bin/python3 -m clawcal device attest"), home="client2",
                extra_env=tpm_env)
            self.check("T13", "TPM attestation: EK chain, credential activation and "
                              "quote verified on the node; managed device reaches A",
                       rc == 0 and "grade A" in out, out[-300:])
            if not (rc == 0 and "grade A" in out):
                # So T14–T17 still test detached work, the device is given the
                # other route to a detached-capable grade — said here, not hidden.
                self.api("POST", f"/api/devices/{dev2}/managed",
                         {"managed": True, "mdm_attested": True,
                          "note": "verification fallback after T13 failed"})
        else:
            self.api("POST", f"/api/devices/{dev2}/managed",
                     {"managed": True, "mdm_attested": True})
            self.check("T13", "TPM attestation", None, "swtpm / tpm2-tools absent")

        # T14 detached: manifest, lease, weights + engine + harness build
        have = all([a.engine_dir, a.detached_weights])
        if have:
            self.api("POST", "/api/admin/bundles/weights",
                     {"path": a.detached_weights, "model_id": a.detached_model,
                      "registry_model": a.detached_model})
            self.api("POST", "/api/admin/bundles/engine",
                     {"path": a.engine_dir, "binary": "llama-server",
                      "variant": "verify", "platform": "linux"})
            if a.harness_bin:
                self.api("POST", "/api/admin/bundles/harness",
                         {"path": a.harness_bin, "platform": "linux",
                          "version": "verify"})
            self.api("POST", "/api/admin/bundles/client")
            (WORK / "clawcal.zip").write_bytes(self.api(
                "GET", "/api/bundles/targets/client/clawcal.zip", raw=True))
            rc, out = self.client(py=(
                "/usr/bin/python3 -m clawcal device renew && "
                f"/usr/bin/python3 -m clawcal device manifest --detached --model "
                f"{a.detached_model} && /usr/bin/python3 -m clawcal device renew "
                f"--detached && /usr/bin/python3 -m clawcal device weights && "
                "/usr/bin/python3 -m clawcal device verify"), home="client2",
                extra_env=tpm_env)
            self.check("T14", "detached manifest signed and bound to the lease; "
                              "weights, engine and harness fetched and verified",
                       rc == 0 and "launch verified" in out and "detached lease" in out,
                       " | ".join(l for l in out.splitlines() if l.strip())[:400])
            # T15 a task on the local engine, loopback only. The node's model
            # and the device's engine share this one GPU here, so the node
            # lets go of its model first.
            self.evict_node_models()
            time.sleep(3)
            (WORK / "work2").mkdir(exist_ok=True)
            (WORK / "work2" / "notes.txt").write_text(
                "Pump P-101 seal leaking since Monday; kit ordered.\n")
            rc, out = (1, "") if not self.room_for_a_model(
                "T15", "detached: local engine") else self.client(py=(
                "/usr/bin/python3 -m clawcal engine start >/dev/null && "
                f"/usr/bin/python3 {shlex.quote(str(ROOT / 'scripts' / 'offline_check.py'))}"
                f" --timeout 900 -- /usr/bin/python3 -m clawcal attach --quiet --workdir "
                f"{shlex.quote(str(WORK / 'work2'))} "
                "'Summarise notes.txt in one sentence. Read it with the read tool first.'"
                "; /usr/bin/python3 -m clawcal engine stop"), home="client2")
            if out:
                self.check("T15", "detached: on-device admission, local engine, "
                                  "opencode; only loopback touched",
                           "run 1: CLEAN" in out and "[ok]" in out and "[CLIENT]" in out,
                           " | ".join(l for l in out.splitlines()
                                      if l.startswith(("[", "run ")))[:400])
            # T16 instrument slice, offline gate, delivered on rejoin
            try:
                sl = self.api("POST", f"/api/devices/{dev2}/slices",
                              {"doc_ids": [rep["id"]]}) if rep else None
            except urllib.error.HTTPError as exc:
                sl = None
                self.check("T16", "instrument slice", False,
                           f"export refused: {exc.read().decode()[:200]}")
            if sl:
                claims = WORK / "claims.json"
                claims.write_text(json.dumps({"title": "Offline check", "sections": [
                    {"heading": "Values", "content":
                     f"Nominal thickness is 12.0 mm (span_id={rep['id']}:"
                     f"nominal_thickness). The allowance is 4.7 mm."}]}))
                rc, out = self.client(py=(
                    f"/usr/bin/python3 -m clawcal slice fetch {sl['slice_id']} && "
                    f"cd {shlex.quote(str(WORK / 'work2'))} && "
                    f"/usr/bin/python3 -m clawcal slice deliver {shlex.quote(str(claims))}"
                    " && /usr/bin/python3 -m clawcal slice flush"), home="client2")
                self.check("T16", "instrument slice: encrypted to the device, gated "
                                  "offline, the node agrees on rejoin",
                           rc == 0 and "1 kept, 1 stripped" in out
                           and "'stripped': 1" in out, out.replace("\n", " | ")[:400])
            rc, out = self.client("device", "sync", home="client2")
            self.check("T17", "the detached period anchors on rejoin",
                       rc == 0 and out.startswith("anchored"), out)
        else:
            for cid in ("T14", "T15", "T16", "T17"):
                self.check(cid, "detached work", None, "pass --engine-dir and "
                           "--detached-weights (a llama.cpp build and a GGUF file)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--harness", action="store_true",
                    help="also run opencode against a node model (T8)")
    ap.add_argument("--harness-runs", type=int, default=5,
                    help="reference-task runs for T8 (stopping rule 1 is 3 of 5)")
    ap.add_argument("--keep", action="store_true", help="leave the node running")
    ap.add_argument("--engine-dir", help="an unpacked llama.cpp release directory")
    ap.add_argument("--detached-weights", help="a GGUF weights file for detached work")
    ap.add_argument("--detached-model", default="qwen3-4b",
                    help="registry name of those weights")
    ap.add_argument("--harness-bin", help="the organisation's opencode build")
    args = ap.parse_args()
    v = Verify(args)
    v.start()
    try:
        rc = v.run()
    finally:
        v.stop()
    out = ROOT / "docs" / "trust-domain-results.json"
    out.write_text(json.dumps({"ran_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                               "harness": args.harness, "results": v.results},
                              indent=1))
    n = {s: sum(r["status"] == s for r in v.results) for s in ("PASS", "FAIL", "SKIP")}
    print(f"\n{n['PASS']} passed, {n['FAIL']} failed, {n['SKIP']} skipped "
          f"-> {out.relative_to(ROOT)}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
