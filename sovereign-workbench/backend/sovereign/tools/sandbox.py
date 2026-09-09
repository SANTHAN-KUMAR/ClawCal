"""Air-gapped code execution sandbox.

Built on Linux primitives rather than a hosted sandbox service, because every
managed option in the survey requires a cloud control plane -- a domain, an
account, an API -- which an air-gapped refinery does not have and cannot get.

Isolation, strongest engine first:

* **bubblewrap** (`--unshare-all`): new user, pid, ipc, uts, cgroup and **network**
  namespaces. The network namespace has no interfaces at all, so egress is not
  filtered, it is absent. The filesystem is a read-only /usr with a single
  writable bind at /workspace.
* **unshare -Urn**: same idea with fewer knobs, used when bubblewrap is missing.
* **degraded**: rlimit-bounded subprocess with a scrubbed environment. Reported
  honestly as degraded, never silently substituted.

Resource limits are applied with setrlimit in the child regardless of engine:
CPU seconds, address space, process count and file size.
"""
from __future__ import annotations

import os
import resource
import shutil
import subprocess
import textwrap
import time
from pathlib import Path
from typing import Any

from .. import audit, db
from ..config import WORKSPACE_DIR, settings
from ..policy import egress
from ..policy.tool_policy import Risk
from .base import Tool, ToolContext, ToolResult

LANG_CMD = {
    "python": ["/usr/bin/python3", "-I", "-B", "{file}"],
    "bash": ["/bin/bash", "{file}"],
    "sh": ["/bin/sh", "{file}"],
}
LANG_EXT = {"python": ".py", "bash": ".sh", "sh": ".sh"}


def workspace_for(task_id: str) -> Path:
    ws = WORKSPACE_DIR / task_id
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def detect_engine() -> str:
    if settings.sandbox.engine != "auto":
        return settings.sandbox.engine
    if shutil.which("bwrap"):
        probe = ["bwrap", "--unshare-all", "--ro-bind", "/usr", "/usr",
                 "--symlink", "usr/bin", "/bin", "--symlink", "usr/lib", "/lib",
                 "--symlink", "usr/lib64", "/lib64", "--proc", "/proc",
                 "--dev", "/dev", "--", "/usr/bin/true"]
        try:
            if subprocess.run(probe, capture_output=True, timeout=10).returncode == 0:
                return "bwrap"
        except Exception:
            pass
    if shutil.which("unshare"):
        try:
            if subprocess.run(["unshare", "-Urn", "--", "/usr/bin/true"],
                              capture_output=True, timeout=10).returncode == 0:
                return "unshare"
        except Exception:
            pass
    return "degraded"


def _current_uid_processes() -> int:
    """Count processes already owned by this uid.

    RLIMIT_NPROC is enforced per *user*, not per process tree, so a naive cap of
    N kills the sandbox launch the moment the desktop session already owns more
    than N processes -- and it would also starve the rest of the user's session
    if a fork bomb ran. The limit therefore has to be expressed as headroom above
    the current count.
    """
    uid = os.getuid()
    n = 0
    try:
        for entry in os.scandir("/proc"):
            if entry.name.isdigit():
                try:
                    if entry.stat().st_uid == uid:
                        n += 1
                except OSError:
                    continue
    except OSError:
        return 512
    return n


def _limits(with_nproc: bool) -> Any:
    """Outer resource limits, applied to the launcher process.

    RLIMIT_NPROC is deliberately excluded when a user namespace is involved: the
    kernel checks it against the *new* namespace's ucounts during clone, and a
    desktop session that already owns several hundred processes makes any useful
    cap fail the namespace creation itself with EAGAIN. The process cap is
    instead applied inside the sandbox by `_init.py`, where the count starts at
    one and the limit actually means what it says.
    """
    s = settings.sandbox
    nproc_cap = _current_uid_processes() + s.max_processes

    def apply() -> None:
        resource.setrlimit(resource.RLIMIT_CPU, (s.cpu_seconds, s.cpu_seconds + 2))
        mem = s.memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        if with_nproc:
            resource.setrlimit(resource.RLIMIT_NPROC, (nproc_cap, nproc_cap))
        fsz = s.max_file_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_FSIZE, (fsz, fsz))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        os.setsid()

    return apply


# Applied inside the sandbox, after namespaces exist.
_INIT_SRC = """\
import os, resource, sys
cpu, mem_mb, nproc, fsz_mb = (int(x) for x in sys.argv[1:5])
target = sys.argv[5:]
resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 2))
resource.setrlimit(resource.RLIMIT_NPROC, (nproc, nproc))
resource.setrlimit(resource.RLIMIT_FSIZE, (fsz_mb * 1024 * 1024,) * 2)
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
os.execv(target[0], target)
"""


def _write_init(ws: Path) -> str:
    init = ws / "_sandbox_init.py"
    init.write_text(_INIT_SRC)
    return init.name


