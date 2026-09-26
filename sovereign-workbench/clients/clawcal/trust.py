"""The client's trust agent (sovereign-workbench-v2.md §6.1, §10).

    device key     an Ed25519 key that *is* the device. Software key in the
                   client's home directory (mode 0600) — grade C at best; a
                   TPM- or Secure-Enclave-held key is what a managed build adds
    enrolment      proves possession of the key; pins the node's identity and
                   the repository's root it receives
    lease          renewed with a request signed by the key; checked offline
                   against the node's signature; on expiry past its grace the
                   client SEALS: org configuration, manifests and harness
                   config are removed and refused until renewal
    chained log    every model call, tool call, gate result and policy check
                   appends H(prev ‖ entry), signed by the device key; synced to
                   the node, which checks it against its recorded anchor

Nothing here is trusted by the node. It is what lets the node *see* what this
device did, and lets a user see what their device is allowed to do.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform as _platform
import secrets
import shutil
import time
from pathlib import Path
from typing import Any

from .vendor import signing

GENESIS = "0" * 64


class TrustError(RuntimeError):
    pass


def home() -> Path:
    h = Path(os.environ.get("CLAWCAL_HOME", Path.home() / ".clawcal")).expanduser()
    h.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(h, 0o700)
    except OSError:
        pass
    return h


def _state_path() -> Path:
    return home() / "state.json"


def load_state() -> dict[str, Any]:
    try:
        return json.loads(_state_path().read_text())
    except (OSError, ValueError):
        return {}


def save_state(state: dict[str, Any]) -> None:
    p = _state_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


# ------------------------------------------------------------------ the key

def device_key() -> tuple[bytes, bytes]:
    """(seed, public key), created on first use with mode 0600."""
    p = home() / "device.ed25519"
    if not p.exists():
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(os.urandom(32))
    seed = p.read_bytes()
    if len(seed) != 32:
        raise TrustError(f"{p} is not a 32-byte Ed25519 seed")
    return seed, signing.public_key(seed)


def device_id(pub: bytes | None = None) -> str:
    pub = pub or device_key()[1]
    return "dev-" + hashlib.sha256(pub).hexdigest()[:16]


def platform_name() -> str:
    s = _platform.system().lower()
    return {"darwin": "macos"}.get(s, s)


def signed_payload(purpose: str, dev: str, ts: float, nonce: str,
                   body: dict[str, Any]) -> bytes:
    """Must equal the node's `control.devices.signed_payload` byte for byte."""
    return signing.canonical({"purpose": purpose, "device_id": dev,
                              "ts": round(float(ts), 3), "nonce": nonce,
                              "body": body})


def envelope(purpose: str, body: dict[str, Any]) -> dict[str, Any]:
    seed, pub = device_key()
    dev = device_id(pub)
    # Strictly increasing, so two requests in one millisecond still order.
    st = load_state()
    ts = max(round(time.time(), 3), round(float(st.get("last_ts", 0)) + 0.001, 3))
    st["last_ts"] = ts
    save_state(st)
    nonce = secrets.token_hex(8)
    sig = signing.sign(seed, signed_payload(purpose, dev, ts, nonce, body))
    return {"device_id": dev, "ts": ts, "nonce": nonce, "body": body,
            "sig": sig.hex()}


# ------------------------------------------------------------ chained log

def entry_hash(prev: str, entry: dict[str, Any]) -> str:
    """Must equal the node's `control.anchors.entry_hash`."""
    core = {"seq": int(entry["seq"]), "ts": round(float(entry["ts"]), 6),
            "kind": str(entry["kind"]), "data": entry.get("data")}
    return hashlib.sha256(prev.encode("ascii") + b"\x1f"
                          + signing.canonical(core)).hexdigest()


def _log_path() -> Path:
    return home() / "log.jsonl"


def log(kind: str, data: Any = None) -> dict[str, Any]:
    """Append one signed, chained entry. Never raises for a full disk silently:
    a log that cannot be written is a policy failure the caller must see."""
    seed, _pub = device_key()
    st = load_state()
    chain = st.get("chain") or {"seq": 0, "head": GENESIS}
    e = {"seq": chain["seq"] + 1, "ts": round(time.time(), 6), "kind": kind,
         "data": data}
    h = entry_hash(chain["head"], e)
    e.update({"prev_hash": chain["head"], "hash": h,
              "sig": signing.sign(seed, bytes.fromhex(h)).hex()})
    with _log_path().open("a") as fh:
        fh.write(json.dumps(e, sort_keys=True) + "\n")
    st = load_state()
    st["chain"] = {"seq": e["seq"], "head": h}
    save_state(st)
    return e


