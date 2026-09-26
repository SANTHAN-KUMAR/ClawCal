"""The client harness: opencode, attached to the node (spec §6.2, §6.3, T10).

The agent loop runs here, on the user's machine; the corpus, the gate and the
hardened sandbox stay on the node and are reached as MCP tools. This module
writes the harness configuration and runs it under the controls the review
found necessary:

* **no cloud fallback is a code rule, not a setting.** The only provider is the
  node (attached) or the local engine on 127.0.0.1 (detached); any other base
  URL is refused before opencode starts;
* **the catalogue is local.** A models catalogue naming only the node's model is
  written beside the config and the remote fetch is disabled
  (`OPENCODE_DISABLE_MODELS_FETCH`, `OPENCODE_MODELS_PATH`) — belt and braces
  with the egress policy, which rejects the fetch anyway;
* **update, share, LSP download, plugins, project config: off**, by config key
  *and* environment, because a key renamed in a version bump should still be
  caught by the other layer;
* **opencode's state is its own** (XDG dirs under the client home), so the
  user's personal opencode configuration never leaks into an org session;
* **a wall-clock deadline on launch.** The upstream failure (#38723) is a wait
  on `init`, not an error. No event within the startup deadline kills the
  harness and says why.

Every event the harness emits — model turns, tool calls, results — is appended
to the device's chained log, which the node anchors.
"""
from __future__ import annotations

import json
import os
import selectors
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import trust

STARTUP_DEADLINE_S = 60.0
TOTAL_DEADLINE_S = 1800.0
DISABLED_PROVIDERS = ["anthropic", "openai", "google", "openrouter", "opencode",
                      "github-copilot", "amazon-bedrock", "azure", "groq",
                      "mistral", "xai", "deepseek", "vercel", "huggingface"]
AGENT_PROMPTS = {
    "draft": "Draft from the organisation's own documents. Retrieve first; cite "
             "span ids; figures come from served spans or the calculate tool. "
             "Produce the deliverable with the deliver tool and report what the "
             "gate kept and stripped.",
    "code": "Work on the local repository with read/edit. Stage the files a test "
            "needs with stage, run them with execute_remote, and report the "
            "exit code and output faithfully.",
    "calc": "Every derived number comes from the calculate tool with named "
            "inputs whose sources you state. Show the steps.",
    "extract": "Read values with extract_values and read_page; never restate a "
               "figure from memory; carry each value's span id.",
    "vision": "The node has already read the page images; report what the tools "
              "return with their outcome labels, never more.",
}


class HarnessError(RuntimeError):
    pass


def harness_home() -> Path:
    p = trust.home() / "harness"
    p.mkdir(parents=True, exist_ok=True)
    return p


def check_base_url(url: str, node_url: str) -> None:
    """The compile rule: the node, or loopback. Nothing else, ever."""
    u, n = urlsplit(url), urlsplit(node_url)
    if u.hostname in ("127.0.0.1", "::1", "localhost"):
        return
    if (u.scheme, u.hostname, u.port) == (n.scheme, n.hostname, n.port):
        return
    raise HarnessError(f"refusing provider base URL {url}: only the trust-domain "
                       f"node ({node_url}) or 127.0.0.1 may serve this harness")


