"""B5 on the client: measure this machine (sovereign-workbench-v2.md §7.1).

Standard library only. Every figure carries its basis — `measured` (timed
here), `driver` (reported by the OS or driver), `link-rated` (PCIe generation ×
width, not a transfer), `prior` (a stated assumption) or `unknown` — because the
node's classification is only as honest as its inputs.

The node classifies; this module only measures. A machine with no GPU runtime
the client can see reports `cpu-only` rather than guessing.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from .trust import platform_name

_PCIE_LANE_GBS = {1: 0.25, 2: 0.5, 3: 0.985, 4: 1.969, 5: 3.938, 6: 7.563}


def _run(cmd: list[str], timeout: float = 8.0) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _ram() -> tuple[float, float, str]:
    """(total MB, available MB, basis)."""
    if os.path.exists("/proc/meminfo"):
        vals = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                k, _, v = line.partition(":")
                vals[k] = float(v.split()[0]) / 1024.0
        return vals.get("MemTotal", 0), vals.get("MemAvailable", 0), "driver"
    if platform.system() == "Darwin":
        total = float(_run(["sysctl", "-n", "hw.memsize"]) or 0) / 1e6
        return total, 0.0, "driver"
    if platform.system() == "Windows":
        import ctypes

        class MS(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]
        ms = MS()
        ms.dwLength = ctypes.sizeof(MS)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(ms))  # type: ignore[attr-defined]
        return ms.ullTotalPhys / 1e6, ms.ullAvailPhys / 1e6, "driver"
    return 0.0, 0.0, "unknown"


def _nvidia() -> dict[str, Any] | None:
    if not shutil.which("nvidia-smi"):
        return None
    out = _run(["nvidia-smi", "--query-gpu=name,memory.total,memory.free,"
                "compute_cap,pcie.link.gen.max,pcie.link.width.max",
                "--format=csv,noheader,nounits"])
    gpus = []
    for line in out.splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) < 6:
            continue

        def f(i: int) -> float:
            try:
                return float(p[i])
            except ValueError:
                return 0.0
        gpus.append({"name": p[0], "vram_mb": f(1), "vram_free_mb": f(2),
                     "compute": p[3], "pcie_gen": int(f(4)), "pcie_width": int(f(5))})
    return {"backend": "cuda", "gpus": gpus} if gpus else None


def _apple() -> dict[str, Any] | None:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        return None
    total = float(_run(["sysctl", "-n", "hw.memsize"]) or 0) / 1e6
    # Metal's recommendedMaxWorkingSetSize is not reachable from the standard
    # library; the node applies its stated prior (65 % of unified memory)
    # unless an operator supplies the measured ceiling.
    ceiling = os.environ.get("CLAWCAL_UNIFIED_CEILING_MB")
    return {"backend": "metal", "unified_ceiling_mb": float(ceiling) if ceiling else None,
            "unified_ceiling_basis": "operator-supplied" if ceiling else "prior",
            "gpus": [{"name": _run(["sysctl", "-n", "machdep.cpu.brand_string"]),
                      "vram_mb": total}]}


def nvme_read_gbs(path: str | None = None, size_mb: int = 512) -> dict[str, Any]:
    """Sequential read speed of the model directory's disk.

    Writes a file, drops it from the page cache (fadvise DONTNEED after fsync),
    then times a cold read. Where the cache cannot be dropped the figure is
    labelled, because a cached read measures RAM, not the disk.
    """
    base = Path(path or tempfile.gettempdir())
    base.mkdir(parents=True, exist_ok=True)
    f = base / f".clawcal-nvme-probe-{os.getpid()}"
    chunk = os.urandom(1 << 20)
    try:
        with open(f, "wb") as fh:
            for _ in range(size_mb):
                fh.write(chunk)
            fh.flush()
            os.fsync(fh.fileno())
        dropped = False
        if hasattr(os, "posix_fadvise"):
            fd = os.open(f, os.O_RDONLY)
            try:
                os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
                dropped = True
            finally:
                os.close(fd)
        t0 = time.perf_counter()
        with open(f, "rb", buffering=0) as fh:
            while fh.read(8 << 20):
                pass
        dt = time.perf_counter() - t0
        gbs = round(size_mb / 1000.0 / dt, 2) if dt > 0 else 0.0
        return {"read_gbs": gbs, "bytes": size_mb << 20, "path": str(base),
                "basis": "measured (cold, page cache dropped)" if dropped
                else "measured (page cache NOT dropped; likely overstated)"}
    except OSError as exc:
        return {"read_gbs": None, "basis": f"unknown ({exc})"}
    finally:
        try:
            f.unlink()
        except OSError:
            pass


def _cpu() -> dict[str, Any]:
    flags: list[str] = []
    model = platform.processor()
    if os.path.exists("/proc/cpuinfo"):
        with open("/proc/cpuinfo") as fh:
            for line in fh:
                if line.startswith("model name") and not model:
                    model = line.split(":", 1)[1].strip()
                if line.startswith("flags"):
                    have = set(line.split(":", 1)[1].split())
                    flags = [x for x in ("avx2", "avx512f", "avx512_bf16", "amx_tile",
                                         "f16c", "fma") if x in have]
                    break
    elif platform.machine() in ("arm64", "aarch64"):
        flags = ["neon"]
    return {"model": model, "cores": os.cpu_count() or 0, "flags": flags}


def measure(*, nvme: bool = True, nvme_path: str | None = None,
            nvme_mb: int = 512) -> dict[str, Any]:
    total, avail, ram_basis = _ram()
    nv, ap = _nvidia(), _apple()
    plat = platform_name()
    prof: dict[str, Any] = {
        "schema": "workbench.profile/v2", "platform": plat,
        "os": f"{platform.system()} {platform.release()}",
        "arch": platform.machine(), "ram_mb": round(total),
        "ram_available_mb": round(avail), "ram_basis": ram_basis,
        "cpu": _cpu(), "measured_at": round(time.time(), 3),
    }
    if nv:
        g = nv["gpus"]
        gen = min(x["pcie_gen"] for x in g)
        width = min(x["pcie_width"] for x in g)
        prof.update({
            "topology": "discrete", "backend": "cuda", "gpus": g,
            "vram_mb": sum(x["vram_mb"] for x in g),
            "vram_free_mb": sum(x["vram_free_mb"] for x in g),
            "pcie": {"gbs": round(_PCIE_LANE_GBS.get(gen, 0) * width, 2),
                     "gen": gen, "width": width,
                     "basis": "link-rated (generation × width from the driver; "
                              "not a transfer benchmark)"}})
    elif ap:
        prof.update({"topology": "unified", "backend": "metal", "gpus": ap["gpus"],
                     "vram_mb": 0, "unified_ceiling_mb": ap["unified_ceiling_mb"],
                     "unified_ceiling_basis": ap["unified_ceiling_basis"]})
    else:
        prof.update({"topology": "cpu-only", "backend": "cpu", "vram_mb": 0,
                     "gpus": []})
    prof["nvme"] = nvme_read_gbs(nvme_path, nvme_mb) if nvme else {
        "read_gbs": None, "basis": "not measured"}
    prof["tpm"] = any(os.path.exists(p) for p in ("/dev/tpmrm0", "/dev/tpm0"))
    prof["secure_enclave"] = plat == "macos" and platform.machine() == "arm64"
    prof["virtualization"] = [v for v, ok in (
        ("kvm", os.path.exists("/dev/kvm")),
        ("wsl2", "microsoft" in platform.release().lower()),
        ("bwrap", bool(shutil.which("bwrap"))),
        ("wasmtime", bool(shutil.which("wasmtime")))) if ok]
    prof["battery"] = any(Path("/sys/class/power_supply").glob("BAT*")) \
        if Path("/sys/class/power_supply").exists() else None
    return prof
