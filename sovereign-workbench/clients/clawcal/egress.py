"""The client's egress policy and its self-check (sovereign-workbench-v2.md §11).

Allowlist: the node, loopback, and — only in detached mode, only while a
download runs — the hosts pinned in the signed manifest.

The client policy **rejects**; it never silently drops (§3.4 item 1). The node's
`drop` is right for a node. On a client a dropped packet makes a stray fetch
wait forever — opencode's startup hang (#38723) is exactly that shape — where
a reject turns it into an error in milliseconds.

Per platform:

    linux    nftables table `inet clawcal_client`, output hook, loopback and the
             allowlist accepted, everything else `reject` (TCP reset / ICMP
             admin-prohibited), counted and logged with a prefix
    windows  Windows Filtering Platform rules through Windows Firewall: default
             outbound block, allow rules per destination (a service installs
             them; generated here as PowerShell)
    macos    a pf anchor with `block return` (the pf spelling of reject); per-
             process attribution needs the Network Extension build (RQ12)

The self-check reports what it could verify and how. It is self-reported
evidence — the node treats it as such (grade C at most) — but it is honest:
"not active" and "could not verify" are different answers.
"""
from __future__ import annotations

import errno
import ipaddress
import shutil
import socket
import subprocess
import time
from typing import Any
from urllib.parse import urlsplit

TABLE = "clawcal_client"
LOG_PREFIX = "CLAWCAL-EGRESS-REJECT "
# Destinations the self-check tries and expects to be refused. Public anycast
# resolvers: reachable from almost anywhere, so a connection means egress is open.
PROBES = [("1.1.1.1", 443), ("8.8.8.8", 53), ("9.9.9.9", 443)]


def _hostport(entry: str) -> tuple[str, int | None]:
    e = entry.strip()
    if "://" in e:
        u = urlsplit(e)
        return u.hostname or "", u.port or (443 if u.scheme == "https" else 80)
    if e.startswith("[") and "]" in e:
        host, _, port = e[1:].partition("]")
        return host, int(port.lstrip(":")) if port.strip(":") else None
    if e.count(":") == 1:
        host, port = e.split(":")
        return host, int(port) if port else None
    return e, None


def _resolve(host: str) -> list[str]:
    try:
        ipaddress.ip_address(host)
        return [host]
    except ValueError:
        pass
    try:
        return sorted({ai[4][0] for ai in socket.getaddrinfo(host, None)})
    except OSError:
        return []


def allowlist(node_url: str, extra: list[str] | None = None) -> list[dict[str, Any]]:
    """[{host, addrs, port, why}] for the node and each extra entry."""
    out = []
    for entry, why in [(node_url, "the trust-domain node")] + [
            (e, "pinned in the signed manifest") for e in (extra or [])]:
        host, port = _hostport(entry)
        if not host or host in ("127.0.0.1", "::1", "localhost"):
            continue
        out.append({"host": host, "addrs": _resolve(host), "port": port, "why": why})
    return out


def nftables_ruleset(node_url: str, extra: list[str] | None = None) -> str:
    lines = [f"table inet {TABLE} {{",
             "  chain output {",
             "    type filter hook output priority 0; policy accept;",
             "    oif lo accept",
             "    ct state established,related accept"]
    for a in allowlist(node_url, extra):
        for addr in a["addrs"]:
            fam = "ip6" if ":" in addr else "ip"
            port = f" tcp dport {a['port']}" if a["port"] else ""
            lines.append(f"    {fam} daddr {addr}{port} accept  # {a['host']}: {a['why']}")
    lines += [f'    meta l4proto tcp log prefix "{LOG_PREFIX}" counter reject with tcp reset',
              f'    log prefix "{LOG_PREFIX}" counter reject with icmpx type admin-prohibited',
              "  }", "}"]
    return "\n".join(lines) + "\n"


def windows_policy(node_url: str, extra: list[str] | None = None) -> str:
    """PowerShell for the Windows Filtering Platform via Windows Firewall."""
    rules = ["# ClawCal client egress policy (run elevated; the installer's service",
             "# applies it). Outbound is blocked by default; the node is allowed.",
             "Set-NetFirewallProfile -Profile Domain,Private,Public "
             "-DefaultOutboundAction Block -LogBlocked True",
             "Get-NetFirewallRule -Group 'ClawCal' -ErrorAction SilentlyContinue | "
             "Remove-NetFirewallRule"]
    for a in allowlist(node_url, extra):
        for addr in a["addrs"]:
            port = f" -Protocol TCP -RemotePort {a['port']}" if a["port"] else ""
            rules.append(f"New-NetFirewallRule -Group 'ClawCal' -DisplayName "
                         f"'ClawCal allow {a['host']}' -Direction Outbound -Action Allow "
                         f"-RemoteAddress {addr}{port}")
    rules.append("New-NetFirewallRule -Group 'ClawCal' -DisplayName 'ClawCal loopback' "
                 "-Direction Outbound -Action Allow -RemoteAddress 127.0.0.1,::1")
    return "\n".join(rules) + "\n"


