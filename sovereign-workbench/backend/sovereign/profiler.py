"""B5 — hardware classification (sovereign-workbench-v2.md §7).

The profiler is to hardware what the router is to tasks: one deterministic,
explainable classification, written to the decision log with its reason. It
never guesses a number it could read: model facts come from the weights file's
own GGUF header and tensor table, device facts from the client's measurements,
each carrying its basis (measured / link-rated / prior / unknown).

A machine can be several classes for several models, so the class is reported
**per candidate model**:

    FIT-FAST     the whole working set fits the fast tier (VRAM, or the unified
                 working-set ceiling on a discrete-less machine)
    SPLIT-PCIE   discrete GPU; an MoE's experts live in host RAM, attention in
                 VRAM; PCIe bandwidth is the cost
    UNIFIED      unified memory; fits the working-set ceiling; no offload — the
                 MVP measured expert offload at 0.53× decode there
    STREAM-NVME  bigger than RAM; MoE experts paged from NVMe; research tier
    CPU-ONLY     no usable GPU; fits host RAM
    MOBILE       phone-class; ≤ 4 B parameters; thin client for agentic work
    NONE         cannot meet the floor; said plainly, with the reason

Only the classes in the policy's `shipped_classes` get a detached manifest
(stopping rule 2); the others are reported, never issued.
"""
from __future__ import annotations

import math
import os
import struct
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

CLASSES = ("FIT-FAST", "SPLIT-PCIE", "UNIFIED", "STREAM-NVME", "CPU-ONLY",
           "MOBILE", "NONE")

# Held back from every tier: the display server and CUDA context on the GPU,
# the OS and the harness in RAM. Same figures the node's own admission uses.
VRAM_RESERVE_MB = 700.0
RAM_RESERVE_MB = 2048.0
RUNTIME_OVERHEAD_MB = 600.0           # engine process, compute graphs, buffers
DEFAULT_CTX_TOKENS = 16384            # the agentic context a manifest reserves
MOBILE_MAX_PARAMS_B = 4.0             # T12: the measured agentic floor sits above
PCIE_TRAP_GBS = 16.0                  # below this, split decode is lane-starved


# ------------------------------------------------------------------ GGUF facts

_GGUF_SCALARS = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f",
                 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}
# Bytes per block and elements per block for the ggml tensor types, enough to
# size every tensor exactly (types absent here size as unknown).
_GGML_BLOCK = {0: (4, 1), 1: (2, 1), 2: (18, 32), 3: (20, 32), 6: (22, 32),
               7: (24, 32), 8: (34, 32), 9: (36, 32), 10: (84, 256),
               11: (110, 256), 12: (144, 256), 13: (176, 256), 14: (210, 256),
               15: (292, 256), 16: (66, 256), 17: (74, 256), 18: (98, 256),
               19: (50, 256), 20: (18, 32), 21: (110, 256), 22: (82, 256),
               23: (136, 256), 24: (1, 1), 25: (2, 1), 26: (4, 1), 27: (8, 1),
               28: (8, 1), 29: (56, 256), 30: (2, 1), 34: (54, 256),
               35: (66, 256), 39: (17, 32)}


class GGUFError(ValueError):
    pass


def _read_str(fh: BinaryIO) -> str:
    (n,) = struct.unpack("<Q", fh.read(8))
    if n > 1 << 24:
        raise GGUFError("implausible string length in GGUF header")
    return fh.read(n).decode("utf-8", "replace")


def _read_value(fh: BinaryIO, vtype: int) -> Any:
    if vtype in _GGUF_SCALARS:
        fmt = _GGUF_SCALARS[vtype]
        return struct.unpack(fmt, fh.read(struct.calcsize(fmt)))[0]
    if vtype == 8:
        return _read_str(fh)
    if vtype == 9:
        (etype,) = struct.unpack("<I", fh.read(4))
        (n,) = struct.unpack("<Q", fh.read(8))
        if etype in _GGUF_SCALARS:
            size = struct.calcsize(_GGUF_SCALARS[etype])
            if n > 64:                      # vocabularies: skip, keep the count
                fh.seek(size * n, 1)
                return {"array_len": n}
            return [_read_value(fh, etype) for _ in range(n)]
        if etype == 8:
            if n > 64:
                for _ in range(n):
                    (m,) = struct.unpack("<Q", fh.read(8))
                    fh.seek(m, 1)
                return {"array_len": n}
            return [_read_str(fh) for _ in range(n)]
        raise GGUFError(f"unsupported GGUF array element type {etype}")
    raise GGUFError(f"unsupported GGUF value type {vtype}")