def build_config(*, node_url: str, token: str | None, admission: dict[str, Any],
                 detached_base: str | None = None) -> dict[str, Any]:
    """The opencode configuration for one admitted plan (§6.3)."""
    headers = {**trust.device_headers(),
               "X-ClawCal-Session": admission["session_id"]}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    spec = admission.get("spec_class", "draft")
    if admission.get("placement") == "client":
        if not detached_base:
            raise HarnessError("placement is client but no local engine is running; "
                               "start it with `clawcal engine start`")
        base, provider = detached_base, "local"
    else:
        base, provider = f"{node_url.rstrip('/')}/v1", "node"
    check_base_url(base, node_url)
    model_name = admission.get("model") or "generalist"
    cfg: dict[str, Any] = {
        "$schema": "https://opencode.ai/config.json",
        "provider": {provider: {
            "npm": "@ai-sdk/openai-compatible",
            "name": "trust-domain node" if provider == "node" else "local engine",
            "options": {"baseURL": base, "apiKey": "trust-domain-lease",
                        "headers": headers if provider == "node" else {}},
            "models": {"generalist": {"name": f"Generalist — {model_name} "
                                              f"({provider})",
                                      "tool_call": True,
                                      "limit": {"context": 32768, "output": 4096}}}}},
        "model": f"{provider}/generalist",
        "small_model": f"{provider}/generalist",
        "autoupdate": False, "share": "disabled", "instructions": [],
        "disabled_providers": DISABLED_PROVIDERS,
        "enabled_providers": [provider],
        "tools": {"webfetch": False, "websearch": False, "codesearch": False},
        "permission": {"bash": "ask", "edit": "ask", "webfetch": "deny"},
        "agent": {f"clawcal-{spec}": {
            "description": f"ClawCal {spec} work, placed on {admission['placement']}",
            "mode": "primary", "model": f"{provider}/generalist",
            "prompt": AGENT_PROMPTS.get(spec, AGENT_PROMPTS["draft"])
            + f"\n\nWhy this model, here: {admission.get('why', '')}"}},
    }
    if provider == "local":
        from . import slices
        if slices.local():
            # Detached with an exported slice: the device's own tool server.
            pkg_parent = str(Path(__file__).resolve().parents[1])
            cfg["mcp"] = {"local-tools": {
                "type": "local", "enabled": True,
                "command": [sys.executable, "-m", "clawcal.localtools"],
                "environment": {"CLAWCAL_HOME": str(trust.home()),
                                "PYTHONPATH": pkg_parent}}}
    if admission.get("node_tools") and provider == "node":
        cfg["mcp"] = {"node-tools": {"type": "remote",
                                     "url": f"{node_url.rstrip('/')}/mcp",
                                     "enabled": True, "headers": headers}}
    return cfg


def models_catalogue(provider: str, base: str, model_name: str) -> dict[str, Any]:
    """A models.dev-shaped catalogue naming only the one model this plan may use."""
    return {provider: {"id": provider, "name": "trust-domain node", "api": base,
                       "npm": "@ai-sdk/openai-compatible", "env": [],
                       "models": {"generalist": {
                           "id": "generalist", "name": f"Generalist — {model_name}",
                           "tool_call": True, "attachment": False,
                           "reasoning": False, "temperature": True,
                           "release_date": "2026-09-25",
                           "limit": {"context": 32768, "output": 4096},
                           "cost": {"input": 0, "output": 0}}}}}


def environment(cfg_path: Path, models_path: Path) -> dict[str, str]:
    h = harness_home()
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("OPENAI_", "ANTHROPIC_", "GEMINI_", "GOOGLE_",
                                "OPENROUTER_", "OPENCODE_"))}
    env.update({
        "OPENCODE_CONFIG": str(cfg_path),
        "OPENCODE_MODELS_PATH": str(models_path),
        "OPENCODE_DISABLE_MODELS_FETCH": "1",
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
        "OPENCODE_DISABLE_SHARE": "1",
        "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
        "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
        "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
        "OPENCODE_DISABLE_CLAUDE_CODE": "1",
        "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
        "OPENCODE_DISABLE_EMBEDDED_WEB_UI": "1",
        "XDG_CONFIG_HOME": str(h / "config"), "XDG_DATA_HOME": str(h / "data"),
        "XDG_CACHE_HOME": str(h / "cache"), "XDG_STATE_HOME": str(h / "state"),
    })
    return env


def binary() -> str:
    """The organisation's verified build first (§6.2: never an upstream release
    binary on a client); an explicit override next; an installed opencode last,
    for development only."""
    hb = (trust.load_state().get("harness_build") or {}).get("binary")
    for cand in (hb, os.environ.get("CLAWCAL_OPENCODE"), shutil.which("opencode"),
                 str(Path.home() / ".opencode" / "bin" / "opencode")):
        if cand and Path(cand).exists():
            return cand
    raise HarnessError("opencode is not installed; the client bundle ships the "
                       "organisation's own build")


def write(cfg: dict[str, Any], model_name: str) -> tuple[Path, Path]:
    h = harness_home()
    provider = next(iter(cfg["provider"]))
    base = cfg["provider"][provider]["options"]["baseURL"]
    cfg_path, models_path = h / "opencode.json", h / "models.json"
    cfg_path.write_text(json.dumps(cfg, indent=1))
    models_path.write_text(json.dumps(models_catalogue(provider, base, model_name)))
    for p in (cfg_path, models_path):
        os.chmod(p, 0o600)                 # it holds the lease token
    return cfg_path, models_path


