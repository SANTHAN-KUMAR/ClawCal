"""The engine supervisor (sovereign-workbench-v2.md §6.1, §7.3, T8, T10).

Starts the local engine the manifest names, with its pinned flags, bound to
loopback, and nothing else:

* the argv is built from the verified manifest; `--host`/`--port` are set here
  structurally and are on every `never` list, so no flag can widen the bind;
* a flag on the manifest's `never` list refuses the launch;
* **every wait has a deadline.** The failure shape of both FreeToken's auto-
  configuration (T8) and opencode's startup fetch (#38723) is not an error, it
  is a wait. Engine start, readiness and every request carry a wall-clock
  limit that turns a hang into a message.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from . import bundle, trust

BINARIES = {"llama.cpp": ("llama-server",), "freetoken": ("ft", "freetoken")}


class EngineError(RuntimeError):
    pass


def argv_for(man: dict[str, Any], weights: str, *,
             extra_flags: list[str] | None = None) -> list[str]:
    eng = man["engine"]
    names = BINARIES.get(eng["name"])
    if not names:
        raise EngineError(f"no supervisor for engine {eng['name']!r}")
    # The node-shipped, hash-verified build first; an operator override next;
    # whatever is on PATH last.
    built = (trust.load_state().get("engine_build") or {}).get("binary")
    binary = (built if built and Path(built).exists() else None) \
        or os.environ.get("CLAWCAL_ENGINE_BINARY") \
        or next((shutil.which(n) for n in names if shutil.which(n)), None)
    if not binary:
        raise EngineError(f"the {eng['name']} binary ({' or '.join(names)}) is not "
                          f"installed on this device; the client bundle for this "
                          f"class ships it")
    flags = list(eng["flags"]) + list(extra_flags or [])
    for f in flags:
        for n in eng["never"]:
            if f == n or f.startswith(n + "="):
                raise EngineError(f"{f} is on the manifest's never list")
    host, port = eng.get("host", "127.0.0.1"), str(eng.get("port", 8080))
    if host not in ("127.0.0.1", "::1"):
        raise EngineError("the engine must bind to loopback")
    if eng["name"] == "llama.cpp":
        dev = pick_device(binary)
        return [binary, "-m", weights, "--host", host, "--port", port,
                *(["--device", dev] if dev and "--device" not in flags else []),
                *flags]
    return [binary, "serve", "--model", weights, "--host", host, "--port", port,
            *flags]


def pick_device(binary: str) -> str | None:
    """The adapter the profile was measured on.

    A laptop's Vulkan build lists the integrated GPU first; left to itself the
    engine can load an 8 GB model onto the iGPU and share system RAM with it.
    The device whose name matches the GPU the profiler measured is pinned; with
    no match, a discrete adapter (the largest reported memory) is preferred."""
    import re
    from . import profiler
    try:
        out = subprocess.run([binary, "--list-devices"], capture_output=True,
                             text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    devs = [(m.group(1), m.group(2), int(m.group(3))) for m in re.finditer(
        r"^\s*(\w+\d+):\s*(.+?)\s*\((\d+) MiB", out, re.M)]
    if len(devs) <= 1:
        return None
    measured = [g.get("name", "") for g in (profiler._nvidia() or {}).get("gpus", [])]
    for dev_id, name, _mb in devs:
        if any(m and (m in name or name in m) for m in measured):
            return dev_id
    discrete = [d for d in devs if "intel" not in d[1].lower()] or devs
    return max(discrete, key=lambda d: d[2])[0]


def _state_file() -> Path:
    return trust.home() / "engine.json"


def start(*, extra_flags: list[str] | None = None) -> dict[str, Any]:
    checked = bundle.verify_launch(extra_flags)
    man = checked["manifest"]
    weights = (trust.load_state().get("weights") or {}).get("path")
    if not weights:
        raise EngineError("no verified weights installed; run `clawcal device "
                          "weights`")
    argv = argv_for(man, weights, extra_flags=extra_flags)
    env = dict(os.environ, **{k: str(v) for k, v in (man["engine"].get("env") or
                                                     {}).items()})
    log = (trust.home() / "engine.log").open("ab")
    proc = subprocess.Popen(argv, env=env, stdout=log, stderr=subprocess.STDOUT,
                            start_new_session=True)
    base = f"http://{man['engine']['host']}:{man['engine']['port']}"
    deadline = time.time() + float(man["engine"].get("start_timeout_s", 180))
    while time.time() < deadline:
        if proc.poll() is not None:
            raise EngineError(f"the engine exited with {proc.returncode} while "
                              f"starting; see {trust.home() / 'engine.log'}")
        try:
            with urllib.request.urlopen(base + "/health", timeout=2) as r:
                if r.status == 200:
                    break
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.5)
    else:
        os.killpg(proc.pid, signal.SIGTERM)
        trust.log("engine_timeout", {"engine": man["engine"]["name"]})
        raise EngineError(f"the engine was not ready within "
                          f"{man['engine'].get('start_timeout_s', 180)} s and was "
                          f"stopped: a wait was turned into an error (T8)")
    _state_file().write_text(json.dumps({"pid": proc.pid, "base_url": base + "/v1",
                                         "argv": argv, "started": time.time()}))
    trust.log("engine_started", {"engine": man["engine"]["name"],
                                 "model": man["model"]["id"], "pid": proc.pid})
    return {"pid": proc.pid, "base_url": base + "/v1", "argv": argv}


def stop() -> bool:
    try:
        st = json.loads(_state_file().read_text())
    except (OSError, ValueError):
        return False
    try:
        os.killpg(int(st["pid"]), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    _state_file().unlink(missing_ok=True)
    trust.log("engine_stopped", {"pid": st.get("pid")})
    return True


def status() -> dict[str, Any]:
    try:
        st = json.loads(_state_file().read_text())
    except (OSError, ValueError):
        return {"running": False}
    try:
        os.kill(int(st["pid"]), 0)
        return {"running": True, **st}
    except OSError:
        return {"running": False, "stale": st}


def busy() -> bool:
    """Has the running engine started work? (Its slots report decoding.)"""
    st = status()
    if not st.get("running"):
        return False
    base = st["base_url"].rsplit("/v1", 1)[0]
    try:
        with urllib.request.urlopen(base + "/slots", timeout=2) as r:
            slots = json.loads(r.read())
        return any(s.get("is_processing") or s.get("n_decoded") for s in slots)
    except (OSError, ValueError):
        return False
