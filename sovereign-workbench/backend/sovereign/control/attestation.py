"""TPM 2.0 attestation, verified on the node, offline (spec §10.2, §10.4, T11).

The review found attestation is a ladder whose rungs mostly need a vendor's
server — except TPM 2.0, which verifies with the vendors' EK roots held
locally. This is that rung, done with the standard tools (`tpm2-tools`,
`openssl`) and no network:

    begin    the device sends its EK certificate and its attestation key's
             TPM public area. The node checks the EK certificate chains to a
             vendor root the organisation installed; computes the AK's name
             itself from the public area (never taking the device's word for
             it); checks the AK is a restricted, TPM-bound signing key; and
             wraps a fresh secret to the EK for that AK name (MakeCredential —
             needs no TPM on the node).
    finish   the device can unwrap the secret only if that AK lives in the
             TPM whose EK was certified (ActivateCredential), and returns it
             with a quote of its PCRs over a nonce bound to its Ed25519 key.
             The node checks the secret, verifies the quote signature, nonce
             and PCR digest (`tpm2_checkquote`), and compares the PCRs with the
             baseline recorded at the first attestation: a device rooted while
             away fails on return (§10.4 item 4).

The EK is pinned at first attestation: a different TPM answering for the same
device is refused. An attestation only sets facts; the grade still needs an
admin's `managed`, and the policy still decides what the grade receives.
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import secrets
import shutil
import struct
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from .. import audit, db
from ..config import DATA_DIR
from . import decisions, devices
from .devices import DeviceError
from .identity import Principal

ROOTS_DIR = Path(os.environ.get("SOVEREIGN_TPM_ROOTS", DATA_DIR / "tpm-roots"))
CHALLENGE_TTL_S = 300.0
PCRS = "sha256:0,1,2,3,4,5,6,7"
TOOLS = ("openssl", "tpm2_makecredential", "tpm2_checkquote")

# TPMA_OBJECT bits an attestation key must carry.
FIXED_TPM, FIXED_PARENT, SENSITIVE_ORIGIN = 1 << 1, 1 << 4, 1 << 5
RESTRICTED, SIGN = 1 << 16, 1 << 18

_pending: dict[str, dict[str, Any]] = {}


def available() -> tuple[bool, str]:
    missing = [t for t in TOOLS if not shutil.which(t)]
    if missing:
        return False, f"the node lacks {', '.join(missing)} (install tpm2-tools)"
    roots = sorted(ROOTS_DIR.glob("*.pem")) if ROOTS_DIR.exists() else []
    if not roots:
        return False, (f"no TPM vendor roots installed in {ROOTS_DIR}; an admin adds "
                       f"the EK root and intermediate certificates of the TPMs the "
                       f"organisation buys")
    return True, f"{len(roots)} vendor root file(s)"


def add_root(pem: str, name: str, admin: Principal) -> dict[str, Any]:
    admin.require("admin", "installing a TPM vendor root")
    if "BEGIN CERTIFICATE" not in pem:
        raise DeviceError("that is not a PEM certificate")
    safe = re.sub(r"[^\w.-]", "_", name)[:60] or "root"
    ROOTS_DIR.mkdir(parents=True, exist_ok=True)
    p = ROOTS_DIR / f"{safe}.pem"
    p.write_text(pem.strip() + "\n")
    r = subprocess.run(["openssl", "x509", "-in", str(p), "-noout", "-subject",
                        "-fingerprint", "-sha256"], capture_output=True, text=True)
    if r.returncode != 0:
        p.unlink()
        raise DeviceError("openssl could not read that certificate")
    decisions.record("trust", "TPM_ROOT_ADDED", f"{admin.name} installed TPM vendor "
                     f"root {safe}: {r.stdout.strip()}", subject_kind="tpm_root",
                     subject_id=safe, principal=admin.name)
    return {"name": safe, "detail": r.stdout.strip()}


def _run(cmd: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=60)


def ak_name(public: bytes) -> tuple[str, int]:
    """(name hex, objectAttributes) from a TPM2B_PUBLIC.

    name = nameAlg ‖ H_nameAlg(TPMT_PUBLIC). Computed here so the node binds the
    credential to the key whose public area it will verify quotes with."""
    if len(public) < 12:
        raise DeviceError("the AK public area is too short")
    (size,) = struct.unpack(">H", public[:2])
    tpmt = public[2:2 + size]
    if len(tpmt) != size:
        raise DeviceError("the AK public area is truncated")
    _type, name_alg, attrs = struct.unpack(">HHI", tpmt[:8])
    if name_alg != 0x000B:
        raise DeviceError("the AK must use SHA-256 as its name algorithm")
    return "000b" + hashlib.sha256(tpmt).hexdigest(), attrs


def begin(device_id: str, envelope: dict[str, Any]) -> dict[str, Any]:
    ok, why = available()
    if not ok:
        raise DeviceError(f"TPM attestation is unavailable: {why}")
    dev, body = devices._accept_signed(device_id, envelope, "attest-begin")
    try:
        ek_der = base64.b64decode(body["ek_cert"])
        ak_pub = base64.b64decode(body["ak_public"])
    except (KeyError, ValueError):
        raise DeviceError("attest-begin needs ek_cert and ak_public (base64)") from None
    name, attrs = ak_name(ak_pub)
    need = FIXED_TPM | FIXED_PARENT | SENSITIVE_ORIGIN | RESTRICTED | SIGN
    if attrs & need != need:
        raise DeviceError("the AK is not a restricted, TPM-bound signing key; a quote "
                          "from it would prove nothing")
    prev = (db.jload(dev.get("attestation"), {}) or {})
    ek_fp = hashlib.sha256(ek_der).hexdigest()
    if prev.get("ek_sha256") and prev["ek_sha256"] != ek_fp:
        raise DeviceError("a different TPM is answering for this device than the one "
                          "pinned at its first attestation; re-enrol the device")
    with tempfile.TemporaryDirectory() as w:
        Path(w, "ek.der").write_bytes(ek_der)
        r = _run(["openssl", "x509", "-inform", "der", "-in", "ek.der", "-out",
                  "ek.pem"], w)
        if r.returncode:
            raise DeviceError("the EK certificate is not a valid X.509 certificate")
        bundle = Path(w, "roots.pem")
        bundle.write_text("".join(p.read_text() for p in sorted(ROOTS_DIR.glob("*.pem"))))
        r = _run(["openssl", "verify", "-partial_chain", "-CAfile", "roots.pem",
                  "ek.pem"], w)
        if r.returncode:
            _fail(device_id, "the EK certificate does not chain to an installed TPM "
                             "vendor root: " + (r.stderr or r.stdout).strip()[:200])
        subj = _run(["openssl", "x509", "-in", "ek.pem", "-noout", "-subject",
                     "-issuer"], w).stdout.strip().replace("\n", "; ")
        _run(["openssl", "x509", "-in", "ek.pem", "-pubkey", "-noout", "-out",
              "ekpub.pem"], w)
        algo = "ecc" if "EC" in _run(["openssl", "pkey", "-pubin", "-in", "ekpub.pem",
                                      "-noout", "-text"], w).stdout[:40] else "rsa"
        secret = secrets.token_bytes(32)
        Path(w, "secret.bin").write_bytes(secret)
        r = _run(["tpm2_makecredential", "-T", "none", "-u", "ekpub.pem", "-G", algo,
                  "-s", "secret.bin", "-n", name, "-o", "cred.out"], w)
        if r.returncode:
            raise DeviceError("MakeCredential failed: " + r.stderr.strip()[:200])
        cred = Path(w, "cred.out").read_bytes()
    cid = db.new_id("att")
    # The quote nonce is bound to this device's key: a quote replayed for
    # another device, or produced for another enrolment, does not verify.
    nonce = hashlib.sha256(secrets.token_bytes(16) + bytes.fromhex(
        dev["public_key"])).hexdigest()[:40]
    _pending[cid] = {"device_id": device_id, "secret": hashlib.sha256(secret).hexdigest(),
                     "nonce": nonce, "ak_public": ak_pub, "ek_sha256": ek_fp,
                     "ek_subject": subj, "expires": time.time() + CHALLENGE_TTL_S}
    audit.record("trust", "attestation_challenge", actor=dev["principal"],
                 detail={"device_id": device_id, "ek": subj[:200]})
    return {"challenge_id": cid, "credential": base64.b64encode(cred).decode(),
            "nonce": nonce, "pcrs": PCRS, "expires_in_s": CHALLENGE_TTL_S}


def _fail(device_id: str, reason: str) -> None:
    att = db.jload(devices.get(device_id).get("attestation"), {}) or {}
    att.update({"kind": "tpm-quote", "verified": False, "verifier": "tpm2-native",
                "detail": reason, "verified_at": time.time()})
    db.update("devices", "id", device_id, {"attestation": db.jdump(att)})
    decisions.record("trust", "ATTESTATION_FAILED", reason, subject_kind="device",
                     subject_id=device_id)
    devices.regrade(device_id, why="attestation failed")
    raise DeviceError(f"attestation failed: {reason}")


def finish(device_id: str, envelope: dict[str, Any]) -> dict[str, Any]:
    dev, body = devices._accept_signed(device_id, envelope, "attest-finish")
    ch = _pending.pop(str(body.get("challenge_id")), None)
    if not ch or ch["device_id"] != device_id or time.time() > ch["expires"]:
        raise DeviceError("unknown or expired attestation challenge; begin again")
    try:
        secret = base64.b64decode(body["secret"])
        parts = {k: base64.b64decode(body[k]) for k in ("quote", "signature", "pcrs")}
    except (KeyError, ValueError):
        raise DeviceError("attest-finish needs secret, quote, signature, pcrs") from None
    if hashlib.sha256(secret).hexdigest() != ch["secret"]:
        _fail(device_id, "the credential was not activated by the certified TPM: "
                         "the AK does not live in the TPM whose EK was presented")
    with tempfile.TemporaryDirectory() as w:
        Path(w, "ak.pub").write_bytes(ch["ak_public"])
        for k, v in parts.items():
            Path(w, k).write_bytes(v)
        r = _run(["tpm2_checkquote", "-u", "ak.pub", "-m", "quote", "-s", "signature",
                  "-f", "pcrs", "-g", "sha256", "-q", ch["nonce"]], w)
    if r.returncode:
        _fail(device_id, "the quote does not verify (signature, nonce or PCR digest): "
                         + (r.stderr or "").strip()[-200:])
    pcrs = dict(re.findall(r"^\s+(\d+)\s*:\s*0x([0-9A-Fa-f]+)", r.stdout, re.M))
    prev = db.jload(dev.get("attestation"), {}) or {}
    baseline = prev.get("pcr_baseline") if prev.get("ek_sha256") == ch["ek_sha256"] \
        else None
    if baseline:
        changed = sorted(int(k) for k, v in pcrs.items() if baseline.get(k) != v)
        if changed:
            _fail(device_id, f"measured boot state changed since the baseline: PCR "
                             f"{', '.join(map(str, changed))} differ. A device whose "
                             f"firmware, bootloader or secure-boot state changed must "
                             f"be re-baselined by an admin")
    att = {"kind": "tpm-quote", "verified": True, "verifier": "tpm2-native",
           "detail": f"EK {ch['ek_subject'][:160]}; quote over {PCRS} verified",
           "ek_sha256": ch["ek_sha256"], "ek_subject": ch["ek_subject"],
           "pcr_baseline": baseline or pcrs, "pcrs": pcrs, "verified_at": time.time()}
    db.update("devices", "id", device_id, {"attestation": db.jdump(att)})
    decisions.record("trust", "ATTESTED", att["detail"], subject_kind="device",
                     subject_id=device_id, principal=dev["principal"],
                     basis={"ek_sha256": ch["ek_sha256"], "pcrs": pcrs,
                            "baseline_set": not baseline})
    grade, reason = devices.regrade(device_id, by=dev["principal"],
                                    why="TPM attestation verified")
    return {"verified": True, "grade": grade, "grade_reason": reason,
            "baseline": "recorded" if not baseline else "matched", "pcrs": pcrs}


def reset_baseline(device_id: str, admin: Principal, reason: str) -> dict[str, Any]:
    admin.require("admin", "re-baselining a device's measured state")
    if not reason.strip():
        raise DeviceError("re-baselining needs a reason on the record")
    dev = devices.get(device_id)
    att = db.jload(dev.get("attestation"), {}) or {}
    att.pop("pcr_baseline", None)
    att["verified"] = False
    db.update("devices", "id", device_id, {"attestation": db.jdump(att)})
    decisions.record("trust", "PCR_BASELINE_RESET", f"{admin.name}: {reason}",
                     subject_kind="device", subject_id=device_id, principal=admin.name)
    devices.regrade(device_id, by=admin.name, why="baseline reset")
    return devices.describe(device_id)
