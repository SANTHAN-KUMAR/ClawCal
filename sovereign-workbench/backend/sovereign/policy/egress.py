"""Sovereignty enforcement: egress policy, and the record of its refusals.

Defence in depth, four layers, each producing an auditable event:

    L1 tool policy      agents are given no network tool at all
    L2 application guard  a socket-level hook in this process refuses any
                          connection outside the loopback allowlist
    L3 sandbox netns      code execution runs in an empty network namespace
    L4 host firewall      nftables default-deny with logging (ops/egress-policy.sh)

The demonstrable artefact is not an empty packet capture. It is the non-empty,
hash-chained denial log this module writes.
"""
from __future__ import annotations

import ipaddress
import socket
import threading
from typing import Any

from .. import audit, db
from ..config import settings

_installed = False
_lock = threading.Lock()
_real_create_connection = socket.create_connection
_real_socket_connect = socket.socket.connect
_real_getaddrinfo = socket.getaddrinfo

# Resolution happens before connection, so remembering the most recent hostname
# each thread resolved lets the denial log name "vendor-portal-sync.example.com"
# rather than an opaque IPv6 literal. Attribution is the point of layer 2.
_recent_hosts = threading.local()

# Loopback is permitted because the local inference backends live there. Nothing
# else is, including private LAN ranges: an on-premise deployment that may talk to
# the rest of the corporate network is not the claim being made.
_ALLOWED_NETS = [ipaddress.ip_network("127.0.0.0/8"),
                 ipaddress.ip_network("::1/128")]


def _resolve(host: str) -> str | None:
    try:
        return socket.gethostbyname(host)
    except Exception:
        return None


def is_allowed(host: str, port: int | None = None) -> tuple[bool, str]:
    if not settings.sovereignty.enforce:
        return True, "egress enforcement disabled by configuration"
    h = (host or "").strip("[]")
    if h in settings.sovereignty.allowed_hosts:
        ok_port = port is None or port in settings.sovereignty.allowed_ports
        if ok_port:
            return True, "loopback destination on an allowed local service port"
        return False, (f"host {h} is local but port {port} is not in the allowed "
                       f"local service ports {list(settings.sovereignty.allowed_ports)}")
    ip_s = h if _looks_like_ip(h) else _resolve(h)
    if ip_s:
        try:
            ip = ipaddress.ip_address(ip_s)
            if any(ip in net for net in _ALLOWED_NETS):
                if port is None or port in settings.sovereignty.allowed_ports:
                    return True, "loopback address on an allowed local service port"
                return False, (f"loopback address {ip_s} but port {port} is not an "
                               f"allowed local service port")
        except ValueError:
            pass
    return False, (f"destination {h}"
                   f"{':' + str(port) if port else ''} is outside the sovereign "
                   f"boundary; policy is DEFAULT_DENY")


def _looks_like_ip(h: str) -> bool:
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return False


class EgressDenied(OSError):
    """Raised in-process when code attempts to leave the sovereign boundary."""


def record_event(*, destination: str, port: int | None, layer: str, result: str,
                 process: str = "workbench", pid: int | None = None,
                 task_id: str | None = None, detail: str = "") -> str:
    import os
    eid = db.new_id("net")
    db.insert("network_events", {
        "id": eid, "task_id": task_id, "process": process,
        "pid": pid if pid is not None else os.getpid(),
        "destination": destination, "port": port, "layer": layer,
        "policy": "DEFAULT_DENY", "result": result, "detail": detail[:600],
        "ts": db.now()})
    audit.record("sovereignty", f"egress_{result.lower()}",
                 outcome=result, task_id=task_id,
                 detail={"destination": destination, "port": port, "layer": layer,
                         "process": process, "detail": detail[:300]})
    audit.bus.publish({"type": "network_event", "id": eid, "destination": destination,
                       "port": port, "layer": layer, "result": result,
                       "task_id": task_id, "process": process})
    return eid


_current_task = threading.local()


def bind_task(task_id: str | None) -> None:
    """Attribute in-process egress attempts on this thread to a task."""
    _current_task.task_id = task_id


def _task() -> str | None:
    return getattr(_current_task, "task_id", None)


def _remember_host(ip: str, host: str) -> None:
    table = getattr(_recent_hosts, "table", None)
    if table is None:
        table = _recent_hosts.table = {}
    table[ip] = host
    if len(table) > 64:
        table.clear()
        table[ip] = host


def _hostname_for(ip: str) -> str | None:
    return (getattr(_recent_hosts, "table", None) or {}).get(ip)


def _describe(host: str) -> str:
    """Prefer the name the caller asked for over the address it resolved to."""
    name = _hostname_for(host)
    return f"{name} ({host})" if name and name != host else host