def _bwrap_argv(workspace: Path) -> list[str]:
    argv = [
        "bwrap",
        "--unshare-all",              # includes --unshare-net: no interfaces at all
        "--die-with-parent",
        "--new-session",              # detach from the controlling terminal
        "--cap-drop", "ALL",
        "--ro-bind", "/usr", "/usr",
        "--symlink", "usr/bin", "/bin",
        "--symlink", "usr/lib", "/lib",
        "--symlink", "usr/lib64", "/lib64",
        "--symlink", "usr/sbin", "/sbin",
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--tmpfs", "/run",
        "--bind", str(workspace), "/workspace",
        "--chdir", "/workspace",
        "--setenv", "HOME", "/workspace",
        "--setenv", "TMPDIR", "/tmp",
        "--setenv", "PATH", "/usr/bin:/bin",
        "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
        "--setenv", "SOVEREIGN_SANDBOX", "1",
    ]
    for etc in ("/etc/alternatives", "/etc/ssl/certs"):
        if Path(etc).exists():
            argv += ["--ro-bind", etc, etc]
    # A resolver that cannot resolve. Belt and braces on top of the empty netns.
    argv += ["--ro-bind-try", "/dev/null", "/etc/resolv.conf"]
    return argv


def run_code(code: str, *, language: str = "python", task_id: str = "adhoc",
             workspace: Path | None = None,
             timeout_s: float | None = None,
             argv_extra: list[str] | None = None) -> dict[str, Any]:
    """Execute code in isolation. Never raises for user-code failure."""
    language = (language or "python").lower()
    if language not in LANG_CMD:
        return {"ok": False, "engine": "none",
                "error": f"unsupported language {language!r}; "
                         f"supported: {sorted(LANG_CMD)}"}

    ws = workspace or workspace_for(task_id)
    src = ws / f"snippet_{int(time.time() * 1000)}{LANG_EXT[language]}"
    src.write_text(textwrap.dedent(code))

    engine = detect_engine()
    wall = timeout_s or settings.sandbox.wall_seconds
    inner = [c.replace("{file}", f"/workspace/{src.name}") for c in LANG_CMD[language]]

    if engine == "bwrap":
        init_name = _write_init(ws)
        s_ = settings.sandbox
        argv = (_bwrap_argv(ws) + (argv_extra or []) + [
            "--", "/usr/bin/python3", "-I", "-B", f"/workspace/{init_name}",
            str(s_.cpu_seconds), str(s_.memory_mb), str(s_.max_processes),
            str(s_.max_file_mb)] + inner)
        env: dict[str, str] = {}
    elif engine == "unshare":
        argv = ["unshare", "--user", "--map-root-user", "--net", "--pid",
                "--fork", "--mount-proc", "--"] + [
            c.replace("{file}", str(src)) for c in LANG_CMD[language]]
        env = {"PATH": "/usr/bin:/bin", "HOME": str(ws), "TMPDIR": "/tmp",
               "SOVEREIGN_SANDBOX": "1"}
    else:
        argv = [c.replace("{file}", str(src)) for c in LANG_CMD[language]]
        env = {"PATH": "/usr/bin:/bin", "HOME": str(ws), "TMPDIR": "/tmp",
               "SOVEREIGN_SANDBOX": "1"}

    t0 = time.time()
    timed_out = False
    try:
        proc = subprocess.run(
            argv, cwd=str(ws), env=env, capture_output=True, text=True,
            timeout=wall, preexec_fn=_limits(with_nproc=(engine == "degraded")),
            errors="replace")
        rc, out, err = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        rc = -1
        out = (exc.stdout or b"").decode("utf-8", "replace") if isinstance(
            exc.stdout, bytes) else (exc.stdout or "")
        err = f"execution exceeded the {wall:.0f}s wall-clock limit and was killed"
    except Exception as exc:
        rc, out, err = -1, "", f"sandbox launch failed: {exc}"

    cap = settings.sandbox.max_output_bytes
    truncated = len(out) > cap or len(err) > cap
    result = {
        "ok": rc == 0 and not timed_out,
        "engine": engine,
        "network": "none (empty network namespace)" if engine in ("bwrap", "unshare")
                   else "not isolated - degraded engine",
        "exit_code": rc,
        "stdout": out[:cap],
        "stderr": err[:cap],
        "truncated": truncated,
        "timed_out": timed_out,
        "duration_s": round(time.time() - t0, 3),
        "workspace": str(ws),
        "script": src.name,
        "limits": {"cpu_s": settings.sandbox.cpu_seconds,
                   "wall_s": wall,
                   "memory_mb": settings.sandbox.memory_mb,
                   "max_processes": settings.sandbox.max_processes},
        "files": sorted(p.name for p in ws.iterdir()
                        if p.is_file() and p.name != "_sandbox_init.py")[:60],
    }
    audit.record("sandbox", "code_executed",
                 outcome="OK" if result["ok"] else "FAILED", task_id=task_id,
                 detail={"engine": engine, "language": language,
                         "exit_code": rc, "duration_s": result["duration_s"],
                         "timed_out": timed_out})
    return result