def run(prompt: str, *, admission: dict[str, Any], cfg: dict[str, Any],
        workdir: Path, startup_deadline_s: float = STARTUP_DEADLINE_S,
        total_deadline_s: float = TOTAL_DEADLINE_S,
        on_event: Any = None, contacted: Any = None) -> dict[str, Any]:
    """Run one plan headless (`opencode run --format json`) under deadlines.

    The startup deadline is met by the first JSON event *or* by `contacted()`
    returning true — the node confirming the harness reached it (a /v1 or MCP
    call). opencode emits nothing until a model turn completes, and a first
    turn on a cold model can take minutes; what #38723 looks like is a harness
    that never reaches the node at all."""
    cfg_path, models_path = write(cfg, admission.get("model") or "generalist")
    spec = admission.get("spec_class", "draft")
    # --dir, not only cwd: opencode otherwise resolves files against the
    # enclosing git repository's root, so a workspace inside a repository
    # would read the wrong files.
    argv = [binary(), "run", "--format", "json", "--pure",
            "--dir", str(Path(workdir).resolve()),
            "-m", cfg["model"], "--agent", f"clawcal-{spec}",
            "--title", f"clawcal {admission['task_id']}", prompt]
    trust.log("harness_start", {"task_id": admission["task_id"],
                                "placement": admission["placement"],
                                "model": admission.get("model"),
                                "agent": argv[argv.index("--agent") + 1]})
    proc = subprocess.Popen(argv, cwd=str(workdir),
                            env=environment(cfg_path, models_path),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True)
    sel = selectors.DefaultSelector()
    sel.register(proc.stdout, selectors.EVENT_READ)          # type: ignore[arg-type]
    t0, first, buf = time.time(), None, b""
    reached: float | None = None
    last_poll = 0.0
    events: list[dict[str, Any]] = []
    text_parts: list[str] = []
    tools_used: list[str] = []
    reason = ""
    while True:
        now = time.time()
        if first is None and reached is None and contacted and now - last_poll > 3:
            last_poll = now
            try:
                if contacted():
                    reached = now
            except Exception:                                  # noqa: BLE001
                pass
        if first is None and reached is None and now - t0 > startup_deadline_s:
            reason = (f"no event within {startup_deadline_s:.0f} s of launch: the "
                      f"harness is waiting on something (opencode #38723 fails this "
                      f"way). Killed; a wait was turned into an error")
            break
        if now - t0 > total_deadline_s:
            reason = f"exceeded the {total_deadline_s:.0f} s wall-clock limit"
            break
        if not sel.select(timeout=1.0):
            if proc.poll() is not None:
                break
            continue
        chunk = os.read(proc.stdout.fileno(), 65536)          # type: ignore[union-attr]
        if not chunk:
            if proc.poll() is not None:
                break
            continue
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if not line.strip():
                continue
            first = first or time.time()
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            events.append(ev)
            _account(ev, text_parts, tools_used)
            if on_event:
                on_event(ev)
    if proc.poll() is None:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
    stderr = (proc.stderr.read() or b"").decode("utf-8", "replace")[-4000:] \
        if proc.stderr else ""
    ok = not reason and proc.returncode == 0
    result = {"ok": ok, "exit_code": proc.returncode, "reason": reason,
              "startup_s": round((reached or first) - t0, 2)
              if (reached or first) else None,
              "wall_s": round(time.time() - t0, 1), "text": "".join(text_parts),
              "tools_used": tools_used, "events": len(events),
              "stderr": stderr if not ok else ""}
    trust.log("harness_end", {k: result[k] for k in ("ok", "exit_code", "reason",
                                                     "wall_s", "tools_used")})
    return result


def _account(ev: dict[str, Any], text: list[str], tools: list[str]) -> None:
    """Fold one opencode JSON event into the result and the chained log."""
    kind = str(ev.get("type", ""))
    part = ev.get("part") or {}
    if kind == "text" and part.get("text"):
        text.append(part["text"])
    elif kind in ("tool_use", "tool") or part.get("type") == "tool":
        name = part.get("tool") or ev.get("tool") or "?"
        state = part.get("state") or {}
        tools.append(name)
        trust.log("tool_call", {"tool": name, "status": state.get("status"),
                                "input": json.dumps(state.get("input"))[:400]
                                if state.get("input") is not None else None,
                                "output": str(state.get("output"))[:400]
                                if state.get("output") is not None else None})
    elif kind in ("step_finish", "step-finish"):
        tokens = (part.get("tokens") or {})
        trust.log("model_call", {"tokens": tokens, "cost": part.get("cost")})
    elif kind == "error":
        trust.log("harness_error", {"error": json.dumps(ev)[:400]})