def read_gguf(path: str | Path) -> dict[str, Any]:
    """Header metadata and tensor table of a GGUF file. Reads kilobytes, not
    the weights."""
    p = Path(path)
    with p.open("rb") as fh:
        if fh.read(4) != b"GGUF":
            raise GGUFError(f"{p.name} is not a GGUF file")
        (version,) = struct.unpack("<I", fh.read(4))
        if version < 2:
            raise GGUFError(f"GGUF v{version} is too old to read")
        n_tensors, n_kv = struct.unpack("<QQ", fh.read(16))
        meta: dict[str, Any] = {}
        for _ in range(n_kv):
            key = _read_str(fh)
            (vtype,) = struct.unpack("<I", fh.read(4))
            meta[key] = _read_value(fh, vtype)
        tensors = []
        for _ in range(n_tensors):
            name = _read_str(fh)
            (nd,) = struct.unpack("<I", fh.read(4))
            dims = struct.unpack(f"<{nd}Q", fh.read(8 * nd))
            (ttype,) = struct.unpack("<I", fh.read(4))
            fh.read(8)                                  # data offset
            tensors.append((name, dims, ttype))
    return {"version": version, "meta": meta, "tensors": tensors,
            "file_bytes": p.stat().st_size}


@dataclass
class ModelFacts:
    """What the classifier needs to know about a candidate model."""
    id: str
    weights_mb: float
    params_b: float = 0.0
    active_params_b: float = 0.0
    moe: bool = False
    layers: int = 0
    experts: int = 0
    experts_used: int = 0
    expert_mb: float = 0.0              # bytes in routed-expert tensors
    kv_mb_per_1k: float = 0.0
    quant: str = ""
    format: str = "gguf"
    architecture: str = ""
    licence: str = ""
    basis: str = "prior"                # read-from-file | registry | prior

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_QUANT_NAMES = {0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 7: "Q8_0", 8: "Q5_0",
                9: "Q5_1", 10: "Q2_K", 11: "Q3_K_S", 12: "Q3_K_M", 13: "Q3_K_L",
                14: "Q4_K_S", 15: "Q4_K_M", 16: "Q5_K_S", 17: "Q5_K_M",
                18: "Q6_K", 19: "IQ2_XXS", 24: "IQ4_XS", 32: "BF16",
                38: "MXFP4"}


def facts_from_gguf(path: str | Path, model_id: str = "") -> ModelFacts:
    g = read_gguf(path)
    meta = g["meta"]
    arch = str(meta.get("general.architecture", ""))
    layers = int(meta.get(f"{arch}.block_count", 0) or 0)
    experts = int(meta.get(f"{arch}.expert_count", 0) or 0)
    used = int(meta.get(f"{arch}.expert_used_count", 0) or 0)
    total_el = expert_el = 0
    total_b = expert_b = 0.0
    for name, dims, ttype in g["tensors"]:
        n = math.prod(dims) if dims else 0
        total_el += n
        blk = _GGML_BLOCK.get(ttype)
        nbytes = (n / blk[1]) * blk[0] if blk else 0.0
        total_b += nbytes
        if "_exps" in name:
            expert_el += n
            expert_b += nbytes
    if not total_b:
        total_b = float(g["file_bytes"])
    moe = experts > 1 and expert_el > 0
    active_el = (total_el - expert_el * (1 - used / experts)) if moe and experts \
        else total_el
    # KV per 1k tokens from the attention geometry: 2 (K and V) × layers ×
    # kv_heads × head_dim × 2 bytes (f16 cache) × 1000. Sliding-window layers
    # are counted in full, which overstates gpt-oss — a safe error.
    heads_kv = meta.get(f"{arch}.attention.head_count_kv") or meta.get(
        f"{arch}.attention.head_count") or 0
    if isinstance(heads_kv, list):
        heads_kv = max(heads_kv) if heads_kv else 0
    key_len = int(meta.get(f"{arch}.attention.key_length", 0) or 0)
    if not key_len:
        emb = int(meta.get(f"{arch}.embedding_length", 0) or 0)
        heads = meta.get(f"{arch}.attention.head_count") or 1
        if isinstance(heads, list):
            heads = max(heads) if heads else 1
        key_len = emb // max(1, int(heads)) if emb else 0
    kv_mb = 2 * layers * int(heads_kv or 0) * key_len * 2 * 1000 / 1e6
    ftype = meta.get("general.file_type")
    quant = _QUANT_NAMES.get(int(ftype), f"type{ftype}") if ftype is not None else ""
    return ModelFacts(
        id=model_id or str(meta.get("general.name") or Path(path).stem),
        weights_mb=round(g["file_bytes"] / 1e6, 1),
        params_b=round(total_el / 1e9, 2), active_params_b=round(active_el / 1e9, 2),
        moe=moe, layers=layers, experts=experts, experts_used=used,
        expert_mb=round(expert_b / 1e6, 1), kv_mb_per_1k=round(kv_mb, 2),
        quant=quant, format="gguf", architecture=arch,
        licence=str(meta.get("general.license", "") or ""), basis="read-from-file")


