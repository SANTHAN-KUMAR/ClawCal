"""The client's side of the bundle store (sovereign-workbench-v2.md §9, §10.4).

    update     fetch timestamp → snapshot → targets from the node and verify them
               against the root pinned at enrolment: rollback, freeze, mix-and-
               match and forgery are refused, never merely warned about
    fetch      download a target and accept it only if its length and SHA-256
               match the signed entry — any channel is acceptable, the hash is
               the control
    launch     at every start: the manifest verifies against the trusted
               targets, its weights sample-verify against the fingerprint taken
               at install (full re-hash on demand), and no `never` flag is
               present. A mismatch refuses to start and is logged for the node.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Any

from . import trust
from .vendor import tuf_verify


class BundleError(RuntimeError):
    pass


def update(api: Any) -> dict[str, Any]:
    """Refresh trusted metadata from the node. Returns the new trusted state."""
    st = trust.load_state()
    tuf = st.get("tuf")
    if not tuf:
        raise BundleError("no pinned root; enrol this device first")

    def fetch_root(v: int) -> dict[str, Any] | None:
        try:
            return api.get(f"/api/bundles/metadata/{v}.root.json")
        except Exception:                                  # noqa: BLE001
            return None
    root = tuf_verify.update_root(tuf["root"], fetch_root)
    ts = api.get("/api/bundles/metadata/timestamp.json")
    snap = api.get("/api/bundles/metadata/snapshot.json")
    tg = api.get("/api/bundles/metadata/targets.json")
    try:
        new = tuf_verify.check_update(dict(tuf, root=root), ts, snap, tg)
    except tuf_verify.RepoError as exc:
        trust.log("bundle_refused", {"error": type(exc).__name__,
                                     "detail": str(exc)[:300]})
        raise BundleError(f"{type(exc).__name__}: {exc}") from None
    st = trust.load_state()
    st["tuf"] = {k: new[k] for k in ("root", "timestamp_version", "snapshot_version",
                                     "targets_version")}
    (trust.home() / "targets.json").write_text(json.dumps(tg, sort_keys=True))
    trust.save_state(st)
    trust.log("bundle_updated", {"root": root["version"],
                                 "targets": new["targets_version"]})
    return new


def _trusted_targets() -> dict[str, Any]:
    """The targets role as last verified (re-verified against the pinned root)."""
    st = trust.load_state()
    try:
        env = json.loads((trust.home() / "targets.json").read_text())
    except (OSError, ValueError):
        raise BundleError("no verified targets metadata; run `clawcal device "
                          "update`") from None
    signed = tuf_verify.verify_role(env, "targets", st["tuf"]["root"],
                                    check_expiry=False)
    if int(signed["version"]) != int(st["tuf"]["targets_version"]):
        raise BundleError("the stored targets metadata is not the version this "
                          "device trusted")
    return signed


def fetch(api: Any, path: str, dest: Path, *, chunk: int = 1 << 22) -> dict[str, Any]:
    """Download a target, verifying length and SHA-256 while streaming."""
    targets = _trusted_targets()
    info = tuf_verify.target_info(targets, path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    headers = {"Authorization": f"Bearer {api.token}"} if api.token else {}
    headers.update(trust.device_headers())
    req = urllib.request.Request(f"{api.url}/api/bundles/targets/{path}",
                                 headers=headers)
    h, n = hashlib.sha256(), 0
    with urllib.request.urlopen(req, timeout=60) as r, tmp.open("wb") as fh:
        while True:
            b = r.read(chunk)
            if not b:
                break
            n += len(b)
            if n > int(info["length"]):
                raise BundleError(f"{path}: more bytes than the signed length")
            h.update(b)
            fh.write(b)
    if n != int(info["length"]) or h.hexdigest() != info["hashes"]["sha256"]:
        tmp.unlink(missing_ok=True)
        trust.log("target_refused", {"path": path, "bytes": n})
        raise BundleError(f"{path}: the download does not match its signed hash; "
                          f"discarded")
    tmp.replace(dest)
    return info


def install_manifest(api: Any, *, mode: str = "attached",
                     model: str | None = None) -> dict[str, Any]:
    """Ask the node to classify this device, then fetch and verify the manifest."""
    trust.enforce_lease()
    st = trust.load_state()
    out = api.post(f"/api/devices/{st['device_id']}/manifest",
                   {"mode": mode, **({"model": model} if model else {})})
    if not out.get("issued"):
        trust.log("manifest_refused", {"reason": out.get("reason")})
        return out
    update(api)
    dest = trust.home() / "manifest.json"
    fetch(api, out["target"], dest)
    body = json.loads(dest.read_text())
    st = trust.load_state()
    st["manifest"] = {"target": out["target"], "sha256": out["sha256"],
                      "mode": mode, "device_class": body["device_class"],
                      "model": body["model"]["id"]}
    trust.save_state(st)
    trust.log("manifest_installed", {"target": out["target"], "sha256": out["sha256"],
                                     "device_class": body["device_class"],
                                     "model": body["model"]["id"]})
    install_harness(api)
    return {"issued": True, "manifest": body, "target": out["target"]}


def install_weights(api: Any, dest_dir: Path | None = None) -> dict[str, Any]:
    """Fetch the manifest's weights over the LAN channel and fingerprint them."""
    man = load_manifest()
    art = man["model"]["artifacts"][0]
    dest = (dest_dir or trust.home() / "models") / Path(art["path"]).name
    if not (dest.exists() and dest.stat().st_size == art["size"]):
        fetch(api, art["path"], dest)
    tuf_verify.verify_target_file(_trusted_targets(), art["path"], dest)
    seed = art["sha256"]
    st = trust.load_state()
    st["weights"] = {"path": str(dest), "sha256": art["sha256"],
                     "sample": tuf_verify.sample_digest(dest, seed),
                     "verified_at": time.time()}
    trust.save_state(st)
    trust.log("weights_installed", {"sha256": art["sha256"], "bytes": art["size"]})
    install_engine(api)
    return trust.load_state()["weights"]


