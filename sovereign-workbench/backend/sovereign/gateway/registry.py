"""Model registry.

A model is a row of capability metadata plus a backend reference. The router reads
capabilities; the scheduler reads residency cost. Adding a model later is a registry
insert -- no code change, satisfying the problem statement's "new open weight models
should be addable later without redesigning the system".
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .. import db
from ..config import settings

# Capability axes. Scores are 0.0-1.0 and are *claims* until the probe measures
# latency; the router combines them with measured residency cost.
CAP_AXES = ("text", "vision", "reasoning", "coding", "extraction",
            "long_context", "tool_use", "speed", "structured")


@dataclass
class ModelCard:
    name: str
    backend: str
    backend_ref: str
    family: str = ""
    role: str = "generalist"
    prompt_adapter: str = "chat"
    modality: str = "text"
    caps: dict[str, float] = field(default_factory=dict)
    ctx_max: int = 8192
    weights_mb: int = 0
    est_vram_mb: int = 0
    est_ram_mb: int = 0
    # VRAM consumed by the KV cache per 1000 context tokens, in MB. This is the
    # number that decides how much context is actually affordable once weights
    # are resident, and it is derived from the model's real attention geometry:
    #   2 (K and V) x layers x kv_heads x head_dim x 2 bytes (fp16)
    kv_mb_per_1k: float = 0.0
    enabled: bool = True
    notes: str = ""

    # populated from model_profiles when available
    profile: dict[str, Any] = field(default_factory=dict)

    def cap(self, axis: str) -> float:
        """A measured capability if the dirty-corpus evaluation recorded one.

        Catalogue scores are claims. Once `scripts/eval_dirty.py --record` has
        measured a model on this appliance, the measurement replaces the claim,
        so any model — better or worse than the catalogue says — is ranked by
        what it actually did, with no code or catalogue change.
        """
        measured = (self.profile.get("measured_caps") or {})
        if isinstance(measured, str):
            import json
            try:
                measured = json.loads(measured)
            except ValueError:
                measured = {}
        m = measured.get(axis)
        if isinstance(m, dict) and m.get("value") is not None and \
                (m.get("samples") or 0) >= 5:
            return float(m["value"])
        return float(self.caps.get(axis, 0.0))

    def cap_basis(self, axis: str) -> str:
        m = (self.profile.get("measured_caps") or {})
        if isinstance(m, str):
            import json
            try:
                m = json.loads(m)
            except ValueError:
                m = {}
        v = m.get(axis) if isinstance(m, dict) else None
        return "measured" if (isinstance(v, dict) and (v.get("samples") or 0) >= 5) \
            else "claim"

    def _profiled(self, key: str) -> float | None:
        """A profile value, unless the row was flagged invalid for that field.

        `invalid_reason` is written by runtime.perfmodel; a field it names is a
        recording bug, and admission must not plan around it.
        """
        v = self.profile.get(key)
        if not v or key in (self.profile.get("invalid_reason") or ""):
            return None
        return float(v)

    @property
    def decode_tps(self) -> float:
        return self._profiled("decode_tps") or 0.0

    @property
    def cold_load_s(self) -> float:
        """Measured cost of making this model resident. The scheduler's core number."""
        v = self._profiled("cold_load_s")
        if v:
            return float(v)
        # Fall back to a size-derived estimate: NVMe read + init, ~1.1 GB/s effective.
        return max(2.0, (self.weights_mb or self.est_vram_mb or 4096) / 1100.0)

    @property
    def kv_cost_per_1k(self) -> float:
        """KV cache MB per 1000 tokens, measured value or a size-derived estimate."""
        if self.kv_mb_per_1k:
            return float(self.kv_mb_per_1k)
        # Fallback for a model registered without attention geometry: KV cost
        # tracks parameter count closely enough for admission purposes.
        return max(20.0, (self.weights_mb or 4096) * 0.027)

    @property
    def residency_mb(self) -> float:
        v = self._profiled("vram_resident_mb")
        return float(v) if v else float(self.est_vram_mb or self.weights_mb)

    def to_row(self) -> dict[str, Any]:
        return {
            "name": self.name, "backend": self.backend, "backend_ref": self.backend_ref,
            "family": self.family, "role": self.role,
            "prompt_adapter": self.prompt_adapter, "modality": self.modality,
            "caps": db.jdump(self.caps), "ctx_max": self.ctx_max,
            "weights_mb": self.weights_mb, "est_vram_mb": self.est_vram_mb,
            "est_ram_mb": self.est_ram_mb, "kv_mb_per_1k": self.kv_mb_per_1k,
            "enabled": int(self.enabled), "notes": self.notes,
        }

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["cold_load_s"] = round(self.cold_load_s, 2)
        d["residency_mb"] = round(self.residency_mb, 1)
        from ..runtime import perfmodel
        d["basis"] = perfmodel.card_basis(self)
        return d