# --------------------------------------------------------------- the classes

@dataclass
class Classification:
    model: str
    device_class: str
    reason: str
    shipped: bool = False
    engine: str = ""
    flags: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    never: list[str] = field(default_factory=list)
    need_mb: float = 0.0
    fast_tier_mb: float = 0.0
    fast_tier_basis: str = ""
    cold_start_s: float | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# What must never appear on an engine's command line, per engine. These are the
# survey's traps turned into enforcement (§7.3): llama.cpp's -hf / --model-url
# download from the internet, --rpc ships tensors to a remote host; FreeToken's
# auto cache sizing starves KV and hangs silently (T8).
NEVER = {
    "llama.cpp": ["--rpc", "-hf", "--hf-repo", "--hf-file", "-hff", "-hfr",
                  "--model-url", "-mu", "--host", "--port"],
    "freetoken": ["--moe-cache-auto", "--host", "--port"],
}


def _fast_tier(profile: dict[str, Any]) -> tuple[float, str, str]:
    """(fast tier MB, basis, topology)."""
    topo = profile.get("topology") or "cpu-only"
    if topo == "discrete":
        return max(0.0, float(profile.get("vram_mb") or 0) - VRAM_RESERVE_MB), \
            "measured VRAM less the display/CUDA reserve", topo
    if topo in ("unified", "mobile"):
        ceiling = profile.get("unified_ceiling_mb")
        if ceiling:
            return float(ceiling), "the platform's reported GPU working-set ceiling", topo
        # Apple grants roughly two thirds to three quarters of unified memory
        # to the GPU; without the platform's number, the lower bound.
        return 0.65 * float(profile.get("ram_mb") or 0), \
            "prior: 65% of unified memory (no ceiling reported)", topo
    return 0.0, "no GPU", topo


