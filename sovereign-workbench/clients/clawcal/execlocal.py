"""`execute_local`: running code on the client, by exception (spec §12.2).

Off by default. The lease says whether this device's grade may run code
locally, and the manifest says with what runtime; both are checked here, every
time. What runs has no network *by construction*, a wall-clock deadline, and
only the directory it was given.

    linux    bubblewrap: every namespace unshared (so no network interface
             exists), read-only /usr, the workspace bound at /workspace
    any      wasmtime (WASI): no sockets in the ABI; pure Python / wasm only
    macos    Apple Virtualization micro-VM — the adapter is not in this build,
             so the tool refuses with that reason rather than running unconfined
    windows  WSL2 with no network — likewise refused with its reason here
"""
from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from . import trust


class ExecRefused(RuntimeError):
    pass


def _policy() -> dict[str, Any]:
    lease = trust.enforce_lease()
    if not lease.get("execute_local"):
        raise ExecRefused(f"grade {lease.get('grade')} is not permitted local "
                          f"execution under this domain's policy; use "
                          f"execute_remote on the node")
    try:
        from . import bundle
        man = bundle.load_manifest()
        pol = (man.get("policy") or {}).get("execute_local") or {}
        if pol and not pol.get("enabled", False):
            raise ExecRefused(pol.get("reason") or "the manifest disables "
                                                   "execute_local")
        return pol
    except Exception as exc:                               # noqa: BLE001
        if isinstance(exc, ExecRefused):
            raise
        return {"runtime": "bwrap", "network": "none", "timeout_s": 120}


def run(command: str, workspace: Path, *, timeout_s: int | None = None
        ) -> dict[str, Any]:
    pol = _policy()
    runtime = pol.get("runtime", "bwrap")
    limit = min(int(timeout_s or pol.get("timeout_s", 120)), 600)
    ws = workspace.resolve()
    if not ws.is_dir():
        raise ExecRefused(f"{ws} is not a directory")
    if runtime == "bwrap":
        if not shutil.which("bwrap"):
            raise ExecRefused("bubblewrap is not installed; local execution is "
                              "unavailable rather than unconfined")
        argv = ["bwrap", "--unshare-all", "--die-with-parent", "--new-session",
                "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin",
                "--symlink", "usr/lib", "/lib", "--symlink", "usr/lib64", "/lib64",
                "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
                "--bind", str(ws), "/workspace", "--chdir", "/workspace",
                "--setenv", "HOME", "/workspace", "--", "/bin/sh", "-c", command]
    elif runtime == "wasi":
        if not shutil.which("wasmtime"):
            raise ExecRefused("wasmtime is not installed; WASI execution is "
                              "unavailable")
        argv = ["wasmtime", "run", "--dir", f"{ws}::/workspace", "--", *command.split()]
    else:
        raise ExecRefused(f"the {runtime} adapter is not part of this client build; "
                          f"refusing rather than running without isolation")
    t0 = time.time()
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=limit)
        rc, out, err, timed = r.returncode, r.stdout, r.stderr, False
    except subprocess.TimeoutExpired as exc:
        rc, timed = -1, True
        out = (exc.stdout or b"").decode() if isinstance(exc.stdout, bytes) else ""
        err = f"exceeded the {limit} s wall-clock limit and was killed"
    res = {"exit_code": rc, "stdout": out[-64000:], "stderr": err[-16000:],
           "timed_out": timed, "runtime": runtime, "network": "none",
           "duration_s": round(time.time() - t0, 3)}
    trust.log("execute_local", {"command": command[:300], "exit_code": rc,
                                "runtime": runtime, "timed_out": timed})
    return res