# ---------------------------------------------------------------------------
# Seed catalogue. Sized for an 8 GB / 15 GB workstation. `sync_with_backends()`
# disables anything the local backends do not actually serve, so the catalogue can
# describe more than this machine holds without lying about availability.
# ---------------------------------------------------------------------------
SEED: list[ModelCard] = [
    ModelCard(
        name="qwen3-8b", backend="ollama", backend_ref="qwen3:8b",
        family="qwen3", role="generalist", prompt_adapter="qwen", modality="text",
        ctx_max=32768, weights_mb=5200, est_vram_mb=6000, est_ram_mb=1200,
        # 36 layers x 8 KV heads x 128 head_dim -> ~140 MB per 1k tokens
        kv_mb_per_1k=140.0,
        caps={"text": 1.0, "vision": 0.0, "reasoning": 0.80, "coding": 0.70,
              "extraction": 0.78, "long_context": 0.80, "tool_use": 0.86,
              "speed": 0.85, "structured": 0.82},
        notes="Primary resident generalist: fits VRAM whole, fastest decode. "
              "Default for routine extraction, retrieval synthesis and drafting.",
    ),
    ModelCard(
        name="gpt-oss-20b", backend="ollama", backend_ref="gpt-oss-20b:latest",
        family="gpt-oss", role="deep-reasoner", prompt_adapter="harmony",
        modality="text", ctx_max=32768, weights_mb=12100, est_vram_mb=7200,
        est_ram_mb=6500,
        # 24 layers x 8 KV heads x 64 head_dim, with alternating sliding-window
        # attention -> ~48 MB per 1k tokens
        kv_mb_per_1k=48.0,
        caps={"text": 1.0, "vision": 0.0, "reasoning": 0.95, "coding": 0.88,
              "extraction": 0.85, "long_context": 0.85, "tool_use": 0.85,
              "speed": 0.30, "structured": 0.85},
        notes="21B MoE (3.6B active), MXFP4. Exceeds 8 GB VRAM so it runs part-"
              "offloaded: high quality, low decode rate. Reserved for tasks whose "
              "reasoning demand justifies the residency cost. Requires the harmony "
              "prompt adapter -- see gateway/adapters.py.",
    ),
    ModelCard(
        name="qwen2.5-7b", backend="ollama", backend_ref="qwen2.5:7b",
        family="qwen2.5", role="coder", prompt_adapter="chat", modality="text",
        ctx_max=32768, weights_mb=4700, est_vram_mb=5500, est_ram_mb=1000,
        # 28 layers x 4 KV heads x 128 head_dim -> ~56 MB per 1k tokens
        kv_mb_per_1k=56.0,
        caps={"text": 0.95, "vision": 0.0, "reasoning": 0.58, "coding": 0.82,
              "extraction": 0.70, "long_context": 0.75, "tool_use": 0.55,
              "speed": 0.90, "structured": 0.62},
        notes="Code generation and repair; also the low-cost fallback when the "
              "generalist is evicted or unhealthy. Tool-use and structured "
              "scores were revised down from their published-benchmark values "
              "after observation on this deployment: on trajectories longer than "
              "about four steps it repeats tool calls with identical arguments "
              "and conflates similarly-named bound variables. Good for "
              "single-shot summarisation and code, weak as an agent.",
    ),
    ModelCard(
        name="qwen2.5vl-3b", backend="ollama", backend_ref="qwen2.5vl:3b",
        family="qwen2.5-vl", role="vision", prompt_adapter="chat", modality="vision",
        ctx_max=16384, weights_mb=3300, est_vram_mb=4200, est_ram_mb=900,
        # 36 layers x 2 KV heads x 128 head_dim -> ~36 MB per 1k tokens
        kv_mb_per_1k=36.0,
        caps={"text": 0.70, "vision": 0.90, "reasoning": 0.50, "coding": 0.20,
              "extraction": 0.85, "long_context": 0.40, "tool_use": 0.40,
              "speed": 0.88, "structured": 0.70},
        notes="Small always-affordable VLM: scanned pages, photographs, handwriting, "
              "drawing tiles. Co-resident with the generalist inside 8 GB.",
    ),
    ModelCard(
        name="qwen3-4b", backend="ollama", backend_ref="qwen3:4b",
        family="qwen3", role="generalist", prompt_adapter="qwen", modality="text",
        ctx_max=32768, weights_mb=2500, est_vram_mb=3300, est_ram_mb=600,
        # 36 layers x 8 KV heads x 128 head_dim x 2 x 2 bytes = 144 KB/token
        kv_mb_per_1k=144.0,
        caps={"text": 0.92, "vision": 0.0, "reasoning": 0.72, "coding": 0.66,
              "extraction": 0.74, "long_context": 0.70, "tool_use": 0.78,
              "speed": 0.92, "structured": 0.76},
        enabled=False,
        notes="Small tool-calling generalist that fits an 8 GB GPU with room for "
              "its KV cache and no host-RAM spill: the model that keeps a "
              "memory-tight workstation usable when the 20B cannot load.",
    ),
    ModelCard(
        name="qwen3-vl-8b", backend="ollama", backend_ref="qwen3-vl:8b-instruct",
        family="qwen3-vl", role="vision", prompt_adapter="chat", modality="vision",
        ctx_max=32768, weights_mb=6140, est_vram_mb=6600, est_ram_mb=800,
        # Qwen3-8B text stack: 36 layers x 8 KV heads x 128 head_dim x 2 x 2 bytes
        kv_mb_per_1k=144.0,
        caps={"text": 0.88, "vision": 0.95, "reasoning": 0.72, "coding": 0.55,
              "extraction": 0.93, "long_context": 0.70, "tool_use": 0.65,
              "speed": 0.60, "structured": 0.85},
        enabled=False,
        notes="Strongest open VLM that fits an 8 GB GPU whole: document OCR, "
              "photographs, handwriting. The INSTRUCT build: the default "
              "`qwen3-vl:8b` tag is the Thinking variant, which ignores think=false "
              "and on a dense form spent its whole budget reasoning and returned "
              "no transcription.",
    ),
    ModelCard(
        name="granite3.2-vision-2b", backend="ollama",
        backend_ref="granite3.2-vision:2b", family="granite-vision", role="vision",
        prompt_adapter="chat", modality="vision", ctx_max=16384, weights_mb=2440,
        est_vram_mb=3200, est_ram_mb=600,
        # granite 3.1 2B: 40 layers x 8 KV heads x 64 head_dim x 2 x 2 bytes
        kv_mb_per_1k=80.0,
        caps={"text": 0.70, "vision": 0.88, "reasoning": 0.45, "coding": 0.20,
              "extraction": 0.88, "long_context": 0.45, "tool_use": 0.35,
              "speed": 0.85, "structured": 0.75},
        enabled=False,
        notes="Document-specialised VLM (forms, tables, charts). Its vision score "
              "is a claim until the dirty-corpus evaluation measures it against "
              "qwen2.5-vl on real scans, photographs and handwriting.",
    ),
    # -- the reference-server tier. Same code, different registry rows (§4):
    # `sync_registry` enables each only if the backend actually serves it, so on
    # an 8 GB laptop they are listed and disabled, and on a 48-80 GB server they
    # are selected by the same capability scoring as everything else.
    ModelCard(
        name="gpt-oss-120b", backend="ollama", backend_ref="gpt-oss:120b",
        family="gpt-oss", role="deep-reasoner", prompt_adapter="harmony",
        modality="text", ctx_max=131072, weights_mb=65000, est_vram_mb=66000,
        est_ram_mb=4000,
        # 36 layers x 8 KV heads x 64 head_dim x 2 (K,V) x 2 bytes = 72 KB/token
        kv_mb_per_1k=72.0,
        caps={"text": 1.0, "vision": 0.0, "reasoning": 0.98, "coding": 0.93,
              "extraction": 0.90, "long_context": 0.92, "tool_use": 0.90,
              "speed": 0.35, "structured": 0.90},
        enabled=False,
        notes="117B MoE (5.1B active), MXFP4. The PS's reference-hardware model: "
              "fits one 80 GB GPU, or part-offloads through llama.cpp's CPU-MoE "
              "path. Requires the harmony adapter.",
    ),
    ModelCard(
        name="qwen3-32b", backend="ollama", backend_ref="qwen3:32b",
        family="qwen3", role="generalist", prompt_adapter="qwen", modality="text",
        ctx_max=40960, weights_mb=20200, est_vram_mb=21500, est_ram_mb=1500,
        # 64 layers x 8 KV heads x 128 head_dim x 2 x 2 bytes = 256 KB/token
        kv_mb_per_1k=256.0,
        caps={"text": 1.0, "vision": 0.0, "reasoning": 0.90, "coding": 0.84,
              "extraction": 0.88, "long_context": 0.82, "tool_use": 0.90,
              "speed": 0.55, "structured": 0.88},
        enabled=False,
        notes="Dense 32B generalist for a 24 GB+ GPU: the server-tier default for "
              "extraction, drafting and multi-step tool use.",
    ),
    ModelCard(
        name="qwen2.5-coder-32b", backend="ollama", backend_ref="qwen2.5-coder:32b",
        family="qwen2.5", role="coder", prompt_adapter="chat", modality="text",
        ctx_max=32768, weights_mb=19900, est_vram_mb=21000, est_ram_mb=1500,
        kv_mb_per_1k=256.0,
        caps={"text": 0.95, "vision": 0.0, "reasoning": 0.75, "coding": 0.94,
              "extraction": 0.75, "long_context": 0.75, "tool_use": 0.70,
              "speed": 0.55, "structured": 0.80},
        enabled=False,
        notes="Server-tier code model: internal tools, scripts and their tests.",
    ),
    ModelCard(
        name="qwen2.5vl-7b", backend="ollama", backend_ref="qwen2.5vl:7b",
        family="qwen2.5-vl", role="vision", prompt_adapter="chat", modality="vision",
        ctx_max=32768, weights_mb=6000, est_vram_mb=7500, est_ram_mb=1200,
        # 28 layers x 4 KV heads x 128 head_dim x 2 x 2 bytes = 56 KB/token
        kv_mb_per_1k=56.0,
        caps={"text": 0.78, "vision": 0.94, "reasoning": 0.60, "coding": 0.30,
              "extraction": 0.90, "long_context": 0.55, "tool_use": 0.50,
              "speed": 0.70, "structured": 0.78},
        enabled=False,
        notes="Stronger VLM for photographs and handwriting where VRAM allows.",
    ),
]

