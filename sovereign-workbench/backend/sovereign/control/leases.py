"""Leases: the revocable permission to be a member device (spec §10.3).

* **Issued** on enrolment and on every renewal, bound to the device key and the
  manifest hash; TTL 24 h attached, 14–30 days detached (policy); a grace window
  after which the client seals.
* **Renewed** by a request signed with the device key. Being "on the trusted
  network" is proven by reaching this endpoint with that key, never by an IP
  range.
* **Revoked** by revoking the device: the token stops working at once and the
  next renewal fails.

The lease document is signed with the node's Ed25519 key, so a detached client
can check it offline and knows exactly when it must seal. The token inside the
response is shown once; the node stores only its hash.
"""
from __future__ import annotations

import hashlib
import secrets
import time
from typing import Any

from .. import audit, db, signing
from ..config import DATA_DIR
from . import decisions, devices, trust
from .devices import DeviceError

KEYS_DIR = DATA_DIR / "keys"


def node_key() -> tuple[bytes, bytes]:
    """The node's signing key (the same appliance key that signs reports)."""
    return signing.appliance_key(KEYS_DIR)


def node_identity() -> dict[str, str]:
    _seed, pub = node_key()
    return {"public_key": pub.hex(), "fingerprint": signing.fingerprint(pub),
            "domain": trust.domain_name()}


def _refuse(device_id: str, reason: str, by: str) -> None:
    decisions.record("trust", "LEASE_REFUSED", reason, subject_kind="device",
                     subject_id=device_id, principal=by)
    raise DeviceError(reason)


def issue(device_id: str, *, mode: str = "attached",
          manifest_sha256: str | None = None, by: str = "system") -> dict[str, Any]:
    """Issue a lease for an ACTIVE device, superseding any earlier one."""
    if mode not in trust.MODES:
        raise DeviceError(f"mode must be one of {trust.MODES}")
    dev = devices.get(device_id)
    if dev["state"] == "QUARANTINED":
        _refuse(device_id, f"{device_id} is quarantined after a tamper event; an "
                           f"operator must clear it before it may re-attach", by)
    if dev["state"] != "ACTIVE":
        _refuse(device_id, f"{device_id} is {dev['state']}: {dev['state_reason']}", by)

    # The grade in the lease is the grade *for this mode*: an unmanaged device
    # that asks to go detached is graded as the detached device it will be.
    grade, reason = trust.compute_grade(dev, mode=mode)
    rules = trust.rules_for(grade)
    if not rules.get("attach"):
        _refuse(device_id, f"grade {grade} may not attach under this domain's "
                           f"policy ({reason})", by)
    if mode == "detached" and not rules.get("detached"):
        _refuse(device_id, f"grade {grade} may not run detached under this "
                           f"domain's policy ({reason}). Detached work needs a "
                           f"managed, attested device", by)

    pol = trust.policy()
    now = time.time()
    ttl = float(pol["lease_ttl_hours"][mode]) * 3600
    grace = float(pol.get("grace_hours", 24)) * 3600
    token = secrets.token_urlsafe(32)
    lease_id = db.new_id("lease")
    body = {
        "schema": "workbench.lease/v2", "lease_id": lease_id,
        "domain": trust.domain_name(), "device_id": device_id,
        "principal": dev["principal"], "device_key": dev["public_key"],
        "mode": mode, "grade": grade, "grade_reason": reason,
        "clearance": trust.clearance(grade),
        "execute_local": bool(rules.get("execute_local")),
        "manifest_sha256": manifest_sha256,
        "issued_at": round(now, 3), "expires_at": round(now + ttl, 3),
        "grace_until": round(now + ttl + grace, 3),
        "renew": {"path": "/api/lease/renew", "sign_with": "device key"},
    }
    seed, pub = node_key()
    document = {"lease": body,
                "signature": {"alg": "ed25519", "key": pub.hex(),
                              "sig": signing.sign(seed, signing.canonical(body)).hex()}}
    with db.tx() as c:
        c.execute("UPDATE leases SET state='SUPERSEDED' WHERE device_id=? AND "
                  "state='ACTIVE'", (device_id,))
        c.execute("INSERT INTO leases (id, device_id, principal, mode, grade, "
                  "manifest_sha256, token_hash, issued_at, expires_at, grace_until, "
                  "state, document) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (lease_id, device_id, dev["principal"], mode, grade,
                   manifest_sha256,
                   hashlib.sha256(token.encode()).hexdigest(), now, now + ttl,
                   now + ttl + grace, "ACTIVE", db.jdump(document)))
        c.execute("UPDATE devices SET mode=?, grade=?, grade_reason=? WHERE id=?",
                  (mode, grade, reason, device_id))
    decisions.record("trust", "LEASE_ISSUED",
                     f"{mode} lease for {device_id} at grade {grade}, "
                     f"{ttl / 3600:.0f} h + {grace / 3600:.0f} h grace",
                     subject_kind="lease", subject_id=lease_id, principal=by,
                     basis={"device_id": device_id, "grade": grade, "mode": mode,
                            "manifest_sha256": manifest_sha256})
    audit.record("trust", "lease_issued", actor=by,
                 detail={"device_id": device_id, "lease_id": lease_id,
                         "mode": mode, "grade": grade})
    return {"token": token, "document": document}


def renew(device_id: str, envelope: dict[str, Any]) -> dict[str, Any]:
    """Renew with a request signed by the device key.

    Body: mode, and optionally a fresh egress_report and manifest_sha256. A
    fresh egress report is recorded first, so the renewed lease carries the
    grade the device has now.
    """
    dev, body = devices._accept_signed(device_id, envelope, "renew")
    if "egress_report" in body:
        db.update("devices", "id", device_id,
                  {"egress_report": db.jdump(devices._egress(body["egress_report"]))})
    devices.regrade(device_id, by=dev["principal"], why="lease renewal")
    return issue(device_id, mode=str(body.get("mode") or dev["mode"] or "attached"),
                 manifest_sha256=body.get("manifest_sha256"), by=dev["principal"])


def verify_document(document: dict[str, Any], node_public_key_hex: str) -> bool:
    """Offline check of a lease document (what a client does)."""
    try:
        sig = document["signature"]
        if sig.get("key") != node_public_key_hex:
            return False
        return signing.verify(bytes.fromhex(node_public_key_hex),
                              signing.canonical(document["lease"]),
                              bytes.fromhex(sig["sig"]))
    except (KeyError, TypeError, ValueError):
        return False


def current(device_id: str) -> dict[str, Any] | None:
    row = db.query_one("SELECT id, mode, grade, issued_at, expires_at, grace_until, "
                       "state, manifest_sha256 FROM leases WHERE device_id=? "
                       "ORDER BY issued_at DESC LIMIT 1", (device_id,))
    return dict(row) if row else None
