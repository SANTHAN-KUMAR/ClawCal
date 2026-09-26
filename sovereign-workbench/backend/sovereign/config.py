"""Central configuration for the Sovereign Workbench.

Every path and tunable lives here. Nothing else in the codebase reads os.environ
directly, so an operator can audit the whole configuration surface in one file.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# transformers will import TensorFlow/Flax if it finds them and flood stderr with
# oneDNN and protobuf warnings. We only ever use the PyTorch path.
os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_FLAX", "0")
os.environ.setdefault("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1")
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

# The embedding model must load from the local cache and never reach for the hub.
# Without this, sentence-transformers performs a metadata check on first load;
# the egress guard correctly refuses it, the load raises, and dense retrieval
# silently degrades to lexical-only — a quiet loss of half the retrieval stack
# that looks like nothing at all from the outside.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

REPO_ROOT = Path(__file__).resolve().parents[2]


def _p(env: str, default: Path) -> Path:
    return Path(os.environ.get(env, str(default))).expanduser().resolve()


# The code tree lives on an ntfs-3g (FUSE) mount which cannot support SQLite WAL,
# POSIX permissions or reliable advisory locking. Runtime state therefore lives on
# ext4. See docs/decisions.md D-01.
DATA_DIR = _p("SOVEREIGN_DATA_DIR", Path.home() / ".sovereign")

DB_PATH = DATA_DIR / "db" / "sovereign.db"
EVIDENCE_DIR = DATA_DIR / "evidence"
UPLOAD_DIR = DATA_DIR / "uploads"
ARTIFACT_DIR = DATA_DIR / "artifacts"
WORKSPACE_DIR = DATA_DIR / "workspaces"
CHECKPOINT_DIR = DATA_DIR / "checkpoints"
MODEL_CACHE_DIR = DATA_DIR / "models"
LOG_DIR = DATA_DIR / "logs"

CORPUS_DIR = REPO_ROOT / "corpus"
TEMPLATE_DIR = REPO_ROOT / "backend" / "sovereign" / "deliverables" / "templates"
FRONTEND_DIR = REPO_ROOT / "frontend"

for _d in (
    DB_PATH.parent, EVIDENCE_DIR, UPLOAD_DIR, ARTIFACT_DIR,
    WORKSPACE_DIR, CHECKPOINT_DIR, MODEL_CACHE_DIR, LOG_DIR,
):
    _d.mkdir(parents=True, exist_ok=True)


def _backend_ports() -> tuple[int, ...]:
    """Loopback ports the configured inference backends actually listen on.

    The allowlist used to be a fixed list of well-known ports, so a second
    Ollama on :11435 or a llama.cpp server on :8081 was refused by the
    appliance's own egress guard. Ports come from the backend URLs; hosts are
    still loopback only.
    """
    from urllib.parse import urlsplit
    urls = (os.environ.get("OLLAMA_URLS", "") + "," + os.environ.get("OLLAMA_URL", "")
            + "," + os.environ.get("OPENAI_COMPAT_URL", "") + ","
            + os.environ.get("LLAMACPP_URL", ""))
    ports = []
    for u in urls.split(","):
        u = u.strip()
        if not u:
            continue
        try:
            sp = urlsplit(u)
        except ValueError:
            continue
        if sp.hostname in ("127.0.0.1", "localhost", "::1") and sp.port:
            ports.append(sp.port)
    return tuple(ports)


@dataclass(frozen=True)
class Sovereignty:
    """Egress policy. The allowlist is the complete set of destinations the control
    plane may itself contact; agents and sandboxes get none of it."""

    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost", "::1", "0.0.0.0")
    # Ports on loopback that hold local inference backends.
    allowed_ports: tuple[int, ...] = tuple(dict.fromkeys(
        (11434, 8000, 8080, 5000, 1234) + _backend_ports()))
    enforce: bool = os.environ.get("SOVEREIGN_ENFORCE_EGRESS", "1") == "1"
    nft_table: str = "sovereign"


@dataclass(frozen=True)
class RuntimeLimits:
    """Resource-governed runtime tunables.

    These defaults are sized for an 8 GB / 15 GB workstation. `probe` overwrites the
    memory numbers with measured values at first boot.
    """

    # A ceiling, not a target: the residency manager lowers it to what the
    # measured VRAM can actually hold, so the same default is 1 on an 8 GB
    # laptop and several on an 80 GB server.
    max_concurrent_agents: int = int(os.environ.get("SOVEREIGN_MAX_AGENTS", "4"))
    max_concurrent_per_user: int = int(os.environ.get("SOVEREIGN_MAX_AGENTS_PER_USER",
                                                      "2"))
    max_queue_depth: int = int(os.environ.get("SOVEREIGN_MAX_QUEUE", "256"))
    task_time_budget_s: int = int(os.environ.get("SOVEREIGN_TASK_BUDGET_S", "1800"))
    step_time_budget_s: int = 420
    heartbeat_interval_s: float = 5.0
    heartbeat_grace_s: float = 180.0
    max_agent_steps: int = 24
    # Anti-thrash: once a model is resident, keep it for at least this long before an
    # eviction is allowed, unless a CRITICAL task demands otherwise.
    min_residency_dwell_s: float = 45.0
    # Priority aging: a queued task gains one effective priority level every N seconds.
    priority_aging_s: float = 90.0
    # VRAM we never allocate, leaving room for the display server and CUDA context.
    vram_reserve_mb: int = int(os.environ.get("SOVEREIGN_VRAM_RESERVE_MB", "700"))
    # RAM we never allocate.
    ram_reserve_mb: int = int(os.environ.get("SOVEREIGN_RAM_RESERVE_MB", "1500"))
    # Refuse rather than hang (T8): every generation call carries a hard deadline.
    generation_timeout_s: int = 900
    # A task whose estimated context exceeds the measured KV budget is rejected.
    kv_safety_factor: float = 0.85


@dataclass(frozen=True)
class Backends:
    ollama_url: str = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
    # Several Ollama servers, served as one backend: a second instance whose
    # weights live on a different disk, or one per GPU host. Each model is
    # routed to whichever endpoint actually serves it.
    ollama_urls: tuple[str, ...] = tuple(
        u.strip().rstrip("/") for u in (
            os.environ.get("OLLAMA_URLS")
            or os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")).split(",")
        if u.strip())
    # Any OpenAI-compatible local server (vLLM, llama.cpp --server, FreeToken, LM Studio).
    openai_compat_url: str = os.environ.get("OPENAI_COMPAT_URL", "")
    openai_compat_models: tuple[str, ...] = tuple(
        m for m in os.environ.get("OPENAI_COMPAT_MODELS", "").split(",") if m
    )
    llamacpp_url: str = os.environ.get("LLAMACPP_URL", "")


@dataclass(frozen=True)
class Knowledge:
    embed_model: str = os.environ.get(
        "SOVEREIGN_EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2"
    )
    embed_dim: int = 384
    chunk_tokens: int = 200
    chunk_overlap: int = 45
    retrieve_k: int = 24
    rerank_k: int = 6
    # Hybrid weighting between dense cosine and lexical BM25.
    dense_weight: float = 0.62


@dataclass(frozen=True)
class Sandbox:
    engine: str = os.environ.get("SOVEREIGN_SANDBOX", "auto")  # auto|bwrap|unshare|none
    cpu_seconds: int = 30
    wall_seconds: int = 60
    memory_mb: int = 2048
    max_output_bytes: int = 256_000
    max_processes: int = 64
    max_file_mb: int = 64


@dataclass(frozen=True)
class Settings:
    host: str = os.environ.get("SOVEREIGN_HOST", "127.0.0.1")
    port: int = int(os.environ.get("SOVEREIGN_PORT", "8794"))
    # The address member devices reach this node at (the TLS terminator on
    # :8443 in a domain deployment). Used in evidence links inside deliverables
    # and in manifests; defaults to the bind address.
    public_url: str = os.environ.get("SOVEREIGN_PUBLIC_URL", "").rstrip("/")
    # Staging for execute_remote (spec §12.1): explicit copies, never mounts.
    staging_max_file_mb: int = int(os.environ.get("SOVEREIGN_STAGE_FILE_MB", "8"))
    staging_max_session_mb: int = int(os.environ.get("SOVEREIGN_STAGE_SESSION_MB",
                                                     "64"))
    # standard | controlled | strict  (locked architecture §19)
    policy_mode: str = os.environ.get("SOVEREIGN_POLICY_MODE", "controlled")
    org_name: str = os.environ.get("SOVEREIGN_ORG", "Bharat Refineries Limited")
    org_unit: str = os.environ.get("SOVEREIGN_UNIT", "Mechanical Inspection Department")
    sovereignty: Sovereignty = field(default_factory=Sovereignty)
    limits: RuntimeLimits = field(default_factory=RuntimeLimits)
    backends: Backends = field(default_factory=Backends)
    knowledge: Knowledge = field(default_factory=Knowledge)
    sandbox: Sandbox = field(default_factory=Sandbox)


settings = Settings()