# macOS's stock /etc/pf.conf evaluates `anchor "com.apple/*"`, so rules loaded
# into a sub-anchor there take effect without editing the system's pf.conf.
PF_ANCHOR = "com.apple/250.ClawCalClient"


def pf_anchor(node_url: str, extra: list[str] | None = None) -> str:
    rules = [f"# ClawCal client egress — loaded with: pfctl -a {PF_ANCHOR} -f <file>",
             "pass out quick on lo0 all"]
    for a in allowlist(node_url, extra):
        for addr in a["addrs"]:
            port = f" port {a['port']}" if a["port"] else ""
            rules.append(f"pass out quick proto tcp to {addr}{port}  # {a['host']}")
    rules.append("block return out log quick all   # reject, never drop")
    return "\n".join(rules) + "\n"


def apply_linux(node_url: str, extra: list[str] | None = None) -> dict[str, Any]:
    """Load the nftables ruleset. Needs CAP_NET_ADMIN (root, or a network
    namespace the client owns)."""
    if not shutil.which("nft"):
        return {"applied": False, "reason": "nft is not installed"}
    ruleset = nftables_ruleset(node_url, extra)
    subprocess.run(["nft", "delete", "table", "inet", TABLE], capture_output=True)
    r = subprocess.run(["nft", "-f", "-"], input=ruleset, text=True,
                       capture_output=True)
    if r.returncode != 0:
        return {"applied": False, "reason": (r.stderr or "nft failed").strip()[:400]}
    return {"applied": True, "ruleset": ruleset}


def apply(node_url: str, extra: list[str] | None = None) -> dict[str, Any]:
    """Install the allowlist with this platform's mechanism (needs admin)."""
    import platform as _p
    system = _p.system()
    if system == "Linux":
        return apply_linux(node_url, extra)
    if system == "Windows":
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                            windows_policy(node_url, extra)],
                           capture_output=True, text=True)
        return {"applied": r.returncode == 0,
                "reason": (r.stderr or r.stdout).strip()[:400] or "ok"}
    if system == "Darwin":
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as fh:
            fh.write(pf_anchor(node_url, extra))
        r = subprocess.run(["pfctl", "-a", PF_ANCHOR, "-f", fh.name],
                           capture_output=True, text=True)
        subprocess.run(["pfctl", "-E"], capture_output=True)
        return {"applied": r.returncode == 0,
                "reason": (r.stderr or "").strip()[:400] or "ok"}
    return {"applied": False, "reason": f"no egress mechanism for {system}"}


def remove() -> dict[str, Any]:
    import platform as _p
    system = _p.system()
    if system == "Windows":
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                            "Get-NetFirewallRule -Group 'ClawCal' -ErrorAction "
                            "SilentlyContinue | Remove-NetFirewallRule; "
                            "Set-NetFirewallProfile -Profile Domain,Private,Public "
                            "-DefaultOutboundAction Allow"],
                           capture_output=True, text=True)
        return {"removed": r.returncode == 0, "detail": r.stderr.strip()[:200]}
    if system == "Darwin":
        r = subprocess.run(["pfctl", "-a", PF_ANCHOR, "-F", "all"],
                           capture_output=True, text=True)
        return {"removed": r.returncode == 0, "detail": r.stderr.strip()[:200]}
    return remove_linux()


def remove_linux() -> dict[str, Any]:
    r = subprocess.run(["nft", "delete", "table", "inet", TABLE],
                       capture_output=True, text=True)
    return {"removed": r.returncode == 0, "detail": r.stderr.strip()[:200]}


