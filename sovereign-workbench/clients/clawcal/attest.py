"""TPM attestation from the device (sovereign-workbench-v2.md §10.2).

Runs the standard `tpm2-tools` against this machine's TPM (or the one named by
CLAWCAL_TPM_TCTI, e.g. a software TPM for testing) and answers the node's
challenge: read the EK certificate, create an attestation key under the EK,
activate the node's credential, and quote the boot PCRs over the node's nonce.

Transient objects are flushed after every step: not every TPM access path has a
resource manager, and a TPM with full object slots refuses the next command.
The device learns nothing it could forge: the node computes the AK's name,
checks the EK chain and verifies the quote itself.
"""
from __future__ import annotations

import base64
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from . import trust

EK_CERT_INDICES = ("0x01c00002", "0x01c0000a")      # RSA, ECC (TCG EK profile)


class AttestError(RuntimeError):
    pass


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["TPM2TOOLS_TCTI"] = os.environ.get("CLAWCAL_TPM_TCTI", "device:/dev/tpmrm0")
    return env


def _t(args: list[str], cwd: str, *, ok_fail: bool = False) -> subprocess.CompletedProcess:
    r = subprocess.run(args, cwd=cwd, env=_env(), capture_output=True, text=True,
                       timeout=60)
    for flag in ("-t", "-s"):
        subprocess.run(["tpm2_flushcontext", flag], cwd=cwd, env=_env(),
                       capture_output=True, timeout=30)
    if r.returncode and not ok_fail:
        err = (r.stderr or r.stdout).strip().splitlines()
        raise AttestError(f"{args[0]} failed: {err[-1] if err else r.returncode}")
    return r


def attest(api: Any) -> dict[str, Any]:
    if not shutil.which("tpm2_quote"):
        raise AttestError("tpm2-tools is not installed on this device")
    st = trust.load_state()
    dev = st.get("device_id")
    if not dev:
        raise AttestError("enrol this device first")
    with tempfile.TemporaryDirectory() as w:
        for idx in EK_CERT_INDICES:
            r = _t(["tpm2_nvread", idx, "-o", "ek.der"], w, ok_fail=True)
            if r.returncode == 0 and Path(w, "ek.der").stat().st_size > 0:
                algo = "rsa" if idx.endswith("2") else "ecc"
                break
        else:
            err = (r.stderr or "").strip().splitlines()
            raise AttestError("no EK certificate in the TPM's NV storage"
                              + (f" ({err[-1]})" if err else "")
                              + "; some firmware TPMs publish it online instead, "
                                "which an air-gapped node cannot fetch")
        _t(["tpm2_createek", "-c", "ek.ctx", "-G", algo, "-u", "ek.pub"], w)
        _t(["tpm2_createak", "-C", "ek.ctx", "-c", "ak.ctx", "-G", "rsa", "-g",
            "sha256", "-s", "rsassa", "-u", "ak.pub", "-f", "tss", "-n", "ak.name"], w)
        ch = api.post(f"/api/devices/{dev}/attest/begin", trust.envelope(
            "attest-begin", {"ek_cert": base64.b64encode(
                Path(w, "ek.der").read_bytes()).decode(),
                "ak_public": base64.b64encode(Path(w, "ak.pub").read_bytes()).decode()}))
        Path(w, "cred.out").write_bytes(base64.b64decode(ch["credential"]))
        # The policy session must survive to ActivateCredential, so no flush
        # between these three.
        subprocess.run(["tpm2_startauthsession", "--policy-session", "-S", "s.ctx"],
                       cwd=w, env=_env(), capture_output=True, timeout=30)
        subprocess.run(["tpm2_policysecret", "-S", "s.ctx", "-c", "e"], cwd=w,
                       env=_env(), capture_output=True, timeout=30)
        r = subprocess.run(["tpm2_activatecredential", "-c", "ak.ctx", "-C", "ek.ctx",
                            "-i", "cred.out", "-o", "secret.bin", "-P",
                            "session:s.ctx"], cwd=w, env=_env(), capture_output=True,
                           text=True, timeout=60)
        subprocess.run(["tpm2_flushcontext", "s.ctx"], cwd=w, env=_env(),
                       capture_output=True, timeout=30)
        _t(["tpm2_flushcontext", "-t"], w, ok_fail=True)
        if r.returncode:
            raise AttestError("ActivateCredential failed: "
                              + (r.stderr.strip().splitlines() or ["?"])[-1])
        _t(["tpm2_quote", "-c", "ak.ctx", "-l", ch["pcrs"], "-q", ch["nonce"],
            "-m", "quote", "-s", "signature", "-o", "pcrs", "-g", "sha256"], w)
        b64 = lambda n: base64.b64encode(Path(w, n).read_bytes()).decode()  # noqa: E731
        out = api.post(f"/api/devices/{dev}/attest/finish", trust.envelope(
            "attest-finish", {"challenge_id": ch["challenge_id"],
                              "secret": b64("secret.bin"), "quote": b64("quote"),
                              "signature": b64("signature"), "pcrs": b64("pcrs")}))
    trust.log("attested", {"grade": out.get("grade"), "baseline": out.get("baseline")})
    return out