def install_guard() -> bool:
    """Patch the socket layer so any outbound attempt is refused and recorded.

    This is layer 2. It is not the strongest layer -- a determined native
    extension could bypass it -- which is exactly why layers 3 and 4 exist. Its
    value is attribution: it knows which task tried.
    """
    global _installed
    with _lock:
        if _installed:
            return False

        def guarded_getaddrinfo(host, port, *args, **kwargs):
            # Enforce at resolution, not only at connection. Otherwise an
            # unresolvable external hostname fails as a DNS error and never
            # appears in the denial log -- which is precisely the record the
            # sovereignty claim depends on.
            if isinstance(host, str) and host:
                p = port if isinstance(port, int) else None
                ok, reason = is_allowed(host, p)
                if not ok:
                    record_event(destination=host, port=p, layer="app-guard",
                                 result="DENIED", task_id=_task(),
                                 detail=f"name resolution refused: {reason}")
                    raise EgressDenied(f"EGRESS_DENIED {host} - {reason}")
            res = _real_getaddrinfo(host, port, *args, **kwargs)
            if isinstance(host, str):
                for entry in res:
                    try:
                        _remember_host(str(entry[4][0]), host)
                    except (IndexError, TypeError):
                        pass
            return res

        def guarded_create_connection(address, *args, **kwargs):
            host, port = address[0], address[1]
            ok, reason = is_allowed(str(host), int(port))
            if not ok:
                record_event(destination=_describe(str(host)), port=int(port),
                             layer="app-guard", result="DENIED",
                             task_id=_task(), detail=reason)
                raise EgressDenied(f"EGRESS_DENIED {host}:{port} - {reason}")
            return _real_create_connection(address, *args, **kwargs)

        def guarded_connect(self, address):
            try:
                if self.family in (socket.AF_INET, socket.AF_INET6) and \
                        isinstance(address, tuple) and len(address) >= 2:
                    host, port = str(address[0]), int(address[1])
                    ok, reason = is_allowed(host, port)
                    if not ok:
                        record_event(destination=_describe(host), port=port,
                                     layer="app-guard", result="DENIED",
                                     task_id=_task(), detail=reason)
                        raise EgressDenied(
                            f"EGRESS_DENIED {host}:{port} - {reason}")
            except EgressDenied:
                raise
            except Exception:
                pass
            return _real_socket_connect(self, address)

        socket.getaddrinfo = guarded_getaddrinfo                  # type: ignore[assignment]
        socket.create_connection = guarded_create_connection      # type: ignore[assignment]
        socket.socket.connect = guarded_connect                   # type: ignore[method-assign]
        _installed = True
        audit.record("sovereignty", "egress_guard_installed", detail={
            "allowed_hosts": list(settings.sovereignty.allowed_hosts),
            "allowed_ports": list(settings.sovereignty.allowed_ports)})
        return True


def remove_guard() -> None:
    global _installed
    with _lock:
        socket.getaddrinfo = _real_getaddrinfo                    # type: ignore[assignment]
        socket.create_connection = _real_create_connection        # type: ignore[assignment]
        socket.socket.connect = _real_socket_connect              # type: ignore[method-assign]
        _installed = False


def guard_active() -> bool:
    return _installed


def status() -> dict[str, Any]:
    counts = db.query(
        "SELECT layer, result, COUNT(*) AS n FROM network_events GROUP BY layer, result")
    recent = db.rows_to_dicts(db.query(
        "SELECT * FROM network_events ORDER BY ts DESC LIMIT 40"))
    return {
        "enforce": settings.sovereignty.enforce,
        "app_guard_installed": _installed,
        "allowed_hosts": list(settings.sovereignty.allowed_hosts),
        "allowed_ports": list(settings.sovereignty.allowed_ports),
        "counts": [{"layer": r["layer"], "result": r["result"], "n": r["n"]}
                   for r in counts],
        "recent": recent,
        "nftables": nftables_status(),
    }


def nftables_status() -> dict[str, Any]:
    """Report whether the host-level default-deny table is loaded (layer 4)."""
    import shutil
    import subprocess
    if not shutil.which("nft"):
        return {"available": False, "detail": "nft binary not present"}
    try:
        r = subprocess.run(["nft", "list", "table", "inet",
                            settings.sovereignty.nft_table],
                           capture_output=True, text=True, timeout=6)
    except Exception as exc:
        return {"available": True, "loaded": False, "detail": str(exc)[:200]}
    if r.returncode != 0:
        return {"available": True, "loaded": False,
                "detail": "table not loaded (run ops/egress-policy.sh as root)"}
    return {"available": True, "loaded": True,
            "rules": r.stdout.strip().splitlines()[:40]}
