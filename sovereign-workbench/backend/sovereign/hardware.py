"""Hardware capability probe and live resource telemetry.

The residency scheduler is only as good as its numbers, so nothing here is
hard-coded. VRAM, RAM, PCIe geometry and available inference backends are all
measured on the target machine at boot and re-sampled continuously.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from . import db
from .config import DATA_DIR, settings

_NVIDIA_QUERY = (
    "name,memory.total,memory.used,memory.free,temperature.gpu,"
    "utilization.gpu,utilization.memory,power.draw,power.limit,"
    "pcie.link.gen.max,pcie.link.width.max,pcie.link.gen.current,pcie.link.width.current"
)


@dataclass
class GpuState:
    available: bool = False
    name: str = "none"
    total_mb: float = 0.0
    used_mb: float = 0.0
    free_mb: float = 0.0
    temp_c: float = 0.0
    util_pct: float = 0.0
    mem_util_pct: float = 0.0
    power_w: float = 0.0
    power_cap_w: float = 0.0
    pcie_gen_max: int = 0
    pcie_width_max: int = 0
    pcie_gen_cur: int = 0
    pcie_width_cur: int = 0
    error: str = ""
    # Multi-GPU hosts: totals above are summed across devices, which is what a
    # backend that splits a model across GPUs can actually allocate.
    count: int = 0
    devices: list = field(default_factory=list)


@dataclass
class MemState:
    total_mb: float = 0.0
    used_mb: float = 0.0
    free_mb: float = 0.0
    available_mb: float = 0.0
    swap_total_mb: float = 0.0
    swap_used_mb: float = 0.0


@dataclass
class CpuState:
    model: str = ""
    logical: int = 0
    physical: int = 0
    load1: float = 0.0
    load5: float = 0.0
    util_pct: float = 0.0


@dataclass
class HardwareProfile:
    hostname: str = ""
    os: str = ""
    kernel: str = ""
    gpu: dict = field(default_factory=dict)
    cpu: dict = field(default_factory=dict)
    memory: dict = field(default_factory=dict)
    disk_free_gb: float = 0.0
    data_fs: str = ""
    backends: dict = field(default_factory=dict)
    sandbox_engines: list = field(default_factory=list)
    ocr: dict = field(default_factory=dict)
    # Where the model weights live, and whether that filesystem is FUSE. A cold
    # load measured off ntfs-3g is a property of the mount, not of the model.
    model_dir: str = ""
    model_fs: str = ""
    model_fs_warning: str = ""
    # Derived: how much VRAM the scheduler may actually hand out.
    usable_vram_mb: float = 0.0
    usable_ram_mb: float = 0.0
    # PCIe bandwidth matters more than VRAM once weights spill to host RAM (H1).
    pcie_est_gbs: float = 0.0
    probed_at: float = 0.0


def _run(cmd: list[str], timeout: float = 8.0) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout).stdout.strip()
    except Exception:
        return ""


_GPU_CACHE: tuple[float, "GpuState"] | None = None
_GPU_TTL_S = 1.0
_gpu_lock = threading.Lock()


def gpu_state(max_age_s: float = _GPU_TTL_S) -> GpuState:
    """Live GPU state, cached for about a second.

    Admission evaluates every queued task on every tick, and each evaluation
    asks for VRAM several times; uncached, a long queue meant hundreds of
    nvidia-smi processes per tick.
    """
    global _GPU_CACHE
    with _gpu_lock:
        if _GPU_CACHE and time.time() - _GPU_CACHE[0] < max_age_s:
            return _GPU_CACHE[1]
        g = _gpu_query()
        _GPU_CACHE = (time.time(), g)
        return g


def _gpu_query() -> GpuState:
    if not shutil.which("nvidia-smi"):
        return GpuState(error="nvidia-smi not present")
    out = _run(["nvidia-smi", f"--query-gpu={_NVIDIA_QUERY}",
                "--format=csv,noheader,nounits"])
    if not out:
        return GpuState(error="nvidia-smi returned nothing")

    devices: list[GpuState] = []
    for line in out.splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) < 4:
            continue

        def f(i: int) -> float:
            try:
                return float(p[i])
            except (IndexError, ValueError):
                return 0.0          # "[N/A]" on some datacentre fields

        def i_(i: int) -> int:
            return int(f(i))

        devices.append(GpuState(
            available=True, name=p[0], total_mb=f(1), used_mb=f(2), free_mb=f(3),
            temp_c=f(4), util_pct=f(5), mem_util_pct=f(6), power_w=f(7),
            power_cap_w=f(8), pcie_gen_max=i_(9), pcie_width_max=i_(10),
            pcie_gen_cur=i_(11), pcie_width_cur=i_(12)))
    if not devices:
        return GpuState(error="nvidia-smi output could not be parsed")
    if len(devices) == 1:
        d = devices[0]
        d.count = 1
        return d
    names = sorted({d.name for d in devices})
    n = len(devices)
    return GpuState(
        available=True,
        name=(f"{n}x {names[0]}" if len(names) == 1 else " + ".join(names)),
        total_mb=sum(d.total_mb for d in devices),
        used_mb=sum(d.used_mb for d in devices),
        free_mb=sum(d.free_mb for d in devices),
        temp_c=max(d.temp_c for d in devices),
        util_pct=sum(d.util_pct for d in devices) / n,
        mem_util_pct=sum(d.mem_util_pct for d in devices) / n,
        power_w=sum(d.power_w for d in devices),
        power_cap_w=sum(d.power_cap_w for d in devices),
        pcie_gen_max=min(d.pcie_gen_max for d in devices),
        pcie_width_max=min(d.pcie_width_max for d in devices),
        pcie_gen_cur=min(d.pcie_gen_cur for d in devices),
        pcie_width_cur=min(d.pcie_width_cur for d in devices),
        count=n,
        devices=[{"name": d.name, "total_mb": d.total_mb, "free_mb": d.free_mb,
                  "used_mb": d.used_mb, "util_pct": d.util_pct} for d in devices])


def usable_vram_mb(g: GpuState) -> float:
    """VRAM the scheduler may allocate: the reserve is held back per device."""
    return max(0.0, g.total_mb - settings.limits.vram_reserve_mb * max(1, g.count))


# ------------------------------------------------------------ host memory
#
# A model that does not fit in VRAM does not fail: the backend quietly places
# the remainder of its weights in host RAM. On a workstation that RAM is shared
# with the desktop, and swap is often zram — compressed RAM, not disk — so the
# first sign of an overcommitted load is the kernel's OOM killer taking the
# browser, the terminal, or the whole machine. Observed here: a 12 GB model
# admitted against 7.4 GB of free VRAM spilled ~5 GB into a host that had
# ~5.7 GB available, and systemd-oomd started killing the desktop.
#
# So host RAM is priced explicitly, every time a model is about to load.

RUNNER_OVERHEAD_MB = 500.0       # runner process, CUDA host buffers, graph
KV_ALLOWANCE_MB = 512.0          # a working context's cache, until priced exactly


def ram_reserve_mb(total_mb: float | None = None) -> float:
    """RAM never handed to a model: the configured floor or 12% of RAM."""
    total = total_mb if total_mb is not None else mem_state().total_mb
    return max(float(settings.limits.ram_reserve_mb), 0.12 * total)


def ram_headroom_mb(snap: dict[str, Any] | None = None) -> float:
    m = (snap or {}).get("memory") or asdict(mem_state())
    return max(0.0, float(m.get("available_mb", 0.0))
               - ram_reserve_mb(float(m.get("total_mb", 0.0)) or None))


def model_footprint_mb(card: Any, ctx_tokens: int | None = None) -> float:
    """Everything a loaded model occupies, wherever it ends up.

    The KV cache is priced from the model's real attention geometry at the
    context the request asks for: at 8K tokens an 8B model's cache is over a
    gigabyte, the difference between fitting a GPU and spilling onto the host.
    """
    weights = float(max(card.weights_mb or 0, card.est_vram_mb or 0,
                        getattr(card, "residency_mb", 0) or 0))
    per_1k = float(getattr(card, "kv_cost_per_1k", 0) or 0)
    kv = per_1k * (ctx_tokens / 1000.0) if (ctx_tokens and per_1k) else KV_ALLOWANCE_MB
    return weights + kv


def host_ram_need_mb(card: Any, vram_available_mb: float,
                     ctx_tokens: int | None = None) -> float:
    """Host RAM a load will take at its peak, given the VRAM it can have now.

    Two costs, and the larger governs. The steady one is the part of the model
    that does not fit the GPU. The transient one is the load itself: Ollama
    loaded every model here with UseMmap:false, reading the whole weights file
    into host memory before copying it to the GPU — so a 5.4 GB model that
    ends up entirely in VRAM still needs about 5.4 GB of RAM while it loads.
    Budgeting only the spill let such a load push a busy workstation into the
    OOM killer, twice. A backend known to memory-map weights (page cache,
    reclaimable) can declare SOVEREIGN_BACKEND_MMAP=1 to budget the spill only.
    """
    spill = max(0.0, model_footprint_mb(card, ctx_tokens) - max(0.0, vram_available_mb))
    steady = spill + RUNNER_OVERHEAD_MB
    if os.environ.get("SOVEREIGN_BACKEND_MMAP") == "1":
        return steady
    weights = float(card.weights_mb or card.est_vram_mb or 0)
    return max(steady, weights + RUNNER_OVERHEAD_MB)


def memory_pressure() -> str | None:
    """Why the host is already under memory pressure, or None.

    Headline free-memory numbers lag reality on a desktop with zram swap: by
    the time MemAvailable looks low the kernel is already stalling. The PSI
    stall share and swap exhaustion say so directly. Under pressure no new
    model is loaded, whatever the arithmetic says.
    """
    try:
        with open("/proc/pressure/memory") as fh:
            some = fh.readline()
        avg10 = float(some.split("avg10=")[1].split()[0])
        if avg10 >= float(os.environ.get("SOVEREIGN_PSI_LIMIT", "10")):
            return f"the kernel reports memory stalls ({avg10:.0f}% of the last 10 s)"
    except (OSError, IndexError, ValueError):
        pass
    try:
        vals = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                k, _, v = line.partition(":")
                vals[k] = float(v.split()[0])
        total, free = vals.get("SwapTotal", 0.0), vals.get("SwapFree", 0.0)
        if total > 0 and free / total < 0.05:
            return (f"swap is {100 - 100 * free / total:.0f}% used; the host has no "
                    f"memory slack left")
    except OSError:
        pass
    return None


def memory_verdict(card: Any, vram_available_mb: float,
                   snap: dict[str, Any] | None = None,
                   freed_ram_mb: float = 0.0,
                   ctx_tokens: int | None = None) -> tuple[bool, str, float, float]:
    """(fits, reason, need, room) for loading `card` now."""
    need = host_ram_need_mb(card, vram_available_mb, ctx_tokens)
    room = ram_headroom_mb(snap) + freed_ram_mb
    pressure = memory_pressure() if snap is None or "_no_pressure" not in snap else None
    if pressure:
        return False, (f"not loading {card.name}: {pressure}. Loading a model now "
                       f"would push this machine into the OOM killer"), need, room
    if need <= room:
        return True, (f"host RAM: needs about {need:.0f} MB, {room:.0f} MB available "
                      f"after the reserve"), need, room
    return False, (
        f"{card.name} would place about {need:.0f} MB in host RAM "
        f"({model_footprint_mb(card, ctx_tokens):.0f} MB footprint, {vram_available_mb:.0f} MB of "
        f"VRAM free for it), but only {room:.0f} MB is available after the "
        f"{ram_reserve_mb():.0f} MB reserve. Loading it would push this machine into "
        f"the OOM killer; free memory, or use a smaller model"), need, room


def model_dir() -> str:
    """Where the inference backend keeps weights, as best this process can tell."""
    explicit = os.environ.get("SOVEREIGN_MODEL_DIR") or os.environ.get("OLLAMA_MODELS")
    if explicit:
        return explicit
    # The backend usually runs as its own service; its environment says.
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open(f"/proc/{pid}/comm") as fh:
                    if fh.read().strip() != "ollama":
                        continue
                with open(f"/proc/{pid}/environ", "rb") as fh:
                    for kv in fh.read().split(b"\0"):
                        if kv.startswith(b"OLLAMA_MODELS="):
                            return kv.split(b"=", 1)[1].decode(errors="replace")
            except OSError:
                continue
    except OSError:
        pass
    for cand in (os.path.expanduser("~/.ollama/models"),
                 "/usr/share/ollama/.ollama/models", "/var/lib/ollama/models"):
        if os.path.isdir(cand):
            return cand
    return ""


def filesystem_of(path: str) -> str:
    if not path:
        return "unknown"
    p = path
    while p and not os.path.exists(p):
        p = os.path.dirname(p)
    return _run(["findmnt", "-no", "FSTYPE", "-T", p or "/"]) or "unknown"


def mem_state() -> MemState:
    vals: dict[str, float] = {}
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                k, _, rest = line.partition(":")
                vals[k] = float(rest.strip().split()[0]) / 1024.0
    except OSError:
        return MemState()
    total = vals.get("MemTotal", 0.0)
    avail = vals.get("MemAvailable", 0.0)
    return MemState(total_mb=total, free_mb=vals.get("MemFree", 0.0),
                    available_mb=avail, used_mb=total - avail,
                    swap_total_mb=vals.get("SwapTotal", 0.0),
                    swap_used_mb=vals.get("SwapTotal", 0.0) - vals.get("SwapFree", 0.0))


_last_cpu: tuple[float, float] | None = None


def cpu_state() -> CpuState:
    global _last_cpu
    model, physical = "", 0
    try:
        with open("/proc/cpuinfo") as fh:
            for line in fh:
                if line.startswith("model name") and not model:
                    model = line.split(":", 1)[1].strip()
                if line.startswith("cpu cores") and not physical:
                    physical = int(line.split(":", 1)[1])
    except OSError:
        pass
    l1, l5 = 0.0, 0.0
    try:
        l1, l5, _ = os.getloadavg()
    except OSError:
        pass
    util = 0.0
    try:
        with open("/proc/stat") as fh:
            parts = [float(x) for x in fh.readline().split()[1:]]
        idle, total = parts[3] + parts[4], sum(parts)
        if _last_cpu:
            di, dt = idle - _last_cpu[0], total - _last_cpu[1]
            if dt > 0:
                util = max(0.0, min(100.0, 100.0 * (1 - di / dt)))
        _last_cpu = (idle, total)
    except (OSError, IndexError, ValueError):
        pass
    return CpuState(model=model, logical=os.cpu_count() or 0, physical=physical,
                    load1=l1, load5=l5, util_pct=round(util, 1))


# Theoretical unidirectional GB/s per PCIe lane, by generation.
_PCIE_LANE_GBS = {1: 0.25, 2: 0.5, 3: 0.985, 4: 1.969, 5: 3.938, 6: 7.563}


def detect_backends() -> dict[str, Any]:
    """Which local inference backends are reachable right now."""
    import urllib.error
    import urllib.request

    found: dict[str, Any] = {}

    def probe(name: str, url: str, path: str) -> None:
        if not url:
            return
        try:
            with urllib.request.urlopen(url.rstrip("/") + path, timeout=2.5) as r:
                found[name] = {"url": url, "status": "up", "code": r.status}
        except Exception as exc:
            found[name] = {"url": url, "status": "down", "detail": str(exc)[:120]}

    urls = settings.backends.ollama_urls or (settings.backends.ollama_url,)
    for i, u in enumerate(urls):
        probe("ollama" if i == 0 else f"ollama@{u.rsplit(':', 1)[-1]}", u,
              "/api/version")
    probe("openai_compat", settings.backends.openai_compat_url, "/v1/models")
    probe("llamacpp", settings.backends.llamacpp_url, "/health")
    return found


def detect_sandboxes() -> list[str]:
    out = []
    if shutil.which("bwrap"):
        # bubblewrap is only useful if unprivileged user namespaces are permitted,
        # so probe it for real rather than trusting the binary's presence.
        cmd = ["bwrap", "--unshare-all", "--die-with-parent",
               "--ro-bind", "/usr", "/usr",
               "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib",
               "--symlink", "usr/lib64", "/lib64", "--symlink", "usr/sbin", "/sbin",
               "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
               "--new-session", "--", "/usr/bin/true"]
        try:
            if subprocess.run(cmd, capture_output=True, timeout=10).returncode == 0:
                out.append("bwrap")
        except Exception:
            pass
    if shutil.which("unshare"):
        r = subprocess.run(["unshare", "-Urn", "--", "/bin/true"],
                           capture_output=True, timeout=10)
        if r.returncode == 0:
            out.append("unshare")
    if shutil.which("podman"):
        out.append("podman")
    return out


def detect_ocr() -> dict[str, Any]:
    info: dict[str, Any] = {"tesseract": False, "langs": [], "pymupdf": False}
    if shutil.which("tesseract"):
        info["tesseract"] = True
        langs = _run(["tesseract", "--list-langs"])
        info["langs"] = [l for l in langs.splitlines()[1:] if l.strip()]
        info["version"] = (_run(["tesseract", "--version"]).splitlines() or [""])[0]
    try:
        import fitz  # noqa: F401
        info["pymupdf"] = True
    except ImportError:
        pass
    return info


def probe(persist: bool = True) -> HardwareProfile:
    g, m, c = gpu_state(), mem_state(), cpu_state()
    gen = g.pcie_gen_max or g.pcie_gen_cur
    width = g.pcie_width_max or g.pcie_width_cur
    pcie = round(_PCIE_LANE_GBS.get(gen, 0.0) * width, 2)

    try:
        fs = _run(["findmnt", "-no", "FSTYPE", "-T", str(DATA_DIR)]) or "unknown"
    except Exception:
        fs = "unknown"

    mdir = model_dir()
    mfs = filesystem_of(mdir)
    mwarn = ""
    if mfs.startswith("fuse") or mfs in ("ntfs", "ntfs3", "exfat", "vfat"):
        mwarn = (f"model weights are on a {mfs} filesystem ({mdir}); cold-load times "
                 f"measured here are bound by that mount, not by the model, and will "
                 f"not transfer to a server with local ext4/xfs NVMe")
    prof = HardwareProfile(
        hostname=platform.node(),
        os=f"{platform.system()} {platform.release()}",
        kernel=platform.version(),
        gpu=asdict(g), cpu=asdict(c), memory=asdict(m),
        disk_free_gb=round(shutil.disk_usage(DATA_DIR).free / 1e9, 2),
        data_fs=fs,
        backends=detect_backends(),
        sandbox_engines=detect_sandboxes(),
        ocr=detect_ocr(),
        model_dir=mdir, model_fs=mfs, model_fs_warning=mwarn,
        usable_vram_mb=usable_vram_mb(g),
        usable_ram_mb=max(0.0, m.total_mb - settings.limits.ram_reserve_mb),
        pcie_est_gbs=pcie,
        probed_at=time.time(),
    )
    if persist:
        db.upsert("hardware_profile",
                  {"id": 1, "payload": db.jdump(asdict(prof)), "ts": prof.probed_at},
                  key="id")
    return prof


def cached_profile() -> dict[str, Any]:
    row = db.query_one("SELECT payload FROM hardware_profile WHERE id=1")
    return db.jload(row["payload"], {}) if row else asdict(probe())


def snapshot() -> dict[str, Any]:
    """Cheap live sample for the resource view (no subprocess beyond nvidia-smi)."""
    g, m, c = gpu_state(), mem_state(), cpu_state()
    return {
        "gpu": asdict(g), "memory": asdict(m), "cpu": asdict(c),
        "usable_vram_mb": usable_vram_mb(g),
        "free_vram_mb": g.free_mb,
        "ts": time.time(),
    }


class TelemetrySampler(threading.Thread):
    """Background sampler feeding the resource view and the admission controller.

    Keeps a short ring buffer so the UI can draw a trace without hammering
    nvidia-smi once per browser frame.
    """

    def __init__(self, interval: float = 2.0, history: int = 180) -> None:
        super().__init__(daemon=True, name="telemetry")
        self.interval = interval
        self.history = history
        self._ring: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.latest: dict[str, Any] = snapshot()

    def run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                s = snapshot()
            except Exception:
                continue
            with self._lock:
                self.latest = s
                self._ring.append({
                    "ts": s["ts"],
                    "vram_used": s["gpu"].get("used_mb", 0),
                    "vram_total": s["gpu"].get("total_mb", 0),
                    "gpu_util": s["gpu"].get("util_pct", 0),
                    "ram_used": s["memory"].get("used_mb", 0),
                    "ram_total": s["memory"].get("total_mb", 0),
                    "cpu_util": s["cpu"].get("util_pct", 0),
                })
                if len(self._ring) > self.history:
                    del self._ring[: len(self._ring) - self.history]

    def series(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._ring)

    def stop(self) -> None:
        self._stop.set()


sampler = TelemetrySampler()
