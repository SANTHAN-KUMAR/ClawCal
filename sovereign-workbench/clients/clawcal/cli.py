"""clawcal — the terminal client (ARCHITECTURE-v2 §5.4).

A thin client over the same REST + SSE surface the web workbench uses. It holds
no state of its own: every line it prints about a task is rendered from the
server's transcript, so the same task looks the same here and in the browser.

    clawcal                                   interactive session
    clawcal "summarise the attached IR" --attach IR-0731.pdf --mode review
    clawcal resume <session>                  pick up a session
    clawcal approve <approval-id> [--deny]    decide a pending approval
    clawcal status                            queue, residency, egress, audit
    clawcal selftest [--report]               run the sovereignty self-test
    clawcal audit export [file]               signed audit export
    clawcal verify <file>                     verify a report or export offline

Trust-domain member device (see `clawcal device help`):
    clawcal device enrol|status|profile|egress|renew|manifest|update|weights|
                   verify|sync|log
    clawcal plan "<task>"                     B1: where would this run, and why
    clawcal attach "<task>" [--workdir DIR]   run the task with the harness,
                                              attached to the node
    clawcal engine start|stop|status          the local engine (detached mode)
    clawcal exec-local "<command>" [--workdir DIR]

Inside a session:  /attach <file>  /evidence  /approve [id]  /deny [id]
                   /mode <review|trusted|locked>  /artefacts  /download <id>
                   /trace  /status  /new  /help  /quit

Standard library only: an air-gapped server often has neither a browser nor a
package index, and the client must run on whatever Python the box ships.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Iterator

DEFAULT_URL = os.environ.get("CLAWCAL_URL", "http://127.0.0.1:8794")
TERMINAL = {"COMPLETED", "FAILED", "TERMINATED", "REJECTED"}
CONFIG = Path(os.environ.get("CLAWCAL_CONFIG",
                             Path.home() / ".config" / "clawcal" / "config.json"))

_COLOUR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
_C = {"ESTABLISHED": "32", "INTERPRETED": "33", "CANNOT DETERMINE": "31",
      "DEGRADED": "35", "dim": "2", "bold": "1", "err": "31", "ok": "32"}


def c(text: str, key: str) -> str:
    return f"\033[{_C[key]}m{text}\033[0m" if _COLOUR and key in _C else text


def badge_colour(line: str) -> str:
    for label in ("CANNOT DETERMINE", "ESTABLISHED", "INTERPRETED", "DEGRADED"):
        tag = f"[{label}]"
        if tag in line:
            line = line.replace(tag, c(tag, label))
    if line.lstrip().startswith("DEGRADED:"):
        line = c(line, "DEGRADED")
    return line


class ApiError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


# ------------------------------------------------------------------- http

class Client:
    def __init__(self, url: str, token: str | None) -> None:
        self.url = url.rstrip("/")
        self.token = token
        # Device and lease headers, when this client speaks as an enrolled
        # member device of the trust domain (see domain.py).
        self.headers: dict[str, str] = {}

    def _req(self, method: str, path: str, body: Any = None, *,
             raw: bytes | None = None, ctype: str | None = None,
             timeout: float = 60.0, binary: bool = False) -> Any:
        headers = {"Accept": "application/json", **self.headers}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        elif ctype:
            headers["Content-Type"] = ctype
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = r.read()
                ct = r.headers.get("Content-Type", "")
                if binary:
                    return payload          # a download is saved byte for byte
                if "json" in ct:
                    return json.loads(payload or b"null")
                return payload
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read())
                detail = body.get("detail") or (
                    f"{body.get('kind')}: {body.get('reason')}" if body.get("reason")
                    else body)
            except Exception:
                detail = e.reason
            raise ApiError(e.code, str(detail)) from None
        except urllib.error.URLError as e:
            raise ApiError(0, f"cannot reach {self.url}: {e.reason}. Is the "
                              f"workbench running? (./run.sh)") from None

    def get(self, path: str, **kw: Any) -> Any:
        return self._req("GET", path, **kw)

    def post(self, path: str, body: Any = None, **kw: Any) -> Any:
        return self._req("POST", path, body if body is not None else {}, **kw)

    def upload(self, path: Path, session_id: str | None) -> dict[str, Any]:
        boundary = uuid.uuid4().hex
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                f"filename=\"{path.name}\"\r\nContent-Type: {mime}\r\n\r\n").encode()
        body = head + path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
        q = f"?session_id={urllib.parse.quote(session_id)}" if session_id else ""
        return self._req("POST", f"/api/upload{q}", raw=body,
                         ctype=f"multipart/form-data; boundary={boundary}",
                         timeout=1800)

    def stream(self, stop: threading.Event) -> Iterator[dict[str, Any]]:
        """Server-sent events, until `stop` is set or the connection drops."""
        q = f"?token={urllib.parse.quote(self.token)}" if self.token else ""
        req = urllib.request.Request(self.url + "/api/stream" + q,
                                     headers={"Accept": "text/event-stream"})
        with urllib.request.urlopen(req, timeout=30) as r:
            for raw in r:
                if stop.is_set():
                    return
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("data:"):
                    try:
                        yield json.loads(line[5:].strip())
                    except ValueError:
                        continue


def load_token(explicit: str | None) -> str | None:
    if explicit:
        return explicit
    if os.environ.get("CLAWCAL_TOKEN"):
        return os.environ["CLAWCAL_TOKEN"]
    try:
        return json.loads(CONFIG.read_text()).get("token")
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------------ session

class Session:
    """One interactive session. Everything it shows comes from the server."""

    def __init__(self, api: Client, session_id: str | None, mode: str | None,
                 verbose: bool = False) -> None:
        self.api = api
        self.id = session_id
        self.mode = mode
        self.verbose = verbose
        self.pending_docs: list[str] = []
        self.printed = 0

    # -- server state --------------------------------------------------
    def ensure(self) -> str:
        if not self.id:
            s = self.api.post("/api/sessions", {"mode": self.mode} if self.mode else {})
            self.id = s["id"]
            self.mode = s["permission_mode"]
            print(c(f"session {self.id} · mode {self.mode}", "dim"))
        return self.id

    def transcript_lines(self) -> list[str]:
        text = self.api.get(f"/api/sessions/{self.id}/transcript?format=text"
                            f"&verbose={'true' if self.verbose else 'false'}")
        return (text.decode() if isinstance(text, bytes) else str(text)).splitlines()

    def print_new(self) -> None:
        lines = self.transcript_lines()
        for line in lines[self.printed:]:
            print(badge_colour(line))
        self.printed = len(lines)

    # -- actions -------------------------------------------------------
    def attach(self, path: str) -> None:
        p = Path(path).expanduser()
        if not p.is_file():
            print(c(f"no such file: {p}", "err"))
            return
        sid = self.ensure()
        print(c(f"indexing {p.name} …", "dim"))
        r = self.api.upload(p, sid)
        failed = f", pages {r['failed_pages']} unreadable" if r.get("failed_pages") else ""
        print(f"  attached {r.get('title')} — {r.get('pages')} page(s), "
              f"{r.get('chunks', 0)} chunks, class {r.get('doc_class')}{failed}"
              + (" (already indexed)" if r.get("reused") else ""))

    def ask(self, prompt: str, *, workflow: str = "general",
            priority: str | None = None) -> str:
        sid = self.ensure()
        body: dict[str, Any] = {"prompt": prompt, "session_id": sid,
                                "workflow": workflow}
        if priority:
            body["priority"] = priority
        r = self.api.post("/api/tasks", body)
        if r.get("injection_scan", {}).get("detected"):
            print(c("  note: the prompt contains instruction-like text; recorded", "dim"))
        self.follow(r["task_id"])
        return r["task_id"]

    def follow(self, task_id: str) -> None:
        """Print the transcript as it grows, and stop to ask on approvals."""
        stop = threading.Event()
        wake = threading.Event()

        def listen() -> None:
            try:
                for ev in self.api.stream(stop):
                    if ev.get("task_id") == task_id or ev.get("type") == "approval":
                        wake.set()
            except Exception:
                pass

        threading.Thread(target=listen, daemon=True).start()
        asked: set[str] = set()
        try:
            while True:
                wake.wait(2.0)
                wake.clear()
                self.print_new()
                t = self.api.get(f"/api/tasks/{task_id}")["task"]
                for a in self.api.get("/api/approvals")["pending"]:
                    if a.get("task_id") == task_id and a["id"] not in asked:
                        asked.add(a["id"])
                        self.prompt_approval(a)
                if t["state"] in TERMINAL:
                    self.print_new()
                    return
                if t["state"] in ("QUEUED", "PAUSED") and t.get("wait"):
                    msg = t["wait"].get("text", "")
                    print(c(f"  … {t['state'].lower()}: {msg}", "dim"), end="\r")
        except KeyboardInterrupt:
            print(c("\n  interrupted: the task keeps running on the server. "
                    f"`clawcal resume {self.id}` to follow it again, or /cancel.",
                    "dim"))
        finally:
            stop.set()

    def prompt_approval(self, a: dict[str, Any]) -> None:
        print(c(f"\n  APPROVAL NEEDED ({a['id']}): {a['tool']}", "bold"))
        print("  " + (a.get("summary") or "").replace("\n", "\n  "))
        if not sys.stdin.isatty():
            print(c(f"  (non-interactive: decide with `clawcal approve {a['id']}`)",
                    "dim"))
            return
        try:
            ans = input("  approve? [y/N] ").strip().lower()
        except EOFError:
            return
        try:
            self.api.post(f"/api/approvals/{a['id']}", {"approve": ans in ("y", "yes")})
            print(c("  approved" if ans in ("y", "yes") else "  denied", "dim"))
        except ApiError as e:
            print(c(f"  {e.detail}", "err"))

    # -- slash commands ------------------------------------------------
    def slash(self, line: str) -> bool:
        """Handle a slash command. Returns False to quit."""
        cmd, _, arg = line[1:].partition(" ")
        arg = arg.strip()
        try:
            if cmd in ("quit", "exit", "q"):
                return False
            if cmd == "help":
                print(__doc__.split("Inside a session:")[1].split("Standard")[0])
            elif cmd == "attach":
                self.attach(arg) if arg else print("usage: /attach <file>")
            elif cmd == "mode":
                if not arg:
                    print(f"mode: {self.mode or '(server default)'}")
                else:
                    s = self.api.post(f"/api/sessions/{self.ensure()}/mode",
                                      {"mode": arg})
                    self.mode = s["permission_mode"]
                    print(f"mode is now {self.mode}")
            elif cmd in ("approve", "deny"):
                pending = self.api.get("/api/approvals")["pending"]
                target = arg or (pending[0]["id"] if pending else "")
                if not target:
                    print("nothing is awaiting approval")
                else:
                    self.api.post(f"/api/approvals/{target}",
                                  {"approve": cmd == "approve"})
                    print(f"{cmd}d {target}")
            elif cmd == "evidence":
                self.show_evidence()
            elif cmd in ("artefacts", "artifacts"):
                ws = self.api.get(f"/api/sessions/{self.ensure()}")["working_set"]
                for d in ws["documents"]:
                    print(f"  doc  {d['doc_id']}  {d['title']}  ({d['kind']})")
                for a in ws["artefacts"]:
                    print(f"  file {a['id']}  {a['name']}  {a['bytes']} B  "
                          f"sha256 {a['sha256'][:12]}")
            elif cmd == "download":
                download(self.api, arg)
            elif cmd == "trace":
                self.verbose = not self.verbose
                print(f"verbose trace {'on' if self.verbose else 'off'}")
                self.printed = 0
                self.print_new()
            elif cmd == "status":
                status(self.api)
            elif cmd == "new":
                self.id, self.printed = None, 0
                print("new session on next message")
            elif cmd == "cancel":
                for t in self.api.get(f"/api/sessions/{self.ensure()}")["tasks"]:
                    if t["state"] not in TERMINAL:
                        self.api.post(f"/api/tasks/{t['id']}/cancel")
                        print(f"cancelled {t['id']}")
            else:
                print(f"unknown command /{cmd}; /help lists them")
        except ApiError as e:
            print(c(str(e.detail), "err"))
        return True

    def show_evidence(self) -> None:
        tasks = self.api.get(f"/api/sessions/{self.ensure()}")["tasks"]
        if not tasks:
            print("no tasks yet")
            return
        d = self.api.get(f"/api/tasks/{tasks[-1]['id']}")
        names = {"A": "ESTABLISHED", "B": "ESTABLISHED", "C": "INTERPRETED",
                 "D": "CANNOT DETERMINE"}
        for cl in d["claims"]:
            label = f"[{names.get(cl['ev_class'], '?')}]"
            print(badge_colour(f"  {cl['ev_class']} {label} {cl['value']:>10}  "
                               f"{(cl['rationale'] or '')[:90]}"))
        for calc in d["calculations"]:
            print(f"  calc {calc['id']}: {calc['expression']} = {calc['result']} "
                  f"{calc.get('unit') or ''}")
        if not d["claims"] and not d["calculations"]:
            print("  no numeric claims recorded for the last task")


# ------------------------------------------------------------------ commands

def status(api: Client) -> None:
    rt = api.get("/api/runtime")
    sv = api.get("/api/sovereignty")
    who = api.get("/api/whoami")["principal"]
    state = {"green": c("GREEN", "ok"), "amber": c("AMBER", "INTERPRETED"),
             "red": c("RED", "err")}.get(sv["state"], sv["state"])
    print(f"signed in as {who['name']} ({who['role']})")
    print(f"sovereignty {state}: app guard {'on' if sv['app_guard'] else 'OFF'}, "
          f"host firewall {sv['host_policy']}, {sv['denials']} denials recorded, "
          f"audit {'verified' if sv['audit']['ok'] else 'BROKEN'} "
          f"({sv['audit']['entries']} entries, head {sv['audit']['head'][:12]})")
    res = rt.get("residency", {})
    resident = ", ".join(f"{r['model']} ({r['vram_mb']:.0f} MB)"
                         for r in res.get("resident", [])) or "none"
    print(f"resident models: {resident}; free VRAM {res.get('free_vram_mb', 0):.0f} MB")
    print(f"running {len(rt['running'])}, queued {len(rt['queued'])} "
          f"(limit {rt['limits']['max_concurrent_agents']}: "
          f"{rt['limits']['concurrency_reason']})")
    for q in rt["queued"][:10]:
        print(f"  {q['id']}  {q['priority']:<8} {q['title'][:50]:<50} "
              f"{(q.get('wait') or {}).get('text', '')}")
    pend = api.get("/api/approvals")["pending"]
    if pend:
        print(f"{len(pend)} approval(s) pending:")
        for a in pend:
            print(f"  {a['id']}  {a['tool']}: {(a.get('summary') or '')[:70]}")


def download(api: Client, artefact_id: str, dest: str | None = None) -> None:
    if not artefact_id:
        print("usage: /download <artefact-id>")
        return
    meta = next((a for a in api.get("/api/artifacts")["artifacts"]
                 if a["id"] == artefact_id), None)
    data = api.get(f"/api/artifacts/{artefact_id}/download", timeout=300,
                   binary=True)
    name = dest or (meta["name"] if meta else artefact_id)
    Path(name).write_bytes(data)
    print(f"saved {name} ({len(data)} bytes)")


def selftest(api: Client, report: bool) -> int:
    r = api.post("/api/sovereignty/selftest")
    s = Session(api, r["session_id"], None)
    s.follow(r["task_id"])
    t = api.get(f"/api/tasks/{r['task_id']}")["task"]
    ok = bool((t.get("result") or {}).get("all_blocked"))
    if report and t["state"] == "COMPLETED":
        rep = api.post(f"/api/sovereignty/report/{r['task_id']}")
        download(api, rep["artifact_id"])
        download(api, rep["signature_artifact_id"])
        print(f"signed with appliance key {rep['fingerprint']}")
    return 0 if ok else 1


def verify(path: str) -> int:
    """Offline: needs only the file (and the repository's verifier code)."""
    here = Path(__file__).resolve()
    sys.path.insert(0, str(here.parents[2] / "backend"))
    os.environ.setdefault("SOVEREIGN_DATA_DIR",
                          str(Path(os.environ.get("TMPDIR", "/tmp")) / "clawcal-verify"))
    from sovereign.control import report          # noqa: E402  (verifier only)
    p = Path(path)
    res = (report.verify_audit_export(p) if p.suffix == ".jsonl"
           else report.verify_report(p))
    print(json.dumps(res, indent=1, default=str))
    return 0 if res.get("ok") else 1


def interactive(api: Client, session_id: str | None, mode: str | None,
                verbose: bool) -> int:
    s = Session(api, session_id, mode, verbose)
    who = api.get("/api/whoami")["principal"]
    print(c(f"ClawCal · {api.url} · {who['name']} ({who['role']}) · /help", "dim"))
    if session_id:
        s.print_new()
    while True:
        try:
            line = input(c("clawcal› ", "bold")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line.startswith("/"):
            if not s.slash(line):
                return 0
            continue
        try:
            s.ask(line)
        except ApiError as e:
            print(c(e.detail, "err"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="clawcal", description=__doc__.split("\n")[0])
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--token", default=None, help="bearer token (or CLAWCAL_TOKEN)")
    ap.add_argument("--mode", choices=["review", "trusted", "locked"])
    ap.add_argument("--attach", action="append", default=[])
    ap.add_argument("--workflow", default="general")
    ap.add_argument("--priority")
    ap.add_argument("--session")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--deny", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("words", nargs="*")
    args, unknown = ap.parse_known_args(argv)

    words = list(args.words)
    from . import domain
    if words and words[0] in domain.COMMANDS:
        # The sub-command parses its own flags from the raw arguments.
        raw = list(sys.argv[1:] if argv is None else argv)
        api = Client(args.url, load_token(args.token))
        return domain.main(api, raw[raw.index(words[0]):])
    if unknown:
        ap.error(f"unrecognised arguments: {' '.join(unknown)}")
    if words and words[0] == "verify":
        return verify(words[1]) if len(words) > 1 else 2
    api = Client(args.url, load_token(args.token))
    try:
        if words and words[0] == "login":
            CONFIG.parent.mkdir(parents=True, exist_ok=True)
            token = words[1] if len(words) > 1 else input("token: ").strip()
            CONFIG.write_text(json.dumps({"token": token, "url": args.url}))
            os.chmod(CONFIG, 0o600)
            print(f"saved to {CONFIG}")
            return 0
        if words and words[0] == "status":
            status(api)
            return 0
        if words and words[0] == "approve":
            if len(words) < 2:
                print("usage: clawcal approve <approval-id> [--deny]")
                return 2
            r = api.post(f"/api/approvals/{words[1]}", {"approve": not args.deny})
            print(f"{r['state'].lower()} by {r['decided_by']}")
            return 0
        if words and words[0] == "resume":
            sid = words[1] if len(words) > 1 else None
            if not sid:
                for s in api.get("/api/sessions?limit=10")["sessions"]:
                    print(f"  {s['id']}  {s['permission_mode']:<8} {s['title'][:60]}")
                return 0
            sess = Session(api, sid, None, args.verbose)
            sess.print_new()
            live = [t for t in api.get(f"/api/sessions/{sid}")["tasks"]
                    if t["state"] not in TERMINAL]
            if live:
                sess.follow(live[-1]["id"])
            return interactive(api, sid, None, args.verbose) if sys.stdin.isatty() else 0
        if words and words[0] == "selftest":
            return selftest(api, args.report)
        if words[:2] == ["audit", "export"]:
            data = api.get("/api/audit/export", timeout=600, binary=True)
            out = words[2] if len(words) > 2 else f"audit-export-{int(time.time())}.jsonl"
            Path(out).write_bytes(data)
            print(f"saved {out}; verify offline with: clawcal verify {out}")
            return 0
        if words and words[0] == "download":
            download(api, words[1] if len(words) > 1 else "",
                     words[2] if len(words) > 2 else None)
            return 0

        if words:                                   # one-shot
            s = Session(api, args.session, args.mode, args.verbose)
            for f in args.attach:
                s.attach(f)
            s.ask(" ".join(words), workflow=args.workflow, priority=args.priority)
            tasks = api.get(f"/api/sessions/{s.id}")["tasks"]
            print(c(f"\nsession {s.id} — `clawcal resume {s.id}` to continue", "dim"))
            return 0 if tasks and tasks[-1]["state"] == "COMPLETED" else 1

        s_id = args.session
        if args.attach:
            s = Session(api, s_id, args.mode)
            for f in args.attach:
                s.attach(f)
            s_id = s.id
        return interactive(api, s_id, args.mode, args.verbose)
    except ApiError as e:
        print(c(e.detail if e.status else str(e), "err"), file=sys.stderr)
        if e.status == 401:
            print("sign in with: clawcal login <token>", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