def read_log(after_seq: int = 0) -> list[dict[str, Any]]:
    out = []
    try:
        with _log_path().open() as fh:
            for line in fh:
                line = line.strip()
                if line:
                    e = json.loads(line)
                    if int(e["seq"]) > after_seq:
                        out.append(e)
    except FileNotFoundError:
        pass
    return out


def verify_local_log() -> dict[str, Any]:
    """Recompute this device's own chain (what the node will check)."""
    _seed, pub = device_key()
    prev, n = GENESIS, 0
    for e in read_log():
        n += 1
        if int(e["seq"]) != n or e.get("prev_hash") != prev:
            return {"ok": False, "at": e.get("seq"), "reason": "chain broken"}
        if entry_hash(prev, e) != e["hash"]:
            return {"ok": False, "at": e["seq"], "reason": "entry edited"}
        if not signing.verify(pub, bytes.fromhex(e["hash"]), bytes.fromhex(e["sig"])):
            return {"ok": False, "at": e["seq"], "reason": "bad signature"}
        prev = e["hash"]
    return {"ok": True, "entries": n, "head": prev}


# --------------------------------------------------------------- node calls

def enrol(api: Any, *, name: str = "", profile: dict[str, Any] | None = None,
          egress_report: dict[str, Any] | None = None,
          attestation: dict[str, Any] | None = None) -> dict[str, Any]:
    _seed, pub = device_key()
    body = {"name": name or _platform.node(), "platform": platform_name(),
            "public_key": pub.hex(), "key_kind": "software",
            "profile": profile or {}, "egress_report": egress_report or {},
            "attestation": attestation or {"kind": "self-report"}}
    out = api.post("/api/devices/enrol", envelope("enrol", body))
    st = load_state()
    st.update({"device_id": out["device"]["id"], "node_url": out.get("node_url"),
               "node_key": out["node"]["public_key"],
               "node_fingerprint": out["node"]["fingerprint"],
               "domain": out["node"]["domain"], "principal": out["device"]["principal"],
               "tuf": {"root": out["tuf_root"]["signed"], "timestamp_version": 0,
                       "snapshot_version": 0, "targets_version": 0},
               "sealed": False})
    save_state(st)
    if isinstance(out.get("lease"), dict) and out["lease"].get("token"):
        _store_lease(out["lease"])
    log("enrolled", {"device_id": out["device"]["id"], "grade": out["device"]["grade"],
                     "node_fingerprint": out["node"]["fingerprint"],
                     "tuf_root_version": out["tuf_root"]["signed"]["version"]})
    return out


def _store_lease(lease: dict[str, Any]) -> dict[str, Any]:
    st = load_state()
    doc = lease["document"]
    from .vendor import tuf_verify
    if not tuf_verify.verify_signed_document(doc, st.get("node_key", ""), "lease"):
        raise TrustError("the lease is not signed by the node this device enrolled "
                         "with; refusing it")
    st["lease"] = {"token": lease["token"], "document": doc}
    st["sealed"] = False
    save_state(st)
    log("lease", {"lease_id": doc["lease"]["lease_id"], "mode": doc["lease"]["mode"],
                  "grade": doc["lease"]["grade"],
                  "expires_at": doc["lease"]["expires_at"]})
    return doc["lease"]


def renew(api: Any, *, mode: str | None = None,
          egress_report: dict[str, Any] | None = None) -> dict[str, Any]:
    st = load_state()
    if not st.get("device_id"):
        raise TrustError("this device is not enrolled; run `clawcal device enrol`")
    body: dict[str, Any] = {"mode": mode or (lease_body() or {}).get("mode",
                                                                      "attached")}
    if egress_report is not None:
        body["egress_report"] = egress_report
    man = st.get("manifest") or {}
    if man.get("sha256") and man.get("mode") == body["mode"]:
        body["manifest_sha256"] = man["sha256"]
    env = envelope("renew", body)
    env_out = api.post("/api/lease/renew", dict(env, device_id=st["device_id"]))
    return _store_lease(env_out)


