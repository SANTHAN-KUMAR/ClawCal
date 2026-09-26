#!/usr/bin/env python3
"""trustctl — administer the trust domain on the node.

Runs on the node itself, against its data directory (SOVEREIGN_DATA_DIR), as
the appliance owner. Every change goes through the same control-plane
functions as the API, so it lands in the decision log with the owner's name.

    trustctl health                              is the domain ready to serve?
    trustctl policy                              show the policy
    trustctl policy set KEY=JSON [KEY=JSON ...]   e.g. split_placement=true
    trustctl devices                             the roster
    trustctl managed DEVICE [--mdm] [--off]      admin states management / MDM
    trustctl revoke DEVICE REASON                revoke: lease stops, renewal fails
    trustctl clear EVENT REASON [--rebase]       clear a tamper event
    trustctl classify DOC CLASS                  public|internal|confidential|restricted
    trustctl publish-client                      sign and publish the client package
    trustctl add-weights PATH MODEL_ID [--registry NAME] [--internet-url URL]
    trustctl add-engine DIR BINARY [--engine llama.cpp] [--platform linux] [--variant V]
    trustctl add-harness FILE [--platform linux] [--version V]
    trustctl add-tpm-root PEM [--name NAME]
    trustctl slice DEVICE DOC [DOC ...]          export an instrument slice
    trustctl rotate-root --root-key PATH [--replace ROLE ...]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from sovereign import control, db  # noqa: E402


def _owner():
    db.init_db()
    return control.identity.ensure_owner()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="trustctl", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("health")
    p = sub.add_parser("policy")
    p.add_argument("action", nargs="?", default="show")
    p.add_argument("pairs", nargs="*")
    sub.add_parser("devices")
    p = sub.add_parser("managed")
    p.add_argument("device")
    p.add_argument("--mdm", action="store_true")
    p.add_argument("--off", action="store_true")
    p = sub.add_parser("revoke")
    p.add_argument("device")
    p.add_argument("reason")
    p = sub.add_parser("clear")
    p.add_argument("event")
    p.add_argument("reason")
    p.add_argument("--rebase", action="store_true")
    p = sub.add_parser("classify")
    p.add_argument("doc")
    p.add_argument("data_class")
    sub.add_parser("publish-client")
    p = sub.add_parser("add-weights")
    p.add_argument("path")
    p.add_argument("model_id")
    p.add_argument("--registry", default="")
    p.add_argument("--internet-url", default="")
    p = sub.add_parser("add-engine")
    p.add_argument("dir")
    p.add_argument("binary")
    p.add_argument("--engine", default="llama.cpp")
    p.add_argument("--platform", default="linux")
    p.add_argument("--variant", default="default")
    p = sub.add_parser("add-harness")
    p.add_argument("file")
    p.add_argument("--platform", default="linux")
    p.add_argument("--version", default="clawcal")
    p = sub.add_parser("add-tpm-root")
    p.add_argument("pem")
    p.add_argument("--name", default="")
    p = sub.add_parser("slice")
    p.add_argument("device")
    p.add_argument("docs", nargs="+")
    p = sub.add_parser("rotate-root")
    p.add_argument("--root-key", required=True)
    p.add_argument("--replace", nargs="*", default=[])
    a = ap.parse_args(argv)
    me = _owner()

    def out(obj) -> None:
        print(json.dumps(obj, indent=1, default=str))

    try:
        if a.cmd == "health":
            h = control.trust.health()
            for i in h["items"]:
                mark = "ok  " if i["ok"] else "--  " if i["ok"] is None else "FIX "
                print(f"{mark}{i['check']:<18} {i['detail']}")
            return 0 if h["ok"] else 1
        if a.cmd == "policy":
            if a.action == "set":
                changes = {}
                for pair in a.pairs:
                    k, _, v = pair.partition("=")
                    node = changes
                    *path, last = k.split(".")
                    for part in path:
                        node = node.setdefault(part, {})
                    node[last] = json.loads(v)
                out(control.trust.set_policy(changes, me))
            else:
                out(control.trust.policy())
        elif a.cmd == "devices":
            for d in control.devices.roster(me):
                lease = (d.get("lease") or {}).get("status", "—")
                tam = len(d["tamper_events"])
                print(f"{d['id']}  {d['state']:<11} grade {d.get('grade') or '?'}  "
                      f"{d['mode']:<8} lease {lease:<9} {d['principal']:<12} "
                      f"{d.get('name') or ''}" + (f"  TAMPER x{tam}" if tam else ""))
        elif a.cmd == "managed":
            out(control.devices.set_managed(a.device, not a.off, me,
                                            mdm_attested=a.mdm))
        elif a.cmd == "revoke":
            out(control.devices.revoke(a.device, me, a.reason))
        elif a.cmd == "clear":
            out(control.anchors.clear(a.event, me, a.reason, rebase=a.rebase))
        elif a.cmd == "classify":
            control.trust.set_data_class(a.doc, a.data_class, me)
            print(f"{a.doc} is now {a.data_class}")
        elif a.cmd == "publish-client":
            out(control.bundles.publish_client(by=me.name))
        elif a.cmd == "add-weights":
            out(control.bundles.manifest.add_weights(
                a.path, a.model_id, internet_url=a.internet_url,
                registry_model=a.registry))
        elif a.cmd == "add-engine":
            out(control.bundles.manifest.add_engine(
                a.dir, engine=a.engine, platform=a.platform, variant=a.variant,
                binary=a.binary))
        elif a.cmd == "add-harness":
            out(control.bundles.manifest.add_harness(a.file, platform=a.platform,
                                                     version=a.version))
        elif a.cmd == "add-tpm-root":
            out(control.attestation.add_root(Path(a.pem).read_text(),
                                             a.name or Path(a.pem).stem, me))
        elif a.cmd == "slice":
            out(control.bundles.slices.export(a.device, a.docs, me))
        elif a.cmd == "rotate-root":
            out(control.bundles.repo.rotate_root(Path(a.root_key), replace=a.replace,
                                                 by=me.name))
    except (ValueError, PermissionError, OSError) as exc:
        print(f"trustctl: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
