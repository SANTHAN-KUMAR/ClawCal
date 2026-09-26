"""Client-side verification of the node's TUF-style repository (spec §9.1).

Standard library plus `signing` only: this file is vendored into the client
package, so a laptop verifies manifests, targets and root rotations with the
same code the node's tests run. It must never import the node.

Four roles, each an envelope `{"signed": {...}, "signatures": [...]}`:

    root       the keys and thresholds for every role; offline key; rotated by
               a new root signed by both the old and the new root keys
    targets    path -> length + sha256 (+ custom fields) for every file
    snapshot   the version of targets.json the repository currently serves
    timestamp  the current snapshot's version and hash; short expiry

What each check stops:

    rollback   a version lower than one already trusted  -> RollbackError
    freeze     an expired timestamp (a repository held at an old state)
                                                         -> FreezeError
    mix-and-match  a targets.json whose version the snapshot does not name, or
               a snapshot whose hash the timestamp does not carry -> MismatchError
    forgery    a signature below the role's threshold    -> SignatureError
    swap       a file whose length or hash differs       -> TargetError
"""
from __future__ import annotations

import hashlib
import time
from typing import Any, Callable

try:                                        # in the node's tree
    from .. import signing
except ImportError:                         # vendored beside signing.py
    from . import signing                   # type: ignore[no-redef]

SPEC_VERSION = "workbench-tuf/1"
ROLES = ("root", "targets", "snapshot", "timestamp")


class RepoError(Exception):
    pass


class SignatureError(RepoError):
    pass


class RollbackError(RepoError):
    pass


class FreezeError(RepoError):
    pass


class MismatchError(RepoError):
    pass


class TargetError(RepoError):
    pass


def meta_hash(envelope: dict[str, Any]) -> str:
    return hashlib.sha256(signing.canonical(envelope)).hexdigest()


def verify_role(envelope: dict[str, Any], role: str,
                root_signed: dict[str, Any], *, now: float | None = None,
                check_expiry: bool = True) -> dict[str, Any]:
    """Check an envelope's signatures against `root_signed` and return `signed`."""
    if not isinstance(envelope, dict) or "signed" not in envelope:
        raise SignatureError(f"{role}: not a signed envelope")
    signed = envelope["signed"]
    if signed.get("_type") != role:
        raise SignatureError(f"expected a {role} role, got {signed.get('_type')!r}")
    if signed.get("spec_version") != SPEC_VERSION:
        raise SignatureError(f"{role}: unsupported spec {signed.get('spec_version')!r}")
    spec = (root_signed.get("roles") or {}).get(role)
    if not spec:
        raise SignatureError(f"the trusted root defines no {role!r} role")
    keys = root_signed.get("keys") or {}
    msg = signing.canonical(signed)
    good: set[str] = set()
    for s in envelope.get("signatures") or []:
        kid = s.get("keyid")
        if kid not in spec.get("keyids", []) or kid in good:
            continue
        key = keys.get(kid) or {}
        try:
            pub = bytes.fromhex(key.get("public", ""))
            sig = bytes.fromhex(s.get("sig", ""))
        except ValueError:
            continue
        if key.get("keytype") == "ed25519" and signing.verify(pub, msg, sig):
            good.add(kid)
    if len(good) < int(spec.get("threshold", 1)):
        raise SignatureError(f"{role}: {len(good)} valid signature(s), threshold "
                             f"{spec.get('threshold', 1)}")
    if check_expiry and float(signed.get("expires", 0)) < (now or time.time()):
        raise FreezeError(f"{role} version {signed.get('version')} expired at "
                          f"{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(float(signed.get('expires', 0))))} UTC: "
                          f"the repository is frozen or its clock is wrong")
    return signed


def update_root(trusted_root: dict[str, Any],
                fetch: Callable[[int], dict[str, Any] | None],
                *, max_steps: int = 32) -> dict[str, Any]:
    """Walk root N -> N+1 -> ... Each new root must be signed by the *old*
    root's root keys and by its own; versions must step by exactly one."""
    current = trusted_root
    for _ in range(max_steps):
        nxt = fetch(int(current["version"]) + 1)
        if nxt is None:
            return current
        new = verify_role(nxt, "root", current, check_expiry=False)
        verify_role(nxt, "root", new, check_expiry=False)
        if int(new["version"]) != int(current["version"]) + 1:
            raise RollbackError(f"root version {new['version']} does not follow "
                                f"{current['version']}")
        current = new
    raise RepoError("too many root rotations in one update")


