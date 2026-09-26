#!/usr/bin/env python3
"""The offline check (sovereign-workbench-v2.md §6.2, RQ9).

Runs a command and samples the sockets of its WHOLE process tree every
100 ms — the runtime child (`bun`, `node`, `python`) included, not just the
process with the product's name; v1's lesson, and the easy one to get wrong.
Fails on any connection whose remote end is not loopback and not an allowed
address (the node).

    offline_check.py --allow 10.0.0.5:8443 --runs 3 -- opencode run "hello"

Linux only: it reads /proc/<pid>/fd and /proc/net/{tcp,tcp6,udp,udp6} of the
network namespace it runs in. Exit 0 = clean on every run.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import socket
import struct
import subprocess
import sys
import time


def _tree(root: int) -> set[int]:
    kids: dict[int, list[int]] = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/stat") as fh:
                ppid = int(fh.read().rsplit(")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            continue
        kids.setdefault(ppid, []).append(int(d))
    out, stack = set(), [root]
    while stack:
        p = stack.pop()
        if p in out:
            continue
        out.add(p)
        stack.extend(kids.get(p, []))
    return out


def _inodes(pids: set[int]) -> dict[int, int]:
    found = {}
    for pid in pids:
        try:
            for fd in os.listdir(f"/proc/{pid}/fd"):
                try:
                    link = os.readlink(f"/proc/{pid}/fd/{fd}")
                except OSError:
                    continue
                if link.startswith("socket:["):
                    found[int(link[8:-1])] = pid
        except OSError:
            continue
    return found


def _addr(hexaddr: str, v6: bool) -> tuple[str, int]:
    ip_hex, port_hex = hexaddr.split(":")
    port = int(port_hex, 16)
    raw = bytes.fromhex(ip_hex)
    if v6:
        raw = b"".join(struct.pack("<I", struct.unpack(">I", raw[i:i + 4])[0])
                       for i in range(0, 16, 4))
        return socket.inet_ntop(socket.AF_INET6, raw), port
    return socket.inet_ntop(socket.AF_INET, raw[::-1]), port


def _sockets() -> dict[int, tuple[str, str, int]]:
    out = {}
    for proto, v6 in (("tcp", False), ("tcp6", True), ("udp", False), ("udp6", True)):
        try:
            with open(f"/proc/net/{proto}") as fh:
                next(fh)
                for line in fh:
                    f = line.split()
                    host, port = _addr(f[2], v6)
                    out[int(f[9])] = (proto, host, port)
        except OSError:
            continue
    return out


def _allowed(host: str, port: int, allow: list[tuple[str, int | None]]) -> bool:
    if port == 0:
        return True                                # unconnected / listening
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    if ip.is_loopback or ip.is_unspecified or (
            ip.version == 6 and ip.ipv4_mapped and ip.ipv4_mapped.is_loopback):
        return True
    return any(host == h and (p is None or p == port) for h, p in allow)


def run_once(cmd: list[str], allow: list[tuple[str, int | None]],
             timeout: float) -> dict:
    # The command's own output passes through; only its sockets are watched.
    proc = subprocess.Popen(cmd)
    seen: dict[str, dict] = {}
    procs: set[str] = set()
    t0 = time.time()
    while proc.poll() is None and time.time() - t0 < timeout:
        pids = _tree(proc.pid)
        for pid in pids:
            try:
                with open(f"/proc/{pid}/comm") as fh:
                    procs.add(fh.read().strip())
            except OSError:
                pass
        socks = _sockets()
        for inode, pid in _inodes(pids).items():
            if inode in socks:
                proto, host, port = socks[inode]
                key = f"{proto}:{host}:{port}"
                if key not in seen:
                    seen[key] = {"proto": proto, "host": host, "port": port,
                                 "pid": pid, "allowed": _allowed(host, port, allow),
                                 "t": round(time.time() - t0, 2)}
        time.sleep(0.1)
    timed_out = proc.poll() is None
    if timed_out:
        proc.kill()
    proc.wait()
    bad = [s for s in seen.values() if not s["allowed"]]
    return {"ok": not bad and not timed_out, "exit_code": proc.returncode,
            "timed_out": timed_out, "wall_s": round(time.time() - t0, 1),
            "processes": sorted(procs), "connections": list(seen.values()),
            "violations": bad}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--allow", action="append", default=[],
                    help="host[:port] the command may reach (the node)")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    cmd = a.cmd[1:] if a.cmd[:1] == ["--"] else a.cmd
    if not cmd:
        ap.error("give a command after --")
    allow = []
    for e in a.allow:
        h, _, p = e.rpartition(":") if e.count(":") == 1 else (e, "", "")
        allow.append((h or e, int(p) if p else None))
    results = [run_once(cmd, allow, a.timeout) for _ in range(a.runs)]
    if a.json:
        print(json.dumps(results, indent=1))
    else:
        for i, r in enumerate(results, 1):
            print(f"run {i}: {'CLEAN' if r['ok'] else 'FAIL'}  exit {r['exit_code']}  "
                  f"{r['wall_s']} s  processes {', '.join(r['processes'])}")
            for c in r["connections"]:
                print(f"   {'ok ' if c['allowed'] else 'BAD'} {c['proto']:<4} "
                      f"{c['host']}:{c['port']}  (pid {c['pid']}, +{c['t']} s)")
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
