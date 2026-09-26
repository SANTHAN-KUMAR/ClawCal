"""Manifests: what a hardware class resolves to (spec §7.3).

Everything the profiler and the router decide about a device is in one signed
file the client verifies at every launch: the engine and its pinned flags, the
flags that must never appear, the model and the hash of every weight file,
where the bytes may come from, which task classes may run there, the local
sandbox, the egress allowlist and the lease terms.

The model catalogue is built from what the node actually holds: registry models
whose weights are on this host (Ollama keeps them content-addressed, so a blob's
name is its hash), plus weight files an admin adds by path. Model facts are
read from each file's GGUF header, never typed in.

Licences (§9.3): the node *redistributes* weights inside the organisation.
Apache-2.0 and MIT pass; Llama and Gemma terms carry conditions and are kept
out of shipped manifests unless an admin records a legal sign-off.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .. import db, profiler, signing
from ..config import DATA_DIR
from . import repo

CATALOGUE_EXTRA = DATA_DIR / "bundles" / "catalogue.json"
FACTS_CACHE = DATA_DIR / "bundles" / "facts-cache.json"

OPEN_LICENCES = ("apache-2.0", "apache 2.0", "mit", "bsd-3-clause", "bsd-2-clause")
CONDITIONAL = ("llama", "gemma")

# The representative task type whose capability floors a class must clear, and
# the agentic floor on top (RQ1/T12: small models fail multi-turn tool use).
CLASS_TASK = {"draft": "deliverable_drafting", "code": "coding",
              "calc": "calculation"}
AGENTIC_TOOL_USE_FLOOR = 0.6


def _facts(path: Path, model_id: str) -> profiler.ModelFacts:
    st = path.stat()
    key = f"{path}:{st.st_size}:{int(st.st_mtime)}"
    try:
        cache = json.loads(FACTS_CACHE.read_text())
    except (OSError, ValueError):
        cache = {}
    if key in cache:
        d = dict(cache[key])
        d["id"] = model_id
        return profiler.ModelFacts(**d)
    f = profiler.facts_from_gguf(path, model_id)
    cache[key] = f.to_dict()
    FACTS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    FACTS_CACHE.write_text(json.dumps(cache, indent=1))
    return f


def licence_ok(licence: str, model_id: str) -> tuple[bool, str]:
    lic = (licence or "").strip().lower()
    signed_off = [s.lower() for s in _extra().get("licence_signoff", [])]
    if model_id.lower() in signed_off:
        return True, f"{licence or 'licence'} — legal sign-off recorded"
    if any(lic.startswith(o) for o in OPEN_LICENCES):
        return True, f"{licence}: permits redistribution inside the organisation"
    if any(c in lic for c in CONDITIONAL):
        return False, (f"{licence}: conditional terms; excluded from shipped "
                       f"manifests until legal signs off (§9.3)")
    return False, (f"licence {licence or 'unknown'!r} is not on the redistribution "
                   f"allowlist; record a sign-off to ship it")


def _extra() -> dict[str, Any]:
    try:
        return json.loads(CATALOGUE_EXTRA.read_text())
    except (OSError, ValueError):
        return {}


def add_weights(path: str, model_id: str, *, internet_url: str = "",
                registry_model: str = "") -> dict[str, Any]:
    """Register a weight file the node holds (an admin action)."""
    p = Path(path).expanduser().resolve()
    f = profiler.facts_from_gguf(p, model_id)          # refuses a non-GGUF file
    extra = _extra()
    extra.setdefault("models", {})[model_id] = {
        "path": str(p), "internet_url": internet_url,
        "registry_model": registry_model}
    CATALOGUE_EXTRA.parent.mkdir(parents=True, exist_ok=True)
    CATALOGUE_EXTRA.write_text(json.dumps(extra, indent=1, sort_keys=True))
    return f.to_dict()


ENGINE_DIR = DATA_DIR / "bundles" / "engines"


def add_engine(path: str, *, engine: str, platform: str, variant: str,
               binary: str) -> dict[str, Any]:
    """Register an engine build the node distributes (an admin action).

    `path` is the unpacked release directory; it is packed once, deterministically
    (sorted entries, fixed mtimes, no owners), so the same build always has the
    same hash. `binary` is the executable's path inside it. The client receives
    it as a signed target, never from the internet (§9.2: client binaries travel
    only on the LAN channel)."""
    import io
    import tarfile
    src = Path(path).expanduser().resolve()
    if not src.is_dir():
        raise ValueError(f"{src} is not a directory")
    if not (src / binary).is_file():
        raise ValueError(f"{binary} is not a file inside {src}")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=6) as tar:
        for f in sorted(src.rglob("*")):
            # A release reaches its libraries through soname links
            # (libllama.so.0 -> libllama.so.0.1). The client accepts only plain
            # files, so a link that resolves inside the release is shipped as a
            # copy of its target; one that points outside it is never followed.
            real = f.resolve()
            if f.is_symlink() and not real.is_relative_to(src):
                continue
            if not real.is_file():
                continue
            info = tar.gettarinfo(str(real), arcname=f.relative_to(src).as_posix())
            info.mtime, info.uid, info.gid, info.uname, info.gname = 0, 0, 0, "", ""
            info.mode = 0o755 if real.stat().st_mode & 0o111 else 0o644
            with real.open("rb") as fh:
                tar.addfile(info, fh)
    data = buf.getvalue()
    ENGINE_DIR.mkdir(parents=True, exist_ok=True)
    key = f"{platform}-{engine}-{variant}".replace("/", "_")
    out = ENGINE_DIR / f"{key}.tar.gz"
    out.write_bytes(data)
    target = f"engines/{platform}/{engine}/{variant}.tar.gz"
    repo.add_targets([{"path": target, "file": str(out),
                       "custom": {"engine": engine, "platform": platform,
                                  "variant": variant, "binary": binary}}])
    extra = _extra()
    extra.setdefault("engines", {})[f"{platform}/{engine}"] = {
        "target": target, "variant": variant, "binary": binary,
        "sha256": hashlib_hex(data), "size": len(data)}
    CATALOGUE_EXTRA.parent.mkdir(parents=True, exist_ok=True)
    CATALOGUE_EXTRA.write_text(json.dumps(extra, indent=1, sort_keys=True))
    return extra["engines"][f"{platform}/{engine}"]


def add_harness(path: str, *, platform: str, version: str) -> dict[str, Any]:
    """Register the organisation's own harness build (spec §6.2: never an
    upstream release binary). Served as a signed target and pinned by hash in
    every manifest for the platform."""
    src = Path(path).expanduser().resolve()
    if not src.is_file():
        raise ValueError(f"{src} is not a file")
    target = f"harness/{platform}/opencode-{version}"
    repo.add_targets([{"path": target, "file": str(src),
                       "custom": {"harness": "opencode", "platform": platform,
                                  "version": version}}])
    extra = _extra()
    extra.setdefault("harness", {})[platform] = {
        "target": target, "version": version,
        "sha256": repo._file_sha256(src), "size": src.stat().st_size}
    CATALOGUE_EXTRA.parent.mkdir(parents=True, exist_ok=True)
    CATALOGUE_EXTRA.write_text(json.dumps(extra, indent=1, sort_keys=True))
    return extra["harness"][platform]


def catalogue() -> list[dict[str, Any]]:
    """Distributable models: facts, file, licence verdict, registry caps."""
    from ..gateway.registry import registry
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for model_id, e in (_extra().get("models") or {}).items():
        p = Path(e["path"])
        if not p.exists():
            continue
        card = registry.get(e.get("registry_model") or "") if e.get(
            "registry_model") else None
        out.append(_entry(model_id, p, card, e.get("internet_url", "")))
        seen.add(str(p))
    for card in registry.all():
        if card.backend != "ollama" or card.modality == "vision":
            continue
        blob = profiler.ollama_blob(card.backend_ref)
        if not blob or str(blob) in seen:
            continue
        out.append(_entry(card.name, blob, card, ""))
        seen.add(str(blob))
    return out


def _entry(model_id: str, path: Path, card: Any, internet_url: str) -> dict[str, Any]:
    try:
        f = _facts(path, model_id)
    except (OSError, profiler.GGUFError) as exc:
        return {"id": model_id, "error": str(exc)[:200], "path": str(path)}
    ok, why = licence_ok(f.licence, model_id)
    caps = dict(card.caps) if card is not None else {}
    return {"id": model_id, "facts": f.to_dict(), "path": str(path),
            "licence_ok": ok, "licence_note": why, "caps": caps,
            "registry_model": card.name if card is not None else "",
            "internet_url": internet_url}


def task_classes_allowed(caps: dict[str, float]) -> list[str]:
    """The detached classes a model may run: each class's capability floors,
    and the agentic floor on tool use. Unknown capabilities allow nothing."""
    from ..router import TASK_TYPES
    if not caps or float(caps.get("tool_use", 0)) < AGENTIC_TOOL_USE_FLOOR:
        return []
    out = []
    for cls, ttype in CLASS_TASK.items():
        needs = TASK_TYPES[ttype].needs
        if all(float(caps.get(axis, 0)) >= floor for axis, floor in needs.items()):
            out.append(cls)
    return out


def _rank(entry: dict[str, Any]) -> tuple:
    caps = entry.get("caps") or {}
    return (len(task_classes_allowed(caps)), float(caps.get("tool_use", 0)),
            float(caps.get("reasoning", 0)), -float(entry["facts"]["weights_mb"]))


def plan(profile: dict[str, Any], *, model_id: str | None = None,
         ctx_tokens: int = profiler.DEFAULT_CTX_TOKENS) -> dict[str, Any]:
    """Classify a device against every catalogue model; choose one to ship."""
    from ..control import trust
    shipped = trust.policy().get("shipped_classes", ["FIT-FAST", "SPLIT-PCIE",
                                                     "UNIFIED"])
    rows = []
    for e in catalogue():
        if "facts" not in e:
            rows.append({"model": e["id"], "device_class": "NONE",
                         "reason": e.get("error", "unreadable")})
            continue
        facts = profiler.ModelFacts(**e["facts"])
        c = profiler.classify(profile, facts, ctx_tokens=ctx_tokens,
                              shipped_classes=shipped)
        d = c.to_dict()
        d.update({"licence_ok": e["licence_ok"], "licence_note": e["licence_note"],
                  "task_classes_allowed": task_classes_allowed(e["caps"]),
                  "facts": e["facts"]})
        rows.append((d, e))
    candidates = [(d, e) for item in rows if isinstance(item, tuple)
                  for d, e in [item]
                  if d["shipped"] and d["licence_ok"]
                  and (model_id is None or e["id"] == model_id)]
    report = [item[0] if isinstance(item, tuple) else item for item in rows]
    if not candidates:
        why = ("no catalogue model is both licensed for redistribution and in a "
               "shipped class on this device")
        if model_id:
            why = f"{model_id} is not shippable to this device"
        return {"chosen": None, "reason": why, "classes": report}
    d, e = max(candidates, key=lambda de: _rank(de[1]))
    return {"chosen": {"classification": d, "entry": e},
            "reason": f"{e['id']}: {d['device_class']} — {d['reason']}",
            "classes": report}


def build(device: dict[str, Any], *, mode: str, model_id: str | None = None,
          node_url: str = "") -> dict[str, Any]:
    """The device's manifest, signed as a repository target. Refuses rather
    than issuing a manifest for a class that does not ship."""
    from ..control import trust
    profile = db.jload(device.get("profile"), {}) or {}
    if not profile:
        raise ValueError("the device has not reported a hardware profile; run "
                         "`clawcal device profile` first")
    profiler.validate_profile(profile)
    p = plan(profile, model_id=model_id)
    if not p["chosen"]:
        return {"issued": False, "reason": p["reason"], "classes": p["classes"]}
    d, e = p["chosen"]["classification"], p["chosen"]["entry"]
    facts = e["facts"]
    grade, _ = trust.compute_grade(device, mode=mode)
    rules = trust.rules_for(grade)
    pol = trust.policy()
    sha = repo._file_sha256(Path(e["path"]))
    model_target = f"models/{sha}.{facts['format']}"
    repo.add_targets([{"path": model_target, "file": e["path"],
                       "custom": {"model": e["id"], "quant": facts["quant"]}}])
    sources: list[dict[str, str]] = [{"lan": f"/api/bundles/targets/{model_target}"}]
    if mode == "detached" and e.get("internet_url"):
        sources.append({"internet": e["internet_url"]})
    egress_allow = ["127.0.0.1", "::1"]
    if node_url:
        egress_allow.append(node_url)
    if mode == "detached" and e.get("internet_url"):
        from urllib.parse import urlsplit
        u = urlsplit(e["internet_url"])
        egress_allow.append(f"{u.hostname}:{u.port or 443}")
    platform = profile.get("platform", device.get("platform", ""))
    runtime = {"linux": "bwrap", "macos": "apple-vz", "windows": "wsl2"}.get(
        platform, "wasi")
    fingerprint = hashlib_hex(signing.canonical({
        k: profile.get(k) for k in ("topology", "backend", "vram_mb", "ram_mb",
                                     "unified_ceiling_mb", "platform")}))
    ttl = float(pol["lease_ttl_hours"][mode])
    body = {
        "schema": "workbench.manifest/v2", "domain": trust.domain_name(),
        "device_id": device["id"], "device_class": d["device_class"],
        "device_fingerprint": f"sha256:{fingerprint}", "platform": platform,
        "mode": mode, "grade": grade,
        "engine": {"name": d["engine"], "flags": d["flags"], "env": d["env"],
                   "never": d["never"], "host": "127.0.0.1", "port": 8080,
                   "start_timeout_s": 180, "request_timeout_s": 600,
                   # The build the node ships for this platform, by hash; absent
                   # when the organisation has not registered one.
                   "artifact": (_extra().get("engines") or {}).get(
                       f"{platform}/{d['engine']}")},
        "harness": (_extra().get("harness") or {}).get(platform),
        "model": {"id": e["id"], "quant": facts["quant"], "format": facts["format"],
                  "moe": facts["moe"], "params_b": facts["params_b"],
                  "active_params_b": facts["active_params_b"],
                  "licence": facts["licence"],
                  "artifacts": [{"path": model_target, "sha256": sha,
                                 "size": Path(e["path"]).stat().st_size}],
                  "sources": sources},
        "policy": {
            "task_classes_allowed": [c for c in d["task_classes_allowed"]]
            if mode == "detached" else ["draft", "code", "calc", "extract", "vision"],
            "execute_local": ({"enabled": True, "runtime": runtime,
                               "network": "none", "timeout_s": 120}
                              if rules.get("execute_local")
                              else {"enabled": False,
                                    "reason": f"grade {grade} is not permitted "
                                              f"local execution"}),
            "egress_allow": egress_allow,
            "egress_enforcement": "reject",
            "clearance": trust.clearance(grade),
        },
        "lease": {"ttl_hours": ttl, "grace_hours": pol.get("grace_hours", 24),
                  "renew_path": "/api/lease/renew"},
        "classification": {"reason": d["reason"], "warnings": d["warnings"],
                           "need_mb": d["need_mb"], "fast_tier_mb": d["fast_tier_mb"],
                           "fast_tier_basis": d["fast_tier_basis"],
                           "cold_start_s": d["cold_start_s"]},
        "issued_at": round(time.time(), 3),
    }
    data = json.dumps(body, indent=1, sort_keys=True).encode()
    target = f"devices/{device['id']}/manifest.json"
    class_target = (f"manifests/{platform}/{d['device_class']}/{d['engine']}/"
                    f"{e['id']}/{facts['quant']}.json")
    generic = {k: v for k, v in body.items()
               if k not in ("device_id", "device_fingerprint", "grade", "issued_at")}
    repo.add_targets([
        {"path": target, "data": data,
         "custom": {"device_id": device["id"], "mode": mode}},
        {"path": class_target,
         "data": json.dumps(generic, indent=1, sort_keys=True).encode(),
         "custom": {"platform": platform, "device_class": d["device_class"],
                    "engine": d["engine"], "model": e["id"],
                    "quant": facts["quant"]}}])
    return {"issued": True, "target": target, "class_target": class_target,
            "manifest": body, "sha256": hashlib_hex(data),
            "classes": p["classes"]}


def hashlib_hex(b: bytes) -> str:
    import hashlib
    return hashlib.sha256(b).hexdigest()