def install_harness(api: Any) -> dict[str, Any] | None:
    """The organisation's own opencode build, if the manifest names one."""
    man = load_manifest()
    art = man.get("harness")
    if not art:
        return None
    dest = trust.home() / "harness-bin" / f"opencode-{art['sha256'][:16]}"
    if not (dest.exists() and hashlib.sha256(dest.read_bytes()).hexdigest()
            == art["sha256"]):
        fetch(api, art["target"], dest)
        os.chmod(dest, 0o755)
    st = trust.load_state()
    st["harness_build"] = {"binary": str(dest), "sha256": art["sha256"],
                           "version": art.get("version")}
    trust.save_state(st)
    trust.log("harness_installed", {"target": art["target"], "sha256": art["sha256"]})
    return st["harness_build"]


def install_engine(api: Any) -> dict[str, Any] | None:
    """Fetch the engine build the manifest names, verify it, unpack it safely.

    Client binaries travel only on the LAN channel (§9.2). The archive is
    accepted only if its hash matches the signed target; every member must be a
    plain file or directory below the destination — no links, no absolute
    paths, no `..`."""
    import tarfile
    man = load_manifest()
    art = (man.get("engine") or {}).get("artifact")
    if not art:
        return None
    dest = trust.home() / "engines" / art["sha256"][:16]
    binary = dest / art["binary"]
    if not binary.exists():
        archive = trust.home() / "engines" / f"{art['sha256'][:16]}.tar.gz"
        fetch(api, art["target"], archive)
        dest.mkdir(parents=True, exist_ok=True)
        root = dest.resolve()
        with tarfile.open(archive) as tar:
            for m in tar.getmembers():
                target = (dest / m.name).resolve()
                if not (m.isfile() or m.isdir()) or not target.is_relative_to(root):
                    raise BundleError(f"engine archive member {m.name!r} is not a "
                                      f"plain file below the engine directory")
            tar.extractall(dest, filter="data")
        archive.unlink(missing_ok=True)
        os.chmod(binary, 0o755)
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    st = trust.load_state()
    st["engine_build"] = {"binary": str(binary), "sha256": digest,
                          "target": art["target"], "variant": art.get("variant")}
    trust.save_state(st)
    trust.log("engine_installed", {"target": art["target"], "binary_sha256": digest})
    return st["engine_build"]


