"""`clawcal` as a member device of a trust domain (sovereign-workbench-v2.md).

    clawcal device enrol [--name N] [--no-nvme]    key, profile, egress check, enrol
    clawcal device status                           lease, grade, mode, anchor
    clawcal device profile [--nvme-mb N]            B5 measurements → the node
    clawcal device egress plan|apply|remove|check [--report] [--platform P]
    clawcal device renew [--detached]               renew the lease (device key)
    clawcal device manifest [--detached] [--model M]  classify, fetch, verify
    clawcal device update                           refresh the signed metadata
    clawcal device weights                          fetch + verify the weights
    clawcal device verify [--full]                  what runs at every launch
    clawcal device sync                             upload the chained log
    clawcal device log                              verify the local chain
    clawcal device attest                           TPM quote, verified by the node
    clawcal plan "<task>"                           B1: class, placement, model
    clawcal attach "<task>" [--workdir DIR]         run it in the harness
    clawcal engine start|stop|status                the local engine (detached)
    clawcal exec-local "<command>" [--workdir DIR]  execute_local, if permitted
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from . import (bundle, detached, egress, execlocal, harness, profiler,
               supervisor, trust)

COMMANDS = ("device", "plan", "attach", "engine", "exec-local", "slice")


def _p(obj: Any) -> None:
    print(json.dumps(obj, indent=1, default=str))


def _node_url(api: Any) -> str:
    return trust.load_state().get("node_url") or api.url


def _as_device(api: Any) -> None:
    api.headers = trust.device_headers()


def _maybe_renew(api: Any) -> None:
    """Renew quietly when under a quarter of the lease is left and the node is
    reachable, so a long task does not run into the expiry. Away from the node
    the lease simply counts down; that is the design, not a failure."""
    body = trust.lease_body()
    st = trust.lease_status()
    if not body or st.get("status") != "ACTIVE":
        return
    ttl = float(body["expires_at"]) - float(body["issued_at"])
    if ttl <= 0 or st["expires_in_s"] > 0.25 * ttl:
        return
    try:
        trust.renew(api, mode=body["mode"],
                    egress_report=egress.self_check(_node_url(api)))
        print("(lease renewed)", file=sys.stderr)
    except Exception as exc:                                  # noqa: BLE001
        trust.log("renew_deferred", {"reason": str(exc)[:200]})


def _age(ts: float | None) -> str:
    if not ts:
        return "never"
    d = time.time() - ts
    return f"{d / 3600:.1f} h ago" if d > 3600 else f"{d / 60:.0f} min ago"


def device(api: Any, argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="clawcal device")
    ap.add_argument("action")
    ap.add_argument("sub", nargs="?")
    ap.add_argument("--name", default="")
    ap.add_argument("--no-nvme", action="store_true")
    ap.add_argument("--nvme-mb", type=int, default=256)
    ap.add_argument("--detached", action="store_true")
    ap.add_argument("--model")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--platform", choices=["linux", "windows", "macos"])
    a = ap.parse_args(argv)
    mode = "detached" if a.detached else None

    if a.action == "help":
        print(__doc__)
        return 0
    if a.action == "enrol":
        prof = profiler.measure(nvme=not a.no_nvme, nvme_mb=a.nvme_mb)
        eg = egress.self_check(api.url)
        out = trust.enrol(api, name=a.name, profile=prof, egress_report=eg)
        d = out["device"]
        print(f"enrolled {d['id']} ({d['platform']}) to {out['node']['domain']}")
        print(f"  node key   {out['node']['fingerprint']}  (compare with your admin)")
        print(f"  grade      {d['grade']}: {d['grade_reason']}")
        print(f"  state      {d['state']}")
        lease = out.get("lease") or {}
        if lease.get("token"):
            b = trust.lease_body() or {}
            print(f"  lease      {b.get('mode')}, clearance "
                  f"{', '.join(b.get('clearance') or []) or 'none'}")
        elif lease.get("refused"):
            print(f"  lease      REFUSED: {lease['refused']}")
        return 0
    st = trust.load_state()
    if not st.get("device_id") and a.action not in ("egress",):
        print("this device is not enrolled: `clawcal device enrol`", file=sys.stderr)
        return 2
    if a.action == "status":
        ls = trust.lease_status()
        _as_device(api)
        try:
            dev = api.get(f"/api/devices/{st['device_id']}")
        except Exception as exc:                              # noqa: BLE001
            dev = {"error": str(exc)}
        print(f"device   {st['device_id']}  ({st.get('domain')})")
        print(f"lease    {ls['status']}"
              + (f", {ls['mode']}, grade {ls['grade']}, expires in "
                 f"{ls['expires_in_s'] / 3600:.1f} h" if "mode" in ls else "")
              + (f" — {ls.get('reason')}" if ls.get("reason") else ""))
        if "error" not in dev:
            print(f"node     state {dev['state']}, grade {dev['grade']}: "
                  f"{dev['grade_reason']}")
            print(f"anchor   entry {dev['anchor']['seq']}, "
                  f"{_age(dev['anchor']['anchored_at'])}; local chain at "
                  f"{(st.get('chain') or {}).get('seq', 0)}")
            open_t = [t for t in dev["tamper_events"] if t["state"] == "OPEN"]
            if open_t:
                print(f"TAMPER   {len(open_t)} open: {open_t[0]['kind']} — "
                      f"{open_t[0]['detail'][:160]}")
            man = st.get("manifest")
            if man:
                print(f"manifest {man['device_class']} · {man['model']} "
                      f"({man['mode']})")
        else:
            print(f"node     unreachable: {dev['error'][:200]}")
        cannot = []
        if ls.get("mode") == "detached":
            cannot.append("extract and vision (they need the node's OCR, corpus and "
                          "gate)")
        if not ls.get("execute_local"):
            cannot.append("execute_local (not permitted at this grade)")
        if cannot:
            print("cannot here: " + "; ".join(cannot))
        return 0
    if a.action == "profile":
        prof = profiler.measure(nvme=not a.no_nvme, nvme_mb=a.nvme_mb)
        trust.report(api, {"profile": prof})
        _p(prof)
        return 0
    if a.action == "egress":
        node = _node_url(api)
        extra: list[str] = []
        try:
            extra = [e for e in bundle.load_manifest()["policy"]["egress_allow"]
                     if e not in ("127.0.0.1", "::1")]
        except Exception:                                     # noqa: BLE001
            pass
        sub = a.sub or "check"
        if sub == "plan":
            plat = a.platform or trust.platform_name()
            print({"linux": egress.nftables_ruleset, "windows": egress.windows_policy,
                   "macos": egress.pf_anchor}[plat](node, extra), end="")
            return 0
        if sub == "apply":
            out = egress.apply(node, extra)
            print("applied" if out["applied"] else f"NOT applied: {out['reason']}")
            if out["applied"] and st.get("device_id"):
                trust.log("egress_applied", {"platform": trust.platform_name()})
            return 0 if out["applied"] else 1
        if sub == "remove":
            _p(egress.remove())
            return 0
        rep = egress.self_check(node)
        if st.get("device_id"):
            trust.log("egress_check", {"active": rep["active"],
                                       "reason": rep["reason"]})
        if a.report and st.get("device_id"):
            out = trust.report(api, {"egress_report": rep})
            print(f"reported; the node grades this device {out['grade']}: "
                  f"{out['grade_reason']}")
        print(("ACTIVE  " if rep["active"] else "INACTIVE ") + rep["reason"])
        for p in rep["probes"]:
            print(f"  {p['target']:<26} {p['result']:<10} {p.get('ms', '')} ms "
                  f"{p.get('errno', '')}")
        return 0 if rep["active"] else 1
    if a.action == "renew":
        eg = egress.self_check(_node_url(api))
        body = trust.renew(api, mode=mode, egress_report=eg)
        print(f"renewed: {body['mode']} lease, grade {body['grade']}, clearance "
              f"{', '.join(body['clearance']) or 'none'}, expires "
              f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(body['expires_at']))}")
        return 0
    if a.action == "manifest":
        out = bundle.install_manifest(api, mode=mode or "attached", model=a.model)
        if not out.get("issued"):
            print(f"no manifest: {out.get('reason')}")
            for c in out.get("classes", []):
                print(f"  {c.get('model', '?'):<22} {c.get('device_class', '?'):<12} "
                      f"{(c.get('reason') or '')[:110]}")
            return 1
        m = out["manifest"]
        print(f"{m['device_class']} · {m['model']['id']} ({m['model']['quant']}) on "
              f"{m['engine']['name']}, {m['mode']}")
        print(f"  {m['classification']['reason']}")
        for w in m["classification"]["warnings"]:
            print(f"  warning: {w}")
        print(f"  task classes here: {', '.join(m['policy']['task_classes_allowed'])}")
        print(f"  never: {' '.join(m['engine']['never'])}")
        if m["mode"] == "detached":
            print("  renew the lease to bind it: `clawcal device renew --detached`")
        return 0
    if a.action == "update":
        new = bundle.update(api)
        print(f"verified: root v{new['root']['version']}, targets "
              f"v{new['targets_version']}, snapshot v{new['snapshot_version']}")
        return 0
    if a.action == "weights":
        w = bundle.install_weights(api)
        print(f"weights verified (full SHA-256): {w['path']}")
        return 0
    if a.action == "verify":
        out = bundle.verify_launch(full=a.full)
        m = out["manifest"]
        print(f"launch verified: manifest OK ({m['device_class']} · "
              f"{m['model']['id']}), weights {out['weights_check']}, lease "
              f"{out['lease']['status']}")
        return 0
    if a.action == "sync":
        out = trust.sync(api)
        if out.get("ok"):
            print(f"anchored: {out['accepted']} new entr"
                  f"{'y' if out['accepted'] == 1 else 'ies'}, node anchor at "
                  f"{out['anchor']['seq']}")
            return 0
        print(f"TAMPER EVENT {out.get('event_id')}: {out.get('kind')} — "
              f"{out.get('reason')}")
        return 1
    if a.action == "attest":
        from . import attest
        try:
            out = attest.attest(api)
        except attest.AttestError as exc:
            print(f"attestation not possible here: {exc}", file=sys.stderr)
            return 1
        print(f"TPM attestation verified by the node; baseline {out['baseline']}; "
              f"grade {out['grade']}: {out['grade_reason']}")
        return 0
    if a.action == "log":
        _p(trust.verify_local_log())
        return 0
    print(f"unknown device action {a.action!r}; see `clawcal device help`",
          file=sys.stderr)
    return 2


def plan(api: Any, argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="clawcal plan")
    ap.add_argument("prompt", nargs="+")
    a = ap.parse_args(argv)
    if trust.load_state().get("device_id") and \
            trust.enforce_lease().get("mode") == "detached":
        out = detached.admit(" ".join(a.prompt))
        print(f"{out['spec_class']} · placement {out['placement'].upper()} · grade "
              f"{out['grade']} · decided on this device (detached)")
        print(f"model   {out['model']}")
        print(f"why     {out['reason']}")
        return 0 if out["admitted"] else 1
    _as_device(api)
    out = api.post("/api/admit", {"prompt": " ".join(a.prompt)})
    trust.log("admitted", {k: out.get(k) for k in ("task_id", "spec_class",
                                                   "placement", "model")}) \
        if trust.load_state().get("device_id") else None
    print(f"{out['task_type']} → {out['spec_class']} · placement "
          f"{out['placement'].upper()} · grade {out['grade']}")
    print(f"model   {out['model'] or '—'}  ({out['model_reason'][:140]})")
    print(f"why     {out['reason']}")
    if out["node_tools"]:
        print(f"node    {', '.join(out['node_tools'])}")
    print(f"session {out['session_id']}  plan {out['task_id']}")
    if out["admitted"]:
        api.post(f"/api/admit/{out['task_id']}/close", {"reason": "plan only"})
    return 0 if out["admitted"] else 1


def attach(api: Any, argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="clawcal attach")
    ap.add_argument("prompt", nargs="+")
    ap.add_argument("--workdir", default=".")
    ap.add_argument("--startup-deadline", type=float, default=harness.STARTUP_DEADLINE_S)
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)
    prompt = " ".join(a.prompt)
    enrolled = bool(trust.load_state().get("device_id"))
    lease = trust.enforce_lease() if enrolled else {}
    away = lease.get("mode") == "detached"
    if away:
        # Away from the node: decide here, from the signed manifest.
        adm = detached.admit(prompt)
    else:
        if enrolled:
            _maybe_renew(api)
        _as_device(api)
        adm = api.post("/api/admit", {"prompt": prompt})
        if enrolled:
            trust.log("admitted", {k: adm.get(k) for k in ("task_id", "spec_class",
                                                           "placement", "model")})
    print(f"[{adm['placement'].upper()}] {adm['spec_class']} with "
          f"{adm['model'] or '—'} — {adm['reason']}")
    if not adm["admitted"]:
        return 1
    local_base = supervisor.status().get("base_url") \
        if adm["placement"] == "client" else None
    cfg = harness.build_config(node_url=_node_url(api), token=api.token,
                               admission=adm, detached_base=local_base)

    def show(ev: dict[str, Any]) -> None:
        if a.quiet:
            return
        part = ev.get("part") or {}
        if ev.get("type") == "tool_use":
            st = part.get("state") or {}
            print(f"  · {part.get('tool')} [{st.get('status')}]")
    def contacted() -> bool:
        if away:
            return supervisor.busy()
        return bool(api.get(f"/api/admit/{adm['task_id']}").get("heartbeat_at"))
    res = harness.run(prompt, admission=adm, cfg=cfg, workdir=Path(a.workdir),
                      startup_deadline_s=a.startup_deadline, on_event=show,
                      contacted=contacted)
    if not away:
        api.post(f"/api/admit/{adm['task_id']}/close",
                 {"reason": "harness finished" if res["ok"] else res["reason"][:200]})
    print()
    print(res["text"].strip() or "(no text)")
    print(f"\n[{'ok' if res['ok'] else 'FAILED'}] {res['wall_s']} s, tools: "
          f"{', '.join(res['tools_used']) or 'none'}"
          + (f" — {res['reason']}" if res["reason"] else ""))
    if res.get("stderr"):
        print(res["stderr"][-1500:], file=sys.stderr)
    return 0 if res["ok"] else 1


def engine(api: Any, argv: list[str]) -> int:
    action = argv[0] if argv else "status"
    if action == "start":
        _p(supervisor.start())
    elif action == "stop":
        print("stopped" if supervisor.stop() else "not running")
    else:
        _p(supervisor.status())
    return 0


def exec_local(api: Any, argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="clawcal exec-local")
    ap.add_argument("command")
    ap.add_argument("--workdir", default=".")
    ap.add_argument("--timeout", type=int)
    a = ap.parse_args(argv)
    try:
        r = execlocal.run(a.command, Path(a.workdir), timeout_s=a.timeout)
    except execlocal.ExecRefused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 3
    sys.stdout.write(r["stdout"])
    sys.stderr.write(r["stderr"])
    return r["exit_code"]


def slice_cmd(api: Any, argv: list[str]) -> int:
    from . import slices
    ap = argparse.ArgumentParser(prog="clawcal slice")
    ap.add_argument("action", choices=["fetch", "list", "search", "deliver", "flush"])
    ap.add_argument("arg", nargs="*")
    a = ap.parse_args(argv)
    try:
        if a.action == "fetch":
            b = slices.fetch(api, a.arg[0])
            print(f"slice {b['slice_id']}: {len(b['docs'])} document(s), {b['spans']} "
                  f"spans, usable until "
                  f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(b['expires_at']))}")
        elif a.action == "list":
            for s in slices.local():
                print(f"{s['slice_id']}  {', '.join(d['title'] for d in s['docs'])}  "
                      f"until {time.strftime('%Y-%m-%d', time.localtime(s['expires_at']))}")
        elif a.action == "search":
            for h in slices.search(" ".join(a.arg)):
                print(f"[{h['span_id']}] {h['doc_title']} p.{h['page_no']}: "
                      f"{h['text'][:160]}")
        elif a.action == "deliver":
            d = json.loads(Path(a.arg[0]).read_text())
            out = slices.deliver(d.get("title", "Report"), d.get("sections"),
                                 kind=d.get("kind", "report"))
            print(f"{out['report']}: {out['counts']['kept']} kept, "
                  f"{out['counts']['stripped']} stripped; .docx queued for the node")
        elif a.action == "flush":
            for o in slices.flush_outbox(api):
                print(f"node wrote {o.get('name')}: gate {o.get('gate', {}).get('counts')}")
    except slices.SliceError as exc:
        print(f"slice: {exc}", file=sys.stderr)
        return 1
    return 0


def main(api: Any, argv: list[str]) -> int:
    from .cli import ApiError
    cmd, rest = argv[0], argv[1:]
    try:
        if cmd == "device":
            return device(api, rest or ["status"])
        if cmd == "plan":
            return plan(api, rest)
        if cmd == "attach":
            return attach(api, rest)
        if cmd == "engine":
            return engine(api, rest)
        if cmd == "exec-local":
            return exec_local(api, rest)
        if cmd == "slice":
            return slice_cmd(api, rest)
    except ApiError as exc:
        print(f"node refused: {exc.detail}", file=sys.stderr)
        return 1
    except (trust.TrustError, bundle.BundleError, supervisor.EngineError,
            harness.HarnessError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 2