def classify(profile: dict[str, Any], model: ModelFacts, *,
             ctx_tokens: int = DEFAULT_CTX_TOKENS,
             shipped_classes: tuple[str, ...] | list[str] = ("FIT-FAST", "SPLIT-PCIE",
                                                             "UNIFIED"),
             ftw_available: bool = False) -> Classification:
    """Classify one device for one candidate model."""
    fast, basis, topo = _fast_tier(profile)
    ram = float(profile.get("ram_mb") or 0)
    ram_budget = max(0.0, ram - RAM_RESERVE_MB)
    kv = model.kv_mb_per_1k * ctx_tokens / 1000.0
    need = model.weights_mb + kv + RUNTIME_OVERHEAD_MB
    platform = (profile.get("platform") or "").lower()
    backend = (profile.get("backend") or "cpu").lower()
    pcie = profile.get("pcie") or {}
    nvme = profile.get("nvme") or {}
    warnings: list[str] = []
    if model.basis == "prior":
        warnings.append("model facts are priors, not read from the weights file")

    def out(cls: str, reason: str, **kw: Any) -> Classification:
        c = Classification(model=model.id, device_class=cls, reason=reason,
                           shipped=cls in shipped_classes, need_mb=round(need),
                           fast_tier_mb=round(fast), fast_tier_basis=basis,
                           warnings=warnings, **kw)
        if cls not in ("NONE", "MOBILE", "STREAM-NVME") and not c.engine:
            _engine(c, model, profile, ctx_tokens, ftw_available)
        if nvme.get("read_gbs") and cls != "NONE":
            c.cold_start_s = round(model.weights_mb / 1000.0 / float(nvme["read_gbs"]), 1)
        return c

    head = (f"{model.id} needs about {need:,.0f} MB ({model.weights_mb:,.0f} MB "
            f"weights + {kv:,.0f} MB KV at {ctx_tokens:,} tokens + "
            f"{RUNTIME_OVERHEAD_MB:.0f} MB runtime)")

    if topo == "mobile":
        if model.params_b and model.params_b <= MOBILE_MAX_PARAMS_B and need <= fast:
            return out("MOBILE", f"{head}; a phone-class device within its OS memory "
                                 f"grant. Agentic work stays attached (T12: ≤ 4 B "
                                 f"models score ≤ 35 % on multi-turn tool use)")
        return out("NONE", f"{head}; a phone-class device cannot hold it, or it is "
                           f"above the {MOBILE_MAX_PARAMS_B:.0f} B mobile ceiling")

    if topo in ("discrete", "unified") and need <= fast:
        cls = "UNIFIED" if topo == "unified" else "FIT-FAST"
        return out(cls, f"{head}, which fits the {fast:,.0f} MB fast tier "
                        f"({basis})" + ("; no offload — on unified memory there is "
                                        "no bandwidth cliff to route around"
                                        if topo == "unified" else ""))

    if topo == "discrete":
        dense_part = model.weights_mb - model.expert_mb
        if model.moe and dense_part + kv + RUNTIME_OVERHEAD_MB <= fast \
                and need - fast <= ram_budget:
            gbs = float(pcie.get("gbs") or 0)
            if gbs and gbs < PCIE_TRAP_GBS:
                warnings.append(f"PCIe measures {gbs:.1f} GB/s "
                                f"({pcie.get('basis', 'unknown basis')}); split decode "
                                f"is lane-starved below {PCIE_TRAP_GBS:.0f} GB/s — the "
                                f"×8-slot trap")
            elif not gbs:
                warnings.append("PCIe bandwidth unknown; split decode speed cannot "
                                "be predicted")
            return out("SPLIT-PCIE",
                       f"{head}: more than the {fast:,.0f} MB of VRAM, but its "
                       f"attention and shared weights ({dense_part:,.0f} MB) plus KV "
                       f"fit VRAM and its {model.expert_mb:,.0f} MB of routed experts "
                       f"fit host RAM ({ram_budget:,.0f} MB after the reserve)")
        if not model.moe and need <= fast + ram_budget:
            return out("NONE", f"{head}: a dense model split across PCIe streams its "
                               f"host-resident layers on every token; below the "
                               f"decode floor, so not offered on this device")

    if topo == "unified" and need > fast and need <= ram_budget:
        return out("NONE", f"{head}: above the {fast:,.0f} MB GPU working-set ceiling "
                           f"({basis}); on unified memory offload does not help "
                           f"(measured 0.53× decode), so choose a smaller model")

    if topo == "cpu-only" or backend == "cpu":
        if need <= ram_budget:
            if not model.moe and (model.active_params_b or model.params_b) > 8:
                return out("NONE", f"{head}: fits RAM, but a dense "
                                   f"{model.params_b:.0f} B model decodes too slowly "
                                   f"on CPU to be an agent")
            return out("CPU-ONLY", f"{head}, which fits {ram_budget:,.0f} MB of host "
                                   f"RAM with no usable GPU; "
                                   f"{model.active_params_b or model.params_b:.1f} B "
                                   f"active parameters per token")

    if model.moe and model.weights_mb > ram_budget:
        if nvme.get("read_gbs"):
            return out("STREAM-NVME",
                       f"{head}: larger than host RAM; an MoE whose experts could be "
                       f"paged from NVMe at {float(nvme['read_gbs']):.1f} GB/s "
                       f"({nvme.get('basis', 'unknown basis')}). A research tier "
                       f"(RQ11): no shared engine ships for it")
        return out("NONE", f"{head}: larger than host RAM and NVMe read speed was "
                           f"not measured")

    return out("NONE", f"{head}: does not fit this device's "
                       f"{fast:,.0f} MB fast tier or {ram_budget:,.0f} MB of host RAM")