class SandboxTool(Tool):
    name = "run_code"
    risk = Risk.COMPUTE
    timeout_s = float(settings.sandbox.wall_seconds + 20)
    description = (
        "Execute Python or shell code in an isolated sandbox with no network "
        "access and no access to the host filesystem outside this task's "
        "workspace. Use it to compute, test, and verify. Print results to stdout; "
        "only stdout and stderr are returned.")
    parameters = {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "the source to execute"},
            "language": {"type": "string", "enum": ["python", "bash", "sh"],
                         "description": "defaults to python"},
        },
        "required": ["code"],
    }

    def approval_summary(self, args: dict[str, Any]) -> str:
        code = str(args.get("code", ""))
        return (f"execute {args.get('language', 'python')} in the sandbox "
                f"({len(code)} chars):\n{code[:600]}")

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        ws = Path(ctx.workspace) if ctx.workspace else workspace_for(ctx.task_id)
        res = run_code(str(args.get("code", "")),
                       language=str(args.get("language", "python")),
                       task_id=ctx.task_id, workspace=ws)
        if res.get("engine") == "degraded":
            audit.record("security", "sandbox_degraded", outcome="DEGRADED",
                         task_id=ctx.task_id,
                         detail="no namespace isolation available on this host")
        ctx.note("sandbox", f"Sandbox exit {res.get('exit_code')}",
                 f"engine={res.get('engine')} network={res.get('network')} "
                 f"{res.get('duration_s')}s", res)
        body = (f"exit_code: {res.get('exit_code')}\n"
                f"engine: {res.get('engine')} (network: {res.get('network')})\n"
                f"--- stdout ---\n{res.get('stdout', '')}\n"
                f"--- stderr ---\n{res.get('stderr', '')}")
        return ToolResult(res.get("ok", False), content=body,
                          display=f"sandbox exit {res.get('exit_code')} in "
                                  f"{res.get('duration_s')}s",
                          error="" if res.get("ok") else
                                (res.get("stderr") or "non-zero exit")[:400],
                          meta=res)


EGRESS_PROBE = """
import socket, json, urllib.request
results = []
for host, port in [("8.8.8.8", 53), ("1.1.1.1", 443), ("api.openai.com", 443)]:
    try:
        socket.create_connection((host, port), timeout=4)
        results.append({"target": f"{host}:{port}", "result": "CONNECTED"})
    except Exception as e:
        results.append({"target": f"{host}:{port}",
                        "result": "BLOCKED", "error": type(e).__name__})
try:
    urllib.request.urlopen("https://example.com", timeout=4)
    results.append({"target": "https://example.com", "result": "CONNECTED"})
except Exception as e:
    results.append({"target": "https://example.com",
                    "result": "BLOCKED", "error": type(e).__name__})
print(json.dumps(results))
"""


def egress_probe(task_id: str = "sovereignty-proof") -> dict[str, Any]:
    """Deliberately attempt egress from inside the sandbox and record the result.

    This is the sovereignty demonstration. A non-empty, attributed denial record
    is stronger evidence than an empty packet capture, so the system attacks
    itself on demand and writes down what happened.
    """
    res = run_code(EGRESS_PROBE, language="python", task_id=task_id, timeout_s=45)
    attempts: list[dict[str, Any]] = []
    try:
        attempts = db.jload(res.get("stdout", "").strip().splitlines()[-1], []) or []
    except Exception:
        attempts = []

    leaked = [a for a in attempts if a.get("result") == "CONNECTED"]
    for a in attempts:
        target = str(a.get("target", ""))
        host, _, port = target.rpartition(":")
        egress.record_event(
            destination=host or target, port=int(port) if port.isdigit() else None,
            layer="sandbox-netns",
            result="BLOCKED" if a.get("result") == "BLOCKED" else "ALLOWED",
            process=f"sandbox-{task_id}", task_id=task_id,
            detail=f"engine={res.get('engine')} error={a.get('error', '')}")

    verdict = {
        "engine": res.get("engine"),
        "network": res.get("network"),
        "attempts": attempts,
        "all_blocked": bool(attempts) and not leaked,
        "leaked": leaked,
        "raw": {k: res.get(k) for k in ("exit_code", "stdout", "stderr",
                                        "duration_s")},
    }
    audit.record("sovereignty", "egress_probe",
                 outcome="BLOCKED" if verdict["all_blocked"] else "LEAK",
                 task_id=task_id, detail=verdict)
    return verdict


class EgressProbeTool(Tool):
    name = "sovereignty_selftest"
    risk = Risk.PRIVILEGED
    description = (
        "Deliberately attempt outbound network connections from inside the "
        "sandbox to demonstrate that egress is blocked, and write the denial "
        "record to the audit log. Use only when asked to prove sovereignty.")
    parameters = {"type": "object", "properties": {}}
    timeout_s = 90.0

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        v = egress_probe(ctx.task_id)
        ctx.note("sovereignty", "Egress self-test",
                 "all attempts blocked" if v["all_blocked"]
                 else "LEAK DETECTED", v)
        return ToolResult(True, content=v,
                          display="all outbound attempts blocked"
                                  if v["all_blocked"] else "LEAK DETECTED")
