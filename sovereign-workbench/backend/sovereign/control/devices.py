"""Member devices of the trust domain (sovereign-workbench-v2.md §10.2, §11).

A device is a key. It enrols by proving it holds the private half of an
Ed25519 key, and every later request that changes its standing (renewing a
lease, uploading its log, reporting its profile) is signed with that key. The
lease token it receives rides on ordinary requests; the key is what renews it.

The transport in front of the node (TLS on :8443, mTLS where the organisation
issues certificates) is a deployment concern. Identity is proved here, at the
application layer, so the same checks hold behind any terminator — and so that
every task, tool call and audit row names the user *and* the device, whatever
the transport was.

What a device says about itself is recorded, never trusted: its grade comes
from `trust.compute_grade`, from facts an admin or a verifier set.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from dataclasses import dataclass
from typing import Any

from .. import audit, db, signing
from . import decisions, trust
from .identity import AuthError, Forbidden, Principal

PLATFORMS = ("linux", "windows", "macos", "ios", "android")
KEY_KINDS = ("software", "tpm", "secure-enclave")

# Signed requests older or newer than this are refused (clock skew allowance).
MAX_SKEW_S = 300.0

DEVICE_HEADER = "X-ClawCal-Device"
LEASE_HEADER = "X-ClawCal-Lease"


class DeviceError(ValueError):
    """A device request that is well-formed but not acceptable."""


@dataclass(frozen=True)
class DeviceContext:
    """The device a request came from, as the node sees it."""
    device_id: str
    principal: str
    grade: str
    mode: str
    lease_id: str
    lease_expires_at: float
    device_class: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"device_id": self.device_id, "principal": self.principal,
                "grade": self.grade, "mode": self.mode, "lease_id": self.lease_id,
                "lease_expires_at": self.lease_expires_at,
                "device_class": self.device_class}


# ------------------------------------------------------------ signed requests

def signed_payload(purpose: str, device_id: str, ts: float, nonce: str,
                   body: dict[str, Any]) -> bytes:
    """The bytes a device signs. `purpose` stops a signature for one endpoint
    being replayed at another."""
    return signing.canonical({"purpose": purpose, "device_id": device_id,
                              "ts": round(float(ts), 3), "nonce": nonce,
                              "body": body})


def verify_signed(envelope: dict[str, Any], purpose: str, *,
                  public_key_hex: str | None = None,
                  replay_floor: float = 0.0) -> dict[str, Any]:
    """Check an envelope's signature, freshness and replay floor. Returns body."""
    for k in ("device_id", "ts", "nonce", "body", "sig"):
        if k not in envelope:
            raise DeviceError(f"signed request is missing {k!r}")
    if not isinstance(envelope["body"], dict):
        raise DeviceError("signed request body must be an object")
    ts = float(envelope["ts"])
    now = time.time()
    if abs(now - ts) > MAX_SKEW_S:
        raise DeviceError(f"signed request timestamp is {now - ts:+.0f} s from the "
                          f"node's clock, outside the ±{MAX_SKEW_S:.0f} s allowed; "
                          f"correct the device clock")
    if ts <= replay_floor:
        raise DeviceError("signed request is not newer than the last one accepted "
                          "from this device (replay refused)")
    pub_hex = public_key_hex
    if pub_hex is None:
        row = db.query_one("SELECT public_key FROM devices WHERE id=?",
                           (envelope["device_id"],))
        if not row:
            raise DeviceError(f"no device {envelope['device_id']!r}")
        pub_hex = row["public_key"]
    try:
        pub, sig = bytes.fromhex(pub_hex), bytes.fromhex(str(envelope["sig"]))
    except ValueError:
        raise DeviceError("public key or signature is not hex") from None
    msg = signed_payload(purpose, str(envelope["device_id"]), ts,
                         str(envelope["nonce"]), envelope["body"])
    if not signing.verify(pub, msg, sig):
        raise DeviceError(f"signature does not verify for {purpose!r}: the request "
                          f"was not made by the holder of this device's key")
    return envelope["body"]


def _accept_signed(device_id: str, envelope: dict[str, Any], purpose: str
                   ) -> tuple[dict[str, Any], dict[str, Any]]:
    """Verify a signed request from an enrolled device; advance its replay floor."""
    dev = get(device_id)
    if envelope.get("device_id") != device_id:
        raise DeviceError("the envelope names a different device")
    body = verify_signed(envelope, purpose, public_key_hex=dev["public_key"],
                         replay_floor=float(dev.get("last_nonce_ts") or 0))
    with db.tx() as c:
        cur = c.execute("UPDATE devices SET last_nonce_ts=?, last_seen_at=? "
                        "WHERE id=? AND last_nonce_ts < ?",
                        (float(envelope["ts"]), time.time(), device_id,
                         float(envelope["ts"])))
        if cur.rowcount != 1:
            raise DeviceError("a newer signed request from this device was accepted "
                              "concurrently (replay refused)")
    return dev, body


