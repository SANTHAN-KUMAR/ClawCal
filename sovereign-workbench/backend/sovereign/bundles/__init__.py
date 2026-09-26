"""Distribution: the signed bundle store and the manifests it serves (§9).

    repo       the TUF-style repository (node side): keys, roles, targets
    verify     verification (client side, vendored into the client package)
    manifest   per-class and per-device manifests from the B5 profiler

This package is the only writer of the repository. The API serves it; the
client verifies it; nothing else touches it.
"""
from __future__ import annotations

import io
import time
import zipfile
from pathlib import Path
from typing import Any

from .. import db
from ..config import REPO_ROOT
from . import manifest, repo, slices, verify

CLIENT_SRC = REPO_ROOT / "clients" / "clawcal"
_VENDOR = {"_vendor/signing.py": REPO_ROOT / "backend" / "sovereign" / "signing.py",
           "_vendor/sealed.py": REPO_ROOT / "backend" / "sovereign" / "sealed.py",
           "_vendor/gatecore.py": REPO_ROOT / "backend" / "sovereign" / "evidence"
           / "gatecore.py",
           "_vendor/tuf_verify.py": Path(__file__).with_name("verify.py")}


def client_package() -> tuple[bytes, str]:
    """The client as one zip: the `clawcal` package with the node's signing
    and verification code vendored in, so a laptop verifies with the very code
    the node is tested with. Deterministic: same sources, same bytes."""
    buf = io.BytesIO()
    files: dict[str, bytes] = {}
    for p in sorted(CLIENT_SRC.rglob("*.py")):
        if "__pycache__" in p.parts or "_vendor" in p.parts:
            continue
        files["clawcal/" + p.relative_to(CLIENT_SRC).as_posix()] = p.read_bytes()
    for dest, src in _VENDOR.items():
        files["clawcal/" + dest] = src.read_bytes()
    files["clawcal/_vendor/__init__.py"] = b'"""Vendored from the node; do not edit."""\n'
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            z.writestr(info, files[name])
    data = buf.getvalue()
    return data, verify.meta_hash({"zip": data.hex()})[:12]


def publish_client(by: str = "system") -> dict[str, Any]:
    data, tag = client_package()
    path = "client/clawcal.zip"
    repo.add_targets([{"path": path, "data": data,
                       "custom": {"kind": "client", "build": tag,
                                  "built_at": round(time.time(), 3)}}], by=by)
    return {"path": path, "bytes": len(data), "build": tag}


def issue_manifest(device_id: str, *, mode: str, principal: Any,
                   model_id: str | None = None, node_url: str = "") -> dict[str, Any]:
    """Classify the device, pick its model, sign its manifest; record why."""
    from ..control import decisions, devices
    dev = devices.get(device_id)
    if dev["principal"] != principal.name and not principal.can("admin"):
        raise PermissionError(f"{device_id} belongs to {dev['principal']}")
    out = manifest.build(dev, mode=mode, model_id=model_id, node_url=node_url)
    summary = [{"model": c.get("model"), "device_class": c.get("device_class"),
                "shipped": c.get("shipped"), "licence_ok": c.get("licence_ok"),
                "reason": c.get("reason")} for c in out.get("classes", [])]
    if not out["issued"]:
        db.update("devices", "id", device_id, {
            "device_class": "NONE",
            "class_report": db.jdump({"device_class": "NONE", "model": "",
                                      "reason": out["reason"],
                                      "task_classes_allowed": [],
                                      "candidates": summary})})
        decisions.record("trust", "MANIFEST_REFUSED", out["reason"],
                         subject_kind="device", subject_id=device_id,
                         principal=principal.name, basis={"candidates": summary})
        return out
    m = out["manifest"]
    db.update("devices", "id", device_id, {
        "device_class": m["device_class"],
        "class_report": db.jdump({
            "device_class": m["device_class"], "model": m["model"]["id"],
            "engine": m["engine"]["name"], "mode": mode,
            "task_classes_allowed": m["policy"]["task_classes_allowed"],
            "manifest_target": out["target"], "manifest_sha256": out["sha256"],
            "reason": m["classification"]["reason"], "candidates": summary})})
    decisions.record("trust", "MANIFEST_ISSUED",
                     f"{device_id}: {m['device_class']} with {m['model']['id']} on "
                     f"{m['engine']['name']} ({mode}); "
                     f"{m['classification']['reason']}"[:1900],
                     subject_kind="device", subject_id=device_id,
                     principal=principal.name,
                     basis={"target": out["target"], "sha256": out["sha256"],
                            "candidates": summary})
    return out


__all__ = ["repo", "verify", "manifest", "slices", "client_package", "publish_client",
           "issue_manifest"]