def load_manifest() -> dict[str, Any]:
    trust.enforce_lease()
    p = trust.home() / "manifest.json"
    if not p.exists():
        raise BundleError("no manifest installed; run `clawcal device manifest`")
    return json.loads(p.read_text())


def verify_launch(extra_flags: list[str] | None = None, *,
                  full: bool = False) -> dict[str, Any]:
    """Everything checked at each start. Raises BundleError to refuse."""
    lease = trust.enforce_lease()
    st = trust.load_state()
    man_info = st.get("manifest") or {}
    targets = _trusted_targets()
    data = (trust.home() / "manifest.json").read_bytes()
    try:
        tuf_verify.verify_target_bytes(targets, man_info["target"], data)
    except (tuf_verify.RepoError, KeyError) as exc:
        trust.log("launch_refused", {"reason": f"manifest: {exc}"})
        raise BundleError(f"the installed manifest does not verify: {exc}") from None
    man = json.loads(data)
    if man["device_id"] != st["device_id"]:
        raise BundleError("the manifest was issued to another device")
    lease_man = (st.get("lease") or {}).get("document", {}).get("lease", {}).get(
        "manifest_sha256")
    if man["mode"] == "detached" and lease_man != man_info.get("sha256"):
        raise BundleError("the detached lease is not bound to this manifest; renew "
                          "the lease after installing the manifest")
    flags = list(man["engine"]["flags"]) + list(extra_flags or [])
    bad = [f for f in flags for n in man["engine"]["never"]
           if f == n or f.startswith(n + "=")]
    if bad:
        trust.log("launch_refused", {"reason": "never-listed flag", "flags": bad})
        raise BundleError(f"flag(s) {bad} are on this manifest's `never` list")
    weights = st.get("weights") or {}
    wcheck = "not installed"
    if weights:
        p = Path(weights["path"])
        if not p.exists():
            raise BundleError(f"weights missing at {p}")
        if full:
            tuf_verify.verify_target_file(targets, man["model"]["artifacts"][0]["path"], p)
            wcheck = "full SHA-256"
        elif tuf_verify.sample_digest(p, weights["sha256"]) != weights["sample"]:
            trust.log("launch_refused", {"reason": "weights sample mismatch"})
            raise BundleError("the weights file changed since it was verified "
                              "(sample digest mismatch); re-install it")
        else:
            wcheck = "sampled (header + 8 chunks)"
    hb = st.get("harness_build")
    if hb:
        p = Path(hb["binary"])
        if not p.exists() or hashlib.sha256(p.read_bytes()).hexdigest() != hb["sha256"]:
            trust.log("launch_refused", {"reason": "harness binary changed"})
            raise BundleError("the harness binary changed since it was verified; "
                              "re-install it with `clawcal device manifest`")
    eng = st.get("engine_build")
    if eng:
        p = Path(eng["binary"])
        if not p.exists() or hashlib.sha256(p.read_bytes()).hexdigest() != eng["sha256"]:
            trust.log("launch_refused", {"reason": "engine binary changed"})
            raise BundleError("the engine binary changed since it was verified; "
                              "re-install it with `clawcal device weights`")
    trust.log("launch_verified", {"manifest": man_info.get("sha256"),
                                  "weights": wcheck, "lease": lease["status"]})
    return {"manifest": man, "weights_check": wcheck, "lease": lease}
