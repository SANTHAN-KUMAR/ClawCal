# Deploying ClawCal on a server or cloud GPU instance

This is the path from a fresh Linux host (a refinery server, or an AWS / Azure /
GCP GPU instance) to a running appliance. Everything here has been exercised on
the development workstation. Steps that need root, and so could not be run
there, are marked as such, along with what was verified instead.

---

## 1. Sizing

| GPU VRAM | What `install.sh --models auto` pulls | Notes |
|---|---|---|
| < 6 GB | `qwen3:4b`, `granite3.2-vision:2b` | CPU-only hosts work, slowly |
| 6–11 GB | `qwen3:4b`, `granite3.2-vision:2b`, `qwen2.5vl:3b` | the laptop class |
| 11–22 GB | `gpt-oss:20b`, `qwen3:8b`, `qwen2.5vl:7b`, `granite3.2-vision:2b` | e.g. L4, A10G |
| 22–70 GB | `qwen3:32b`, `gpt-oss:20b`, `qwen2.5vl:7b`, `qwen3:8b` | e.g. L40S, A6000 |
| ≥ 70 GB | `gpt-oss:120b`, `qwen3:32b`, `qwen2.5-coder:32b`, `qwen2.5vl:7b`, `gpt-oss:20b` | A100/H100 80 GB — the PS's reference tier |

Each set is chosen so that **every model fits in VRAM with room for its KV
cache.** A model that spills into host RAM is how a busy host meets the OOM
killer. Admission refuses such loads (see §7), so pulling them would waste disk.

Host RAM: at least 16 GB. The control plane itself uses about 1 GB; the rest
is for the inference backend and the OS page cache.

Disk: model weights plus about 5 GB. Put `OLLAMA_MODELS` on local NVMe
(ext4/xfs), not on a network or FUSE mount; the hardware probe warns if it is
on FUSE.

Multi-GPU hosts are supported as one pool: VRAM is summed across devices. The
backend decides placement, and the scheduler budgets against the total.

---

## 2. Install

```bash
git clone <this repository> /opt/clawcal && cd /opt/clawcal/sovereign-workbench
sudo ./ops/install.sh --host 0.0.0.0 --install-ollama
```

The installer does these steps, in this order:

1. installs system packages (tesseract, bubblewrap, nftables, Python 3.10+) —
   dnf on Fedora / RHEL / Amazon Linux 2023, apt on Ubuntu / Debian;
2. creates a `clawcal` system user owning `/var/lib/clawcal`;
3. builds a virtualenv, with the CPU build of torch (embeddings run on CPU);
4. **fetches the embedding model now**, while the network is still open;
5. pulls the model set sized to the GPU (`--models` overrides);
6. writes `/etc/clawcal/clawcal.env` (never overwritten on re-run);
7. installs a hardened `clawcal.service`, with a memory cap and
   `ProtectSystem=strict`;
8. applies the egress policy. Over SSH this is **rollback-guarded**: it removes
   itself in 180 s unless the self-test passes and confirms it;
9. starts the service, runs the sovereignty self-test, and writes the signed
   report. Only then does it make the egress policy persistent across reboots.

`--dry-run` prints every step and changes nothing. `--offline DIR` installs
wheels from a directory for an air-gapped host. Build that directory on a
connected machine with `pip download -r requirements.txt -d DIR`.

*Verified here:* the dry run, `shellcheck` over both scripts,
`systemd-analyze verify` on the generated unit, and the egress ruleset loaded
and exercised inside an unprivileged network namespace (§5). *Not run here:* the
root-only steps on a real host.

---

## 3. Authentication

The server refuses to listen on a non-loopback address unless
`SOVEREIGN_AUTH=token`. The installer sets this whenever `--host` is not
loopback.

* The owner's first token is written once, to
  `/var/lib/clawcal/owner.token` (mode 0600).
* Issue a token per person through the admin API: a principal has a role of
  `viewer`, `engineer`, `approver` or `admin`, and every task, approval,
  decision and audit row names them.

  ```bash
  curl -H "Authorization: Bearer $OWNER" -X POST \
    https://clawcal.internal/api/admin/principals \
    -d '{"name":"r.iyer","role":"approver","department":"Inspection"}'
  ```

* The web UI asks for a token on first load. The terminal client uses
  `clawcal login <token>`.
* `SOVEREIGN_SEPARATION_OF_DUTIES=1` forbids anyone from approving an action
  on a task they submitted.

A request that arrives through a reverse proxy is **never** trusted as the
local owner, even from 127.0.0.1. Put the proxy in front and keep token auth
on.

---