def check_update(state: dict[str, Any], timestamp: dict[str, Any],
                 snapshot: dict[str, Any], targets: dict[str, Any], *,
                 now: float | None = None) -> dict[str, Any]:
    """Verify fetched timestamp/snapshot/targets against trusted state.

    `state` holds `root` (signed dict) and the last trusted versions. Returns
    the new state; raises on rollback, freeze, mismatch or forgery.
    """
    root = state["root"]
    ts = verify_role(timestamp, "timestamp", root, now=now)
    if int(ts["version"]) < int(state.get("timestamp_version", 0)):
        raise RollbackError(f"timestamp version {ts['version']} is older than the "
                            f"trusted {state['timestamp_version']}")
    snap_meta = (ts.get("meta") or {}).get("snapshot.json") or {}
    if meta_hash(snapshot) != (snap_meta.get("hashes") or {}).get("sha256"):
        raise MismatchError("snapshot.json does not match the hash the timestamp "
                            "carries")
    snap = verify_role(snapshot, "snapshot", root, now=now)
    if int(snap["version"]) != int(snap_meta.get("version", -1)):
        raise MismatchError("snapshot version differs from the timestamp's")
    if int(snap["version"]) < int(state.get("snapshot_version", 0)):
        raise RollbackError(f"snapshot version {snap['version']} is older than the "
                            f"trusted {state['snapshot_version']}")
    tmeta = (snap.get("meta") or {}).get("targets.json") or {}
    tg = verify_role(targets, "targets", root, now=now)
    if int(tg["version"]) != int(tmeta.get("version", -1)):
        raise MismatchError(f"targets version {tg['version']} is not the "
                            f"{tmeta.get('version')} the snapshot names")
    if int(tg["version"]) < int(state.get("targets_version", 0)):
        raise RollbackError(f"targets version {tg['version']} is older than the "
                            f"trusted {state['targets_version']}")
    return {"root": root, "timestamp_version": int(ts["version"]),
            "snapshot_version": int(snap["version"]),
            "targets_version": int(tg["version"]), "targets": tg,
            "checked_at": now or time.time()}


def target_info(targets_signed: dict[str, Any], path: str) -> dict[str, Any]:
    info = (targets_signed.get("targets") or {}).get(path)
    if not info:
        raise TargetError(f"{path} is not a target of this repository")
    return info


def verify_target_bytes(targets_signed: dict[str, Any], path: str,
                        data: bytes) -> dict[str, Any]:
    info = target_info(targets_signed, path)
    if len(data) != int(info["length"]):
        raise TargetError(f"{path}: {len(data)} bytes, the signed length is "
                          f"{info['length']}")
    if hashlib.sha256(data).hexdigest() != info["hashes"]["sha256"]:
        raise TargetError(f"{path}: SHA-256 differs from the signed hash")
    return info


def verify_target_file(targets_signed: dict[str, Any], path: str, file_path: Any,
                       *, chunk: int = 1 << 22) -> dict[str, Any]:
    """Full hash of a file on disk against its target entry."""
    import os
    info = target_info(targets_signed, path)
    size = os.path.getsize(file_path)
    if size != int(info["length"]):
        raise TargetError(f"{path}: {size} bytes on disk, the signed length is "
                          f"{info['length']}")
    h = hashlib.sha256()
    with open(file_path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    if h.hexdigest() != info["hashes"]["sha256"]:
        raise TargetError(f"{path}: SHA-256 of the file on disk differs from the "
                          f"signed hash")
    return info


def sample_digest(file_path: Any, seed: str, *, samples: int = 8,
                  chunk: int = 1 << 20) -> str:
    """A cheap, reproducible fingerprint of a large file: its header plus
    `samples` chunks at offsets derived from `seed`. Used at launch so a 35 GB
    weight file is not re-hashed on every start (§10.4 item 1); the full hash
    runs at install and on a schedule."""
    import os
    size = os.path.getsize(file_path)
    h = hashlib.sha256(f"{size}:{seed}".encode())
    with open(file_path, "rb") as fh:
        h.update(fh.read(min(chunk, size)))
        for i in range(samples):
            off = int(hashlib.sha256(f"{seed}:{i}".encode()).hexdigest(), 16) % max(1, size)
            fh.seek(off)
            h.update(fh.read(chunk))
    return h.hexdigest()


def verify_signed_document(doc: dict[str, Any], public_key_hex: str,
                           body_key: str) -> bool:
    """A node-signed document (a lease, a manifest): {body_key: ..., signature}."""
    try:
        sig = doc["signature"]
        if sig.get("key") != public_key_hex:
            return False
        return signing.verify(bytes.fromhex(public_key_hex),
                              signing.canonical(doc[body_key]),
                              bytes.fromhex(sig["sig"]))
    except (KeyError, TypeError, ValueError):
        return False