def report(api: Any, body: dict[str, Any]) -> dict[str, Any]:
    st = load_state()
    out = api.post(f"/api/devices/{st['device_id']}/report", envelope("report", body))
    log("reported", {k: (v.get("active") if k == "egress_report" else True)
                     for k, v in body.items() if isinstance(v, dict)})
    return out


def sync(api: Any) -> dict[str, Any]:
    """Upload the log segment since the node's anchor."""
    st = load_state()
    anchor = api.get(f"/api/devices/{st['device_id']}/log?limit=1")["anchor"]
    if anchor.get("rebase_pending"):
        # An operator re-baselined this device after a tamper event. History
        # since the old anchor is not re-sent — it cannot be made honest by
        # re-signing — the chain continues from here, acknowledged and signed.
        entries = [log("rebase_ack", {"node_anchor_seq": anchor["seq"]})]
    else:
        entries = read_log(after_seq=int(anchor["seq"]))
    body = {"entries": entries}
    return api.post(f"/api/devices/{st['device_id']}/log", envelope("sync", body))


# ------------------------------------------------------------------- lease

def lease_body() -> dict[str, Any] | None:
    lease = load_state().get("lease") or {}
    return (lease.get("document") or {}).get("lease")


def lease_status(now: float | None = None) -> dict[str, Any]:
    """ACTIVE, IN_GRACE, SEALED or UNLEASED — checked offline, from the
    node-signed document."""
    now = now or time.time()
    st = load_state()
    body = lease_body()
    if not st.get("device_id"):
        return {"status": "UNENROLLED"}
    if st.get("sealed"):
        return {"status": "SEALED", "reason": st.get("sealed_reason", "")}
    if not body:
        return {"status": "UNLEASED"}
    from .vendor import tuf_verify
    if not tuf_verify.verify_signed_document(st["lease"]["document"],
                                             st.get("node_key", ""), "lease"):
        return {"status": "SEALED", "needs_seal": True,
                "reason": "the stored lease no longer verifies against the node's key"}
    status = ("ACTIVE" if now <= body["expires_at"] else
              "IN_GRACE" if now <= body["grace_until"] else "EXPIRED")
    return {"status": status, "mode": body["mode"], "grade": body["grade"],
            "clearance": body.get("clearance"),
            "execute_local": body.get("execute_local"),
            "expires_in_s": round(body["expires_at"] - now),
            "grace_until": body["grace_until"], "lease_id": body["lease_id"]}


ORG_FILES = ("manifest.json", "harness", "org")


def seal(reason: str) -> list[str]:
    """Lease gone: remove org configuration; keep only what is public (§10.3).

    The data-encryption key of an exported instrument slice would be wiped
    here too; v2 exports none by default, so there is none to wipe.
    """
    removed = []
    for name in ORG_FILES:
        p = home() / name
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
            removed.append(name)
        elif p.exists():
            p.unlink()
            removed.append(name)
    st = load_state()
    st.update({"sealed": True, "sealed_reason": reason, "sealed_at": time.time()})
    st.pop("manifest", None)
    st.get("lease", {}).pop("token", None)
    save_state(st)
    log("sealed", {"reason": reason, "removed": removed})
    return removed


def enforce_lease() -> dict[str, Any]:
    """Called before anything that needs org configuration. Seals on expiry."""
    s = lease_status()
    if s["status"] == "EXPIRED" or s.get("needs_seal"):
        seal(s.get("reason") or "the lease expired past its grace window")
        s = lease_status()
    if s["status"] in ("SEALED", "UNENROLLED", "UNLEASED"):
        raise TrustError(f"this device is {s['status'].lower()}"
                         + (f": {s.get('reason')}" if s.get("reason") else "")
                         + ". Only public capabilities remain; reach the node and "
                           "run `clawcal device renew`")
    return s


def device_headers() -> dict[str, str]:
    st = load_state()
    token = (st.get("lease") or {}).get("token")
    if not (st.get("device_id") and token):
        return {}
    return {"X-ClawCal-Device": st["device_id"], "X-ClawCal-Lease": token}
