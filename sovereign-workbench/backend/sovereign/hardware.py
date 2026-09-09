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


def gpu_state() -> GpuState:
    if not shutil.which("nvidia-smi"):
        return GpuState(error="nvidia-smi not present")
    out = _run(["nvidia-smi", f"--query-gpu={_NVIDIA_QUERY}",
                "--format=csv,noheader,nounits"])
    if not out:
        return GpuState(error="nvidia-smi returned nothing")
    p = [x.strip() for x in out.splitlines()[0].split(",")]

    def f(i: int) -> float:
        try:
            return float(p[i])
        except (IndexError, ValueError):
            return 0.0

    def i_(i: int) -> int:
        return int(f(i))

    return GpuState(available=True, name=p[0], total_mb=f(1), used_mb=f(2), free_mb=f(3),
                    temp_c=f(4), util_pct=f(5), mem_util_pct=f(6), power_w=f(7),
                    power_cap_w=f(8), pcie_gen_max=i_(9), pcie_width_max=i_(10),
                    pcie_gen_cur=i_(11), pcie_width_cur=i_(12))


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

    probe("ollama", settings.backends.ollama_url, "/api/version")
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
        usable_vram_mb=max(0.0, g.total_mb - settings.limits.vram_reserve_mb),
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
        "usable_vram_mb": max(0.0, g.total_mb - settings.limits.vram_reserve_mb),
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