# Other names the same weights are published under. A fresh `ollama pull
# gpt-oss:20b` on a cloud box serves "gpt-oss:20b", while this workstation
# imported the GGUF as "gpt-oss-20b"; both are the same model, and refusing one
# of them would disable the model on every deployment but this one.
ALT_REFS: dict[str, tuple[str, ...]] = {
    "gpt-oss-20b": ("gpt-oss:20b", "gpt-oss-20b:latest", "gpt-oss-20b"),
    "gpt-oss-120b": ("gpt-oss:120b", "gpt-oss-120b:latest"),
    "qwen3-8b": ("qwen3:8b", "qwen3:8b-q4_K_M"),
    "qwen2.5-7b": ("qwen2.5:7b", "qwen2.5:7b-instruct"),
    "qwen2.5vl-3b": ("qwen2.5vl:3b",),
    "qwen2.5vl-7b": ("qwen2.5vl:7b",),
    "qwen3-4b": ("qwen3:4b",),
    # Deliberately not "qwen3-vl:8b": that tag is the Thinking build.
    "qwen3-vl-8b": ("qwen3-vl:8b-instruct", "qwen3-vl:8b-instruct-q4_K_M"),
    "granite3.2-vision-2b": ("granite3.2-vision:2b", "granite3.2-vision:latest"),
    "qwen3-32b": ("qwen3:32b",),
    "qwen2.5-coder-32b": ("qwen2.5-coder:32b",),
}