def _mechanism() -> dict[str, Any]:
    import platform as _p
    system = _p.system()
    if system == "Linux" and shutil.which("nft"):
        r = subprocess.run(["nft", "list", "table", "inet", TABLE],
                           capture_output=True, text=True)
        if r.returncode == 0:
            loaded = "reject" in r.stdout
            return {"mechanism": "nftables", "loaded": loaded,
                    "detail": "table present" + ("" if loaded else
                                                 " but has no reject rule")}
        err = (r.stderr or "").lower()
        if "operation not permitted" in err or "permission denied" in err:
            return {"mechanism": "nftables", "loaded": None,
                    "detail": "cannot read the ruleset without CAP_NET_ADMIN"}
        return {"mechanism": "nftables", "loaded": False,
                "detail": f"table inet {TABLE} is not loaded"}
    if system == "Windows":
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "$p = Get-NetFirewallProfile -Profile Domain,Private,Public; "
             "$r = @(Get-NetFirewallRule -Group 'ClawCal' -ErrorAction "
             "SilentlyContinue).Count; "
             "\"$(($p | Where-Object { $_.DefaultOutboundAction -eq 'Block' "
             "-and $_.Enabled }).Count) $r\""], capture_output=True, text=True)
        try:
            blocked, rules = (int(x) for x in r.stdout.split())
        except ValueError:
            return {"mechanism": "wfp", "loaded": None,
                    "detail": "could not read the firewall profiles"}
        return {"mechanism": "wfp", "loaded": blocked == 3 and rules > 0,
                "detail": f"{blocked}/3 profiles block outbound by default; "
                          f"{rules} ClawCal allow rule(s)"}
    if system == "Darwin":
        r = subprocess.run(["pfctl", "-a", PF_ANCHOR, "-sr"], capture_output=True,
                           text=True)
        if r.returncode != 0:
            return {"mechanism": "pf", "loaded": None,
                    "detail": "pfctl needs root to read the anchor"}
        info = subprocess.run(["pfctl", "-s", "info"], capture_output=True,
                              text=True).stdout
        loaded = "block return" in r.stdout and "Status: Enabled" in info
        return {"mechanism": "pf", "loaded": loaded,
                "detail": f"anchor {PF_ANCHOR}: "
                          + ("rules loaded, pf enabled" if loaded else
                             "rules missing or pf disabled")}
    return {"mechanism": "none", "loaded": False, "detail": "no supported mechanism"}


def _probe(host: str, port: int, timeout: float) -> dict[str, Any]:
    t0 = time.perf_counter()
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return {"target": f"{host}:{port}", "result": "CONNECTED",
                "ms": round((time.perf_counter() - t0) * 1000)}
    except socket.timeout:
        return {"target": f"{host}:{port}", "result": "TIMEOUT",
                "ms": round((time.perf_counter() - t0) * 1000)}
    except OSError as exc:
        code = errno.errorcode.get(exc.errno or 0, str(exc.errno))
        return {"target": f"{host}:{port}", "result": "REJECTED", "errno": code,
                "ms": round((time.perf_counter() - t0) * 1000)}


def self_check(node_url: str, *, timeout: float = 3.0) -> dict[str, Any]:
    """Is the allowlist in force, and does it *reject*?

    active = the mechanism is loaded (or unreadable) AND every probe to a
    non-allowlisted destination was refused fast AND the node is reachable.
    A probe that connected means egress is open. One that timed out means
    packets are dropped (or there is no route) — not the reject the client
    policy requires, so it does not count as active.
    """
    mech = _mechanism()
    probes = [_probe(h, p, timeout) for h, p in PROBES]
    host, port = _hostport(node_url)
    node = _probe(host, port or 443, timeout) if host else {"result": "SKIPPED"}
    connected = [p for p in probes if p["result"] == "CONNECTED"]
    timed_out = [p for p in probes if p["result"] == "TIMEOUT"]
    if connected:
        active, reason = False, (f"egress is open: {connected[0]['target']} "
                                 f"accepted a connection")
    elif timed_out:
        active, reason = False, (f"{timed_out[0]['target']} timed out: packets are "
                                 f"dropped or unroutable, not rejected — a stray "
                                 f"fetch would hang instead of failing")
    elif mech["loaded"] is False:
        active, reason = False, (f"probes were refused but the policy is not "
                                 f"loaded ({mech['detail']}); the refusal came from "
                                 f"elsewhere")
    elif node.get("result") != "CONNECTED":
        active, reason = False, (f"the node at {host}:{port} is not reachable "
                                 f"({node.get('result')}); the allowlist is wrong")
    else:
        active, reason = True, (f"every probe rejected in ≤ "
                                f"{max(p['ms'] for p in probes)} ms; node reachable")
    return {"active": active, "reason": reason,
            "enforcement": "reject" if active else "none",
            "mechanism": mech["mechanism"],
            "mechanism_verified": bool(mech["loaded"]),
            "mechanism_detail": mech["detail"],
            "probes": probes + [dict(node, target=f"node {node.get('target', '')}")],
            "checked_at": round(time.time(), 3)}
