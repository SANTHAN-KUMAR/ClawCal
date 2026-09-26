"""The node's bundle store: a TUF-style repository (spec §9.1).

Behind the same port as everything else, at /api/bundles. Four roles with
their own Ed25519 keys under SOVEREIGN_DATA_DIR/keys/tuf/:

    root.ed25519        OFFLINE. Created at initialisation, used only to sign a
                        new root; the operator moves it off the node afterwards
                        (`scripts/bundle.py rotate-root --root-key PATH` brings
                        it back for a rotation).
    targets / snapshot / timestamp   online, used by the node.

Targets are the files a client may fetch: the client package, per-class and
per-device manifests, and model weights. Weights are not copied into the
repository — a target may point at a file already on the node (an Ollama blob
is named by its own SHA-256) and is streamed from there. The hash is computed
once and cached against the file's size and mtime.

Every change bumps targets → snapshot → timestamp. The timestamp has a short
expiry and is re-signed as it ages, so a client can tell a live repository
from one frozen at an old state.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

from .. import audit, signing
from ..config import DATA_DIR
from .verify import SPEC_VERSION, meta_hash, verify_role

REPO_DIR = DATA_DIR / "bundles"
META_DIR = REPO_DIR / "metadata"
TARGET_DIR = REPO_DIR / "targets"
KEY_DIR = DATA_DIR / "keys" / "tuf"
INDEX = REPO_DIR / "target-sources.json"
HASH_CACHE = REPO_DIR / "hash-cache.json"

ROOT_TTL_S = 365 * 86400
TARGETS_TTL_S = 90 * 86400
SNAPSHOT_TTL_S = 30 * 86400
TIMESTAMP_TTL_S = 86400

_lock = threading.RLock()


class RepoUnavailable(RuntimeError):
    pass


def _key(role: str, *, create: bool = True, path: Path | None = None
         ) -> tuple[bytes, bytes] | None:
    p = path or (KEY_DIR / f"{role}.ed25519")
    if not p.exists():
        if not create:
            return None
        KEY_DIR.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(os.urandom(32))
    seed = p.read_bytes()
    if len(seed) != 32:
        raise RepoUnavailable(f"{p} is not a 32-byte Ed25519 seed")
    return seed, signing.public_key(seed)


def _sign(signed: dict[str, Any], keys: list[tuple[bytes, bytes]]) -> dict[str, Any]:
    msg = signing.canonical(signed)
    return {"signed": signed,
            "signatures": [{"keyid": signing.key_id(pub),
                            "sig": signing.sign(seed, msg).hex()}
                           for seed, pub in keys]}


def _write(name: str, envelope: dict[str, Any]) -> None:
    META_DIR.mkdir(parents=True, exist_ok=True)
    tmp = META_DIR / f".{name}.tmp"
    tmp.write_text(json.dumps(envelope, indent=1, sort_keys=True))
    os.replace(tmp, META_DIR / name)


def load(name: str) -> dict[str, Any] | None:
    try:
        return json.loads((META_DIR / name).read_text())
    except (OSError, ValueError):
        return None


def _root_signed(root_keys: dict[str, tuple[bytes, bytes]], version: int
                 ) -> dict[str, Any]:
    keys, roles = {}, {}
    for role, (_seed, pub) in root_keys.items():
        kid = signing.key_id(pub)
        keys[kid] = {"keytype": "ed25519", "public": pub.hex()}
        roles[role] = {"keyids": [kid], "threshold": 1}
    from ..control import trust
    return {"_type": "root", "spec_version": SPEC_VERSION, "version": version,
            "expires": round(time.time() + ROOT_TTL_S, 3),
            "domain": trust.domain_name(), "keys": keys, "roles": roles}


def init() -> dict[str, Any]:
    """Create the repository on first use. Idempotent."""
    with _lock:
        root = load("root.json")
        if root:
            return root["signed"]
        roles = {r: _key(r) for r in ("root", "targets", "snapshot", "timestamp")}
        signed = _root_signed(roles, 1)                     # type: ignore[arg-type]
        env = _sign(signed, [roles["root"]])                # type: ignore[list-item]
        _write("1.root.json", env)
        _write("root.json", env)
        TARGET_DIR.mkdir(parents=True, exist_ok=True)
        _publish({})
        audit.record("bundles", "repository_initialised",
                     detail={"root_keyid": signing.key_id(roles["root"][1])})
        return signed


def trusted_root() -> dict[str, Any]:
    return (load("root.json") or {"signed": init()})["signed"]


def root_fingerprint() -> str:
    """What an operator reads aloud to a user enrolling a device offline."""
    return hashlib.sha256(signing.canonical(load("root.json") or {})).hexdigest()


def _targets_map() -> dict[str, Any]:
    env = load("targets.json")
    return dict((env or {}).get("signed", {}).get("targets") or {})


def _publish(targets: dict[str, Any]) -> None:
    """Sign a new targets → snapshot → timestamp chain."""
    tk, sk, xk = _key("targets"), _key("snapshot"), _key("timestamp")
    prev_t = (load("targets.json") or {}).get("signed", {}).get("version", 0)
    prev_s = (load("snapshot.json") or {}).get("signed", {}).get("version", 0)
    now = time.time()
    t_env = _sign({"_type": "targets", "spec_version": SPEC_VERSION,
                   "version": prev_t + 1, "expires": round(now + TARGETS_TTL_S, 3),
                   "targets": targets}, [tk])                 # type: ignore[list-item]
    s_env = _sign({"_type": "snapshot", "spec_version": SPEC_VERSION,
                   "version": prev_s + 1, "expires": round(now + SNAPSHOT_TTL_S, 3),
                   "meta": {"targets.json": {"version": prev_t + 1}}},
                  [sk])                                       # type: ignore[list-item]
    _write("targets.json", t_env)
    _write("snapshot.json", s_env)
    _timestamp(s_env, xk)                                     # type: ignore[arg-type]


def _timestamp(s_env: dict[str, Any], xk: tuple[bytes, bytes]) -> dict[str, Any]:
    prev = (load("timestamp.json") or {}).get("signed", {}).get("version", 0)
    env = _sign({"_type": "timestamp", "spec_version": SPEC_VERSION,
                 "version": prev + 1,
                 "expires": round(time.time() + TIMESTAMP_TTL_S, 3),
                 "meta": {"snapshot.json": {"version": s_env["signed"]["version"],
                                            "hashes": {"sha256": meta_hash(s_env)}}}},
                [xk])
    _write("timestamp.json", env)
    return env


def fresh_timestamp() -> dict[str, Any]:
    """The timestamp, re-signed if more than half its life has gone."""
    with _lock:
        init()
        env = load("timestamp.json")
        if env and float(env["signed"]["expires"]) - time.time() > TIMESTAMP_TTL_S / 2:
            return env
        return _timestamp(load("snapshot.json") or {}, _key("timestamp"))  # type: ignore[arg-type]


def metadata(name: str) -> dict[str, Any] | None:
    """A role file as served. `N.root.json` serves a historical root."""
    if name == "timestamp.json":
        return fresh_timestamp()
    init()
    if name in ("root.json", "targets.json", "snapshot.json") or (
            name.endswith(".root.json") and name.split(".")[0].isdigit()):
        return load(name)
    return None


# ------------------------------------------------------------------ targets

def _file_sha256(path: Path) -> str:
    """SHA-256 of a file, cached against (size, mtime) — weights are large."""
    st = path.stat()
    key = f"{path}:{st.st_size}:{int(st.st_mtime)}"
    try:
        cache = json.loads(HASH_CACHE.read_text())
    except (OSError, ValueError):
        cache = {}
    if key in cache:
        return cache[key]
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            b = fh.read(1 << 22)
            if not b:
                break
            h.update(b)
    digest = h.hexdigest()
    cache[key] = digest
    REPO_DIR.mkdir(parents=True, exist_ok=True)
    HASH_CACHE.write_text(json.dumps(cache, indent=1))
    return digest


def _sources() -> dict[str, str]:
    try:
        return json.loads(INDEX.read_text())
    except (OSError, ValueError):
        return {}


def add_targets(entries: list[dict[str, Any]], *, by: str = "system") -> dict[str, Any]:
    """Add or replace targets in one signed publication.

    Each entry: {"path": repo path, and one of "data": bytes | "file": path},
    plus optional "custom". A "file" is served from where it lies.
    """
    with _lock:
        init()
        targets = _targets_map()
        sources = _sources()
        for e in entries:
            path = str(e["path"]).lstrip("/")
            if ".." in Path(path).parts:
                raise ValueError(f"target path {path!r} escapes the repository")
            if "data" in e:
                data: bytes = e["data"]
                dest = TARGET_DIR / path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
                length, digest = len(data), hashlib.sha256(data).hexdigest()
                sources.pop(path, None)
            else:
                src = Path(e["file"]).resolve()
                length, digest = src.stat().st_size, _file_sha256(src)
                sources[path] = str(src)
            targets[path] = {"length": length, "hashes": {"sha256": digest},
                             "custom": e.get("custom") or {}}
        REPO_DIR.mkdir(parents=True, exist_ok=True)
        INDEX.write_text(json.dumps(sources, indent=1, sort_keys=True))
        _publish(targets)
    audit.record("bundles", "targets_published", actor=by,
                 detail={"paths": [str(e["path"]) for e in entries][:20]})
    return targets


def target_file(path: str) -> tuple[Path, dict[str, Any]] | None:
    """(file on disk, signed entry) for a target, or None."""
    path = path.lstrip("/")
    info = _targets_map().get(path)
    if not info:
        return None
    src = _sources().get(path)
    f = Path(src) if src else TARGET_DIR / path
    return (f, info) if f.exists() else None


def targets() -> dict[str, Any]:
    return _targets_map()


def rotate_root(root_key_path: Path, *, replace: list[str] | None = None,
                by: str = "system") -> dict[str, Any]:
    """Sign root N+1 with the old root key and the new one. `replace` names
    roles whose keys are regenerated (revoking a compromised online key)."""
    with _lock:
        old_env = load("root.json")
        if not old_env:
            raise RepoUnavailable("no repository to rotate")
        old = old_env["signed"]
        old_root = _key("root", create=False, path=root_key_path)
        if not old_root or signing.key_id(old_root[1]) not in old["roles"]["root"]["keyids"]:
            raise RepoUnavailable("that is not the current root key")
        replace = replace or []
        new_keys: dict[str, tuple[bytes, bytes]] = {}
        for role in ("root", "targets", "snapshot", "timestamp"):
            p = KEY_DIR / f"{role}.ed25519"
            if role in replace:
                if p.exists():
                    p.rename(p.with_suffix(f".retired-{int(time.time())}"))
                new_keys[role] = _key(role)                 # type: ignore[assignment]
            elif role == "root":
                new_keys[role] = old_root
            else:
                new_keys[role] = _key(role)                 # type: ignore[assignment]
        signed = _root_signed(new_keys, int(old["version"]) + 1)
        keys = [old_root] + ([new_keys["root"]] if "root" in replace else [])
        env = _sign(signed, keys)
        verify_role(env, "root", old, check_expiry=False)
        _write(f"{signed['version']}.root.json", env)
        _write("root.json", env)
        _publish(_targets_map())
    audit.record("bundles", "root_rotated", actor=by,
                 detail={"version": signed["version"], "replaced": replace})
    return signed