class ModelRegistry:
    def __init__(self) -> None:
        self._cache: dict[str, ModelCard] = {}

    # -- persistence ------------------------------------------------------
    def seed(self) -> None:
        """Insert catalogue rows, and refresh shipped metadata on untouched ones.

        This used to upsert every column on every sync, which silently undid
        an administrator's edits and reset each row's `enabled` flag. A row an
        admin has edited is theirs; a row nobody edited tracks the shipped
        catalogue, except for `enabled`, which only the backend sync decides.
        """
        for card in SEED:
            row = card.to_row()
            existing = db.query_one(
                "SELECT enabled, backend_ref, edited_by FROM model_registry "
                "WHERE name=?", (card.name,))
            if existing is None:
                db.insert("model_registry", row)
                continue
            if existing["edited_by"]:
                continue
            # The shipped ref is written back every time: preserving the stored
            # one meant a catalogue correction (qwen3-vl:8b -> the instruct
            # build) could never reach an installed appliance. Sync runs right
            # after seeding and re-resolves any alias the backend actually uses.
            row.pop("enabled", None)
            db.update("model_registry", "name", card.name, row)
        for extra in settings.backends.openai_compat_models:
            db.upsert("model_registry", ModelCard(
                name=extra.replace("/", "-"), backend="openai_compat",
                backend_ref=extra, family="external-local", role="generalist",
                prompt_adapter="chat", ctx_max=16384,
                caps={"text": 1.0, "reasoning": 0.7, "coding": 0.7, "extraction": 0.7,
                      "tool_use": 0.7, "speed": 0.6, "structured": 0.7},
                notes="Registered from OPENAI_COMPAT_MODELS. Demonstrates that a new "
                      "backend is a registry row, not a redesign.",
            ).to_row(), key="name")
        self.invalidate()

    def invalidate(self) -> None:
        self._cache.clear()

    def _load(self) -> dict[str, ModelCard]:
        if self._cache:
            return self._cache
        profiles = {r["model"]: dict(r)
                    for r in db.query("SELECT * FROM model_profiles")}
        for r in db.query("SELECT * FROM model_registry"):
            card = ModelCard(
                name=r["name"], backend=r["backend"], backend_ref=r["backend_ref"],
                family=r["family"] or "", role=r["role"] or "generalist",
                prompt_adapter=r["prompt_adapter"] or "chat",
                modality=r["modality"] or "text",
                caps=db.jload(r["caps"], {}) or {}, ctx_max=r["ctx_max"] or 8192,
                weights_mb=r["weights_mb"] or 0, est_vram_mb=r["est_vram_mb"] or 0,
                est_ram_mb=r["est_ram_mb"] or 0,
                kv_mb_per_1k=r["kv_mb_per_1k"] or 0.0, enabled=bool(r["enabled"]),
                notes=r["notes"] or "", profile=profiles.get(r["name"], {}),
            )
            self._cache[card.name] = card
        return self._cache

    # -- lookup -----------------------------------------------------------
    def all(self, include_disabled: bool = False) -> list[ModelCard]:
        cards = list(self._load().values())
        return cards if include_disabled else [c for c in cards if c.enabled]

    def get(self, name: str) -> ModelCard | None:
        cards = self._load()
        if name in cards:
            return cards[name]
        for c in cards.values():          # also accept the backend's own name
            if c.backend_ref == name:
                return c
        return None

    def by_role(self, role: str) -> list[ModelCard]:
        return [c for c in self.all() if c.role == role]

    def vision_models(self) -> list[ModelCard]:
        """Vision-capable models, best first — measured ones before claims.

        A catalogue claim of 0.93 once outranked a measured 0.90, sending every
        page to a model that could not even load here. A model is preferred on
        its measurement; an unmeasured one follows every measured one until
        `scripts/eval_dirty.py --record` has scored it.
        """
        return sorted((c for c in self.all() if c.cap("vision") > 0.4),
                      key=lambda c: (c.cap_basis("vision") != "measured",
                                     -c.cap("vision")))

    def set_backend_ref(self, name: str, ref: str) -> None:
        db.update("model_registry", "name", name, {"backend_ref": ref})
        self.invalidate()

    def set_enabled(self, name: str, enabled: bool, reason: str = "") -> None:
        db.update("model_registry", "name", name,
                  {"enabled": int(enabled), "notes": reason} if reason
                  else {"enabled": int(enabled)})
        self.invalidate()

    def record_profile(self, model: str, prof: dict[str, Any]) -> None:
        row = {"model": model, "measured_at": db.now(), **prof}
        row["raw"] = db.jdump(prof.get("raw", {}))
        db.upsert("model_profiles", row, key="model")
        self.invalidate()


registry = ModelRegistry()