## 4. TLS and the reverse proxy

Run exactly **one** ClawCal process per host. The scheduler, the residency
manager and the live event stream are in-process, and a second worker would be
a second scheduler admitting work against the same GPU. `--workers` other than
1 is refused. Scale out with one appliance per GPU host.

Example (Caddy obtains and renews certificates itself):

```
clawcal.internal {
    reverse_proxy 127.0.0.1:8794 {
        flush_interval -1          # server-sent events must not be buffered
    }
}
```

With nginx, set `proxy_buffering off;` and `proxy_read_timeout 3600s;` for
`/api/stream`, and `client_max_body_size` at least `SOVEREIGN_MAX_UPLOAD_MB`
(default 200).

In that layout, bind ClawCal to 127.0.0.1 and open only 443 inbound:
`SOVEREIGN_INGRESS_PORTS="22 443"`, which is the default.

---

## 5. Egress policy on a cloud instance

`ops/egress-policy.sh` sets outbound traffic to DROP by default and logs every
refused packet (`SOVEREIGN-EGRESS-DENY`). On a cloud VM, three things a naive
drop-everything policy breaks are allowed explicitly:

* **inbound SSH and 443** (`SOVEREIGN_INGRESS_PORTS`). The v1 script dropped
  all new inbound connections and would have locked the administrator out;
* **DHCP and IPv6 neighbour discovery**. Without them the instance loses its
  address at lease renewal;
* **NTP**, to `SOVEREIGN_NTP_SERVERS` only (on AWS: `169.254.169.123`).

The **instance metadata service** (169.254.169.254, fd00:ec2::254) is dropped
and logged by name as `SOVEREIGN-EGRESS-DENY IMDS`: it hands out the instance
role's credentials.

Consequences to plan for:

* The AWS SSM agent, CloudWatch agent and package updates stop working. That
  is the point. Update the host through a maintenance window that removes and
  re-applies the policy.
* An IAM instance role becomes unreachable from the host. Give the instance
  none.

*Verified here,* in a private network namespace with a dummy default route:

- outbound UDP to 8.8.8.8, 1.1.1.1 and 169.254.169.254 was refused by the
  ruleset (`EPERM`, and the drop counter incremented);
- DHCP and the allowed NTP server were sent;
- loopback was unaffected.

---

## 6. Models on several Ollama servers

`OLLAMA_URLS` takes a comma-separated list, and each model is routed to the
server that holds it:

```
OLLAMA_URLS=http://127.0.0.1:11434,http://10.0.2.15:11434
```

Residency is scheduled from `nvidia-smi` for the whole host, so two local
servers on one GPU are coordinated rather than competing. Remote endpoints are
still loopback-only unless you add them to the egress allowlist. Outside
loopback they are egress, which the policy refuses by design.

---

## 7. Why a model sometimes waits

Before any load, admission prices the host RAM a model would spill into. That
spill is its footprint (weights plus a KV cache priced from its attention
geometry at the requested context) minus the VRAM free for it. Admission
compares that against available RAM minus a reserve: `SOVEREIGN_RAM_RESERVE_MB`
or 12% of RAM, whichever is larger. If it does not fit, the task waits with
the numbers in its reason, or falls back to a smaller model that fits.

This exists because it happened. On the 16 GB development workstation, a
12 GB model admitted against free VRAM alone spilled 5 GB into a host that had
5.7 GB available. systemd-oomd then killed the desktop. The gateway applies the
same check to every model call, including those made from inside tools.

---

## 8. Operations

| Task | How |
|---|---|
| health (load balancer) | `GET /api/health` (unauthenticated, says nothing confidential) |
| status | `clawcal status` |
| sovereignty evidence | `clawcal selftest --report` → signed PDF + `.sig.json` |
| audit export | `clawcal audit export audit.jsonl`; verify anywhere with `clawcal verify audit.jsonl` |
| quotas at runtime | `POST /api/admin/limits` (persisted, and recorded as a decision) |
| a new model | `ollama pull <tag>`, then `POST /api/admin/models`; enabled only if served |
| organisation templates | drop `approval_note.docx` / `report.docx` in `$DATA/templates`; check with `python -m sovereign.deliverables.templates validate <file>` |
| backup | stop the service, then copy `$DATA/db`, `$DATA/keys`, `$DATA/templates`, `$DATA/artifacts`, `$DATA/evidence` |
| upgrade | `git pull && sudo ./ops/install.sh`; schema migrations are additive and run at start |

Keep `$DATA/keys/appliance.ed25519` backed up and secret. Its public key
(`appliance.ed25519.pub`) is what a security officer uses to verify reports.