def _engine(c: Classification, model: ModelFacts, profile: dict[str, Any],
            ctx_tokens: int, ftw_available: bool) -> None:
    """Choose the engine and its pinned flags for a class (§7.2 table)."""
    platform = (profile.get("platform") or "").lower()
    backend = (profile.get("backend") or "cpu").lower()
    if c.device_class == "SPLIT-PCIE" and platform == "linux" and backend == "cuda" \
            and ftw_available:
        c.engine = "freetoken"
        c.flags = ["--kv-reserve-tokens", str(ctx_tokens),
                   "--max-running-requests", "1"]
        c.env = {"FREETOKEN_DISABLE_JIT": "1"}
        c.never = list(NEVER["freetoken"])
        return
    c.engine = "llama.cpp"
    c.flags = ["--ctx-size", str(ctx_tokens), "--parallel", "1", "--jinja"]
    if backend != "cpu" and c.device_class != "CPU-ONLY":
        c.flags += ["--n-gpu-layers", "999"]
    if c.device_class == "SPLIT-PCIE":
        # Keep this many layers' experts on the CPU: enough to bring the VRAM
        # part under the fast tier, from the per-layer expert size.
        per_layer = model.expert_mb / max(1, model.layers)
        excess = c.need_mb - c.fast_tier_mb
        k = max(1, min(model.layers or 1, math.ceil(excess / per_layer)
                       if per_layer else model.layers or 1))
        c.flags += ["--n-cpu-moe", str(k)]
        if platform == "windows":
            c.warnings.append("FreeToken's Windows build is unmeasured; llama.cpp "
                              "--n-cpu-moe until it is (§3.4 item 8)")
    c.never = list(NEVER["llama.cpp"])


def classify_all(profile: dict[str, Any], models: list[ModelFacts],
                 **kw: Any) -> list[Classification]:
    return [classify(profile, m, **kw) for m in models]


def validate_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """Normalise a client-reported profile; refuse one that cannot be read."""
    if not isinstance(profile, dict):
        raise ValueError("profile must be an object")
    topo = profile.get("topology")
    if topo not in ("discrete", "unified", "cpu-only", "mobile"):
        raise ValueError("profile.topology must be discrete, unified, cpu-only or "
                         "mobile")
    for k in ("vram_mb", "ram_mb"):
        v = profile.get(k, 0)
        if not isinstance(v, (int, float)) or v < 0 or v > 16_000_000:
            raise ValueError(f"profile.{k} must be a plausible number of MB")
    return profile


# ------------------------------------------------------------- the node itself

def node_profile() -> dict[str, Any]:
    """This node's own measurements, in the device-profile shape."""
    from . import hardware
    hp = hardware.cached_profile()
    gpu = hp.get("gpu") or {}
    mem = hp.get("memory") or {}
    have_gpu = bool(gpu.get("available"))
    return {
        "schema": "workbench.profile/v2", "platform": "linux",
        "topology": "discrete" if have_gpu else "cpu-only",
        "vram_mb": gpu.get("total_mb") or 0, "vram_free_mb": gpu.get("free_mb") or 0,
        "ram_mb": mem.get("total_mb") or 0,
        "ram_available_mb": mem.get("available_mb") or 0,
        "pcie": {"gbs": hp.get("pcie_est_gbs") or 0,
                 "basis": "link-rated (generation × width from the driver)"
                 if hp.get("pcie_est_gbs") else "unknown"},
        "backend": "cuda" if have_gpu else "cpu",
        "gpus": gpu.get("devices") or ([{"name": gpu.get("name"),
                                         "vram_mb": gpu.get("total_mb")}]
                                       if have_gpu else []),
        "measured_at": hp.get("probed_at"),
    }


def ollama_blob(backend_ref: str) -> Path | None:
    """The GGUF blob Ollama keeps for a model reference, if it is on this host.

    Ollama names blobs by their SHA-256, so the manifest's hash is the file's
    name — a detached client can verify what it downloads against it.
    """
    from . import hardware
    import json
    root = Path(hardware.model_dir() or os.path.expanduser("~/.ollama/models"))
    name, _, tag = backend_ref.partition(":")
    ns, _, repo = name.rpartition("/")
    mf = root / "manifests" / "registry.ollama.ai" / (ns or "library") / repo / (tag or "latest")
    try:
        doc = json.loads(mf.read_text())
    except (OSError, ValueError):
        return None
    for layer in doc.get("layers", []):
        if layer.get("mediaType") == "application/vnd.ollama.image.model":
            blob = root / "blobs" / str(layer.get("digest", "")).replace(":", "-")
            return blob if blob.exists() else None
    return None