# ------------------------------------------------------------------ registry

def get(device_id: str) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM devices WHERE id=?", (device_id,))
    if not row:
        raise DeviceError(f"no device {device_id!r}")
    return dict(row)


def _public(dev: dict[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in dev.items() if k not in ("last_nonce_ts",)}
    for k in ("attestation", "egress_report", "profile", "class_report"):
        out[k] = db.jload(out.get(k), None)
    try:
        out["key_fingerprint"] = signing.fingerprint(bytes.fromhex(dev["public_key"]))
    except ValueError:
        out["key_fingerprint"] = "invalid"
    return out


def describe(device_id: str) -> dict[str, Any]:
    return _public(get(device_id))


def regrade(device_id: str, *, by: str = "system", why: str = "") -> tuple[str, str]:
    """Recompute and persist a device's grade; record a decision if it changed."""
    dev = get(device_id)
    grade, reason = trust.compute_grade(dev)
    if grade != dev.get("grade") or reason != dev.get("grade_reason"):
        db.update("devices", "id", device_id, {"grade": grade, "grade_reason": reason})
        if grade != dev.get("grade"):
            decisions.record("trust", f"GRADE_{grade}",
                             f"{device_id}: {dev.get('grade') or 'ungraded'} -> "
                             f"{grade}. {reason}" + (f" ({why})" if why else ""),
                             subject_kind="device", subject_id=device_id,
                             principal=by, basis={"from": dev.get("grade"),
                                                  "to": grade})
    return grade, reason


def enrol(principal: Principal, envelope: dict[str, Any]) -> dict[str, Any]:
    """Register a device. The envelope is signed by the key being enrolled.

    Body: name, platform, public_key, key_kind, and optionally profile,
    egress_report and attestation (recorded as claims, never as facts).
    """
    principal.require("engineer", "enrolling a device")
    body = envelope.get("body") or {}
    pub_hex = str(body.get("public_key", "")).lower()
    try:
        if len(bytes.fromhex(pub_hex)) != 32:
            raise ValueError
    except ValueError:
        raise DeviceError("public_key must be a 32-byte Ed25519 key in hex") from None
    platform = str(body.get("platform", "")).lower()
    if platform not in PLATFORMS:
        raise DeviceError(f"platform must be one of {PLATFORMS}")
    key_kind = str(body.get("key_kind", "software"))
    if key_kind not in KEY_KINDS:
        raise DeviceError(f"key_kind must be one of {KEY_KINDS}")
    device_id = "dev-" + hashlib.sha256(bytes.fromhex(pub_hex)).hexdigest()[:16]
    if envelope.get("device_id") != device_id:
        raise DeviceError(f"the envelope must name the device id derived from its "
                          f"key: {device_id}")
    # Proof of possession: the enrolling key signed this very request.
    verify_signed(envelope, "enrol", public_key_hex=pub_hex)
    held = db.query_one("SELECT COUNT(*) AS n FROM devices WHERE principal=? AND "
                        "state IN ('ACTIVE','PENDING','QUARANTINED')",
                        (principal.name,))["n"]
    cap = int(trust.policy().get("max_devices_per_principal", 5))
    if held >= cap:
        raise DeviceError(f"{principal.name} already holds {held} device(s), the "
                          f"domain's limit of {cap}; revoke one before enrolling "
                          f"another")
    if db.query_one("SELECT id FROM devices WHERE public_key=?", (pub_hex,)):
        raise DeviceError(f"this key is already enrolled as {device_id}; a revoked "
                          f"device must generate a new key to re-enrol")

    att = body.get("attestation") or {"kind": "self-report"}
    # A device may *present* attestation evidence; only a verifier on the node
    # marks it verified. Whatever the device sent in `verified` is discarded.
    att = {"kind": str(att.get("kind", "self-report"))[:40],
           "evidence": att.get("evidence"), "verified": False,
           "detail": "presented by the device; not yet verified"}
    pol = trust.policy()
    state = "ACTIVE" if pol.get("enrolment") == "auto" else "PENDING"
    now = time.time()
    db.insert("devices", {
        "id": device_id, "principal": principal.name,
        "name": str(body.get("name") or device_id)[:80], "platform": platform,
        "public_key": pub_hex, "key_kind": key_kind, "managed": 0,
        "attestation": db.jdump(att),
        "egress_report": db.jdump(_egress(body.get("egress_report"))),
        "profile": db.jdump(body.get("profile") or {}),
        "mode": "attached", "state": state,
        "state_reason": ("enrolled; active under the domain's auto-enrolment policy"
                         if state == "ACTIVE" else "awaiting an admin's approval"),
        "enrolled_at": now, "last_seen_at": now,
        "last_nonce_ts": float(envelope["ts"]),
        "approved_by": "policy:auto" if state == "ACTIVE" else None,
        "chain_seq": 0})
    grade, reason = regrade(device_id, by=principal.name, why="enrolment")
    decisions.record("trust", "ENROLLED" if state == "ACTIVE" else "ENROLMENT_PENDING",
                     f"{principal.name} enrolled {platform} device {device_id} "
                     f"({body.get('name') or 'unnamed'}), grade {grade}: {reason}",
                     subject_kind="device", subject_id=device_id,
                     principal=principal.name,
                     basis={"platform": platform, "key_kind": key_kind,
                            "attestation_kind": att["kind"], "grade": grade})
    audit.record("trust", "device_enrolled", outcome=state, actor=principal.name,
                 detail={"device_id": device_id, "platform": platform,
                         "grade": grade})
    return describe(device_id)


def _egress(report: Any) -> dict[str, Any]:
    """Normalise a client's egress self-check. Recorded as reported."""
    if not isinstance(report, dict):
        return {}
    return {"active": bool(report.get("active")),
            "enforcement": str(report.get("enforcement", ""))[:80],
            "mechanism": str(report.get("mechanism", ""))[:80],
            "reason": str(report.get("reason", ""))[:400],
            "probes": report.get("probes") if isinstance(report.get("probes"), list)
            else [],
            "checked_at": min(float(report.get("checked_at") or 0), time.time()),
            "reported_at": time.time()}


def report_state(device_id: str, envelope: dict[str, Any]) -> dict[str, Any]:
    """A signed status report: egress self-check and/or B5 profile."""
    dev, body = _accept_signed(device_id, envelope, "report")
    changes: dict[str, Any] = {}
    if "egress_report" in body:
        changes["egress_report"] = db.jdump(_egress(body["egress_report"]))
    if "profile" in body and isinstance(body["profile"], dict):
        changes["profile"] = db.jdump(body["profile"])
    if "attestation" in body and isinstance(body["attestation"], dict):
        a = body["attestation"]
        changes["attestation"] = db.jdump({
            "kind": str(a.get("kind", "self-report"))[:40],
            "evidence": a.get("evidence"), "verified": False,
            "detail": "presented by the device; not yet verified"})
    if changes:
        db.update("devices", "id", device_id, changes)
    grade, reason = regrade(device_id, by=dev["principal"], why="device report")
    audit.record("trust", "device_report", actor=dev["principal"],
                 detail={"device_id": device_id, "fields": sorted(changes),
                         "grade": grade})
    return describe(device_id)


# ------------------------------------------------------- admin and verifiers

def approve(device_id: str, admin: Principal) -> dict[str, Any]:
    admin.require("admin", "approving a device")
    dev = get(device_id)
    if dev["state"] != "PENDING":
        raise DeviceError(f"{device_id} is {dev['state']}, not PENDING")
    db.update("devices", "id", device_id, {"state": "ACTIVE", "approved_by": admin.name,
                                           "state_reason": f"approved by {admin.name}"})
    decisions.record("trust", "APPROVED", f"{admin.name} approved {device_id}",
                     subject_kind="device", subject_id=device_id,
                     principal=admin.name)
    return describe(device_id)


def set_managed(device_id: str, managed: bool, admin: Principal, *,
                mdm_attested: bool = False, note: str = "") -> dict[str, Any]:
    """An admin states the device is under organisational management.

    `mdm_attested` records that the organisation's MDM verified the device
    (Apple Managed Device Attestation, Intune compliance). The MDM talks to the
    vendor; the node records the result and who vouched for it (§10.2).
    """
    admin.require("admin", "marking a device managed")
    dev = get(device_id)
    changes: dict[str, Any] = {"managed": int(bool(managed))}
    if mdm_attested and managed:
        changes["attestation"] = db.jdump({
            "kind": "mdm", "verified": True, "verifier": admin.name,
            "detail": note or "MDM attestation recorded by an admin",
            "verified_at": time.time()})
    elif not managed:
        att = db.jload(dev.get("attestation"), {}) or {}
        if att.get("kind") == "mdm":
            changes["attestation"] = db.jdump({"kind": "self-report",
                                               "verified": False})
    db.update("devices", "id", device_id, changes)
    decisions.record("trust", "MANAGED" if managed else "UNMANAGED",
                     f"{admin.name} marked {device_id} "
                     f"{'managed' if managed else 'unmanaged'}"
                     + (" with MDM attestation" if mdm_attested else "")
                     + (f": {note}" if note else ""),
                     subject_kind="device", subject_id=device_id,
                     principal=admin.name)
    regrade(device_id, by=admin.name, why="management status changed")
    return describe(device_id)


class AttestationVerifier:
    """Seam for hardware attestation (§10.2). Keylime is the Linux reference:
    its registrar holds the TPM manufacturers' EK roots, so a quote verifies
    offline. Unconfigured, it refuses honestly rather than pretending."""
    name = "unconfigured"

    def verify(self, device: dict[str, Any], evidence: Any) -> tuple[bool, str]:
        return False, ("no TPM attestation verifier is configured on this node "
                       "(set SOVEREIGN_KEYLIME_VERIFIER to a local Keylime "
                       "verifier); the quote was recorded but not verified, so "
                       "the device stays at the grade its other facts allow")


class KeylimeVerifier(AttestationVerifier):
    """Asks a Keylime verifier on the node's own network for the agent's state.

    The agent id is the device id. Keylime's verifier reports whether the
    agent's quotes and measured boot are currently trusted.
    """
    name = "keylime"

    def __init__(self, url: str) -> None:
        self.url = url.rstrip("/")

    def verify(self, device: dict[str, Any], evidence: Any) -> tuple[bool, str]:
        import json
        import urllib.request
        agent = (evidence or {}).get("agent_id") or device["id"]
        try:
            with urllib.request.urlopen(f"{self.url}/v2.1/agents/{agent}",
                                        timeout=5) as r:
                data = json.loads(r.read())
        except Exception as exc:                        # noqa: BLE001
            return False, f"keylime verifier unreachable: {exc}"[:300]
        state = ((data.get("results") or {}).get("operational_state"))
        # Keylime operational states: 3 = Get Quote (attesting and trusted);
        # 7 = Failed, 9 = Invalid Quote, 10 = Tenant Quote Failed.
        ok = state in (3, "Get Quote")
        return ok, f"keylime operational_state={state}"


def verifier() -> AttestationVerifier:
    url = os.environ.get("SOVEREIGN_KEYLIME_VERIFIER", "").strip()
    return KeylimeVerifier(url) if url else AttestationVerifier()


def verify_attestation(device_id: str, admin: Principal,
                       ver: AttestationVerifier | None = None) -> dict[str, Any]:
    """Run the configured verifier over the evidence a device presented."""
    admin.require("admin", "verifying a device attestation")
    dev = get(device_id)
    att = db.jload(dev.get("attestation"), {}) or {}
    if att.get("kind") != "tpm-quote":
        raise DeviceError(f"{device_id} has presented {att.get('kind')!r} "
                          f"evidence, not a TPM quote")
    v = ver or verifier()
    ok, detail = v.verify(dev, att.get("evidence"))
    att.update({"verified": bool(ok), "verifier": v.name, "detail": detail,
                "verified_at": time.time()})
    db.update("devices", "id", device_id, {"attestation": db.jdump(att)})
    decisions.record("trust", "ATTESTED" if ok else "ATTESTATION_UNVERIFIED",
                     f"{v.name}: {detail}", subject_kind="device",
                     subject_id=device_id, principal=admin.name)
    regrade(device_id, by=admin.name, why="attestation checked")
    return describe(device_id)


def revoke(device_id: str, admin: Principal, reason: str = "") -> dict[str, Any]:
    """Revoke a device. Its lease stops working now; its renewal will fail.
    That is the whole revocation procedure (§10.3)."""
    admin.require("admin", "revoking a device")
    get(device_id)
    db.update("devices", "id", device_id, {
        "state": "REVOKED",
        "state_reason": f"revoked by {admin.name}" + (f": {reason}" if reason else "")})
    db.execute("UPDATE leases SET state='REVOKED' WHERE device_id=? AND "
               "state='ACTIVE'", (device_id,))
    db.execute("UPDATE slices SET state='REVOKED' WHERE device_id=?", (device_id,))
    # A revoked device cannot be reviewed back into the domain, so its open
    # tamper events are closed by the revocation — on the record, not dropped.
    db.execute("UPDATE tamper_events SET state='CLEARED', cleared_by=?, cleared_at=?, "
               "clear_reason='resolved by revocation of the device' "
               "WHERE device_id=? AND state='OPEN'", (admin.name, time.time(), device_id))
    decisions.record("trust", "REVOKED", f"{admin.name} revoked {device_id}"
                     + (f": {reason}" if reason else ""),
                     subject_kind="device", subject_id=device_id,
                     principal=admin.name)
    audit.record("trust", "device_revoked", outcome="REVOKED", actor=admin.name,
                 detail={"device_id": device_id, "reason": reason})
    return describe(device_id)


# ------------------------------------------------------------ request path

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def authenticate(device_id: str | None, lease_token: str | None,
                 principal: Principal) -> DeviceContext | None:
    """Resolve the device a request claims to come from, or None if it claims
    none. A claim that does not hold is an error, never a silent downgrade."""
    if not device_id and not lease_token:
        return None
    if not (device_id and lease_token):
        raise AuthError(f"a device request must carry both {DEVICE_HEADER} and "
                        f"{LEASE_HEADER}")
    digest = _token_hash(lease_token)
    row = db.query_one("SELECT l.*, d.state AS dstate, d.principal AS downer, "
                       "d.grade AS dgrade, d.device_class, d.state_reason "
                       "FROM leases l JOIN devices d ON d.id = l.device_id "
                       "WHERE l.token_hash=?", (digest,))
    if not row or not hmac.compare_digest(row["token_hash"], digest) \
            or row["device_id"] != device_id:
        raise AuthError("unknown lease for this device")
    if row["dstate"] == "REVOKED":
        raise AuthError(f"device {device_id} has been revoked: {row['state_reason']}")
    if row["dstate"] == "QUARANTINED":
        raise AuthError(f"device {device_id} is quarantined after a tamper event; "
                        f"an operator must clear it before it may re-attach")
    if row["dstate"] != "ACTIVE":
        raise AuthError(f"device {device_id} is {row['dstate']}")
    if row["state"] != "ACTIVE":
        raise AuthError(f"this lease is {row['state']}; renew it with the device key")
    if time.time() > row["expires_at"]:
        raise AuthError("this lease has expired; renew it with the device key")
    if row["downer"] != principal.name:
        raise Forbidden(f"device {device_id} is enrolled to {row['downer']}, not "
                        f"{principal.name}")
    grade = row["dgrade"] or "D"
    if not trust.rules_for(grade).get("attach"):
        raise Forbidden(f"grade {grade} may not attach under this domain's policy")
    db.execute("UPDATE devices SET last_seen_at=? WHERE id=? AND "
               "(last_seen_at IS NULL OR last_seen_at < ?)",
               (time.time(), device_id, time.time() - 30))
    return DeviceContext(device_id=device_id, principal=principal.name,
                         grade=grade, mode=row["mode"], lease_id=row["id"],
                         lease_expires_at=row["expires_at"],
                         device_class=row["device_class"])


# ------------------------------------------------------------------ roster

def roster(principal: Principal) -> list[dict[str, Any]]:
    """Every device, its grade, lease, last anchor and open tamper events.
    An engineer sees their own devices; an approver or admin sees all."""
    from . import anchors
    anchors.sweep_silent()
    if principal.can("approver"):
        rows = db.query("SELECT * FROM devices ORDER BY enrolled_at DESC")
    else:
        rows = db.query("SELECT * FROM devices WHERE principal=? "
                        "ORDER BY enrolled_at DESC", (principal.name,))
    out = []
    now = time.time()
    for r in rows:
        d = _public(dict(r))
        d.pop("profile", None)
        d.pop("class_report", None)
        lease = db.query_one("SELECT id, mode, grade, expires_at, grace_until, state "
                             "FROM leases WHERE device_id=? ORDER BY issued_at DESC "
                             "LIMIT 1", (r["id"],))
        if lease:
            lease = dict(lease)
            lease["status"] = (lease["state"] if lease["state"] != "ACTIVE" else
                               "ACTIVE" if now <= lease["expires_at"] else
                               "IN_GRACE" if now <= lease["grace_until"] else
                               "EXPIRED")
        d["lease"] = lease
        d["tamper_events"] = db.rows_to_dicts(db.query(
            "SELECT id, ts, kind, detail, blocking, state FROM tamper_events "
            "WHERE device_id=? AND state='OPEN' ORDER BY ts DESC", (r["id"],)))
        d["anchor_age_s"] = (now - r["anchored_at"]) if r["anchored_at"] else None
        out.append(d)
    return out
