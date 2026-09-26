# The trust domain: node, member devices, one policy

*Implements `sovereign-workbench-v2.md` ("Architecture v2, Re-based") on top of
the existing ClawCal control plane. Status 2026-09-25: built, unit-tested
(60 tests in `tests/test_trust_domain.py`), and verified end to end on real
processes by `scripts/verify_trust_domain.py` (results in
`docs/trust-domain-results.json`).*

The rule everything follows from: **compute and data never cross a boundary the
organisation cannot revoke.** Version 1 enforced it by putting everything on one
box. This version keeps that box and makes it the **node** of a *trust domain*.
Member devices (laptops, desktops) enrol, hold revocable leases, and are
**graded**. The node's policy decides what each grade may receive. The design
never asks a laptop to be trustworthy. It asks the node to know exactly what it
served, to whom, and under which grade.

## 1. How it fits the existing architecture

Nothing was replaced. The appliance's control plane gains an eighth authority,
`trust`, and the new pieces are reached through the same `control` façade the
API layer already uses (the boundary test covers the new router too).

```
              ┌──────────────────────────── NODE (this appliance) ─────────────────────────────┐
  device ────►│ api/deps.caller     principal (token) + device (lease headers) → grade         │
  (clawcal)   │ api/node.py         /api/devices · /api/lease · /api/admit · /api/bundles       │
              │                     /mcp (tool server) · /v1 (OpenAI-compatible) · /evidence    │
              │ control/trust       policy: grade × data class  (8th authority: `trust`)        │
              │ control/devices     enrolment, signed requests, attestation seam, revocation    │
              │ control/leases      node-signed leases, renewal by device key                   │
              │ control/anchors     device hash chains, anchors, tamper events, quarantine      │
              │ placement.py        B1 + placement: node · client · split · refused             │
              │ profiler.py         B5: GGUF facts × device profile → class per model           │
              │ bundles/            TUF-style repo, manifests, the client package               │
              │ toolserver.py       MCP → the existing ToolGateway (policy, approvals, audit)   │
              │ inference.py        /v1 → the existing gateway (adapters, memory, deadlines)    │
              │ evidence/served     what the node served, per session                           │
              │ evidence/gate       B3 against served spans: numbers, tags, dates               │
              │ tools/remote        stage_files · execute_remote (bwrap) · deliver (.docx)      │
              └─────────────────────────────────────────────────────────────────────────────────┘
```

| Spec section | Where it lives | Reuses |
|---|---|---|
| §1 trust domain, data classes | `control/trust.py`, `documents.data_class` | `control_settings`, decision log |
| §10.2 device keys, identity | `control/devices.py` | `signing.py` (Ed25519), `identity` principals |
| §10.3 leases | `control/leases.py` | the appliance signing key |
| §10.4 chained log, anchors, tamper | `control/anchors.py` | decision + audit chains |
| §11 grades, roster | `control/trust.compute_grade`, `GET /api/devices`, sovereignty strip | — |
| §7 B5 classes, §7.3 manifests | `profiler.py`, `bundles/manifest.py` | registry capabilities, Ollama's content-addressed blobs |
| §8 B1 placement, `/admit` | `placement.py` | `router.classify`, `router.select_model`, residency limits |
| §9 signed bundles | `bundles/repo.py` (node), `bundles/verify.py` (client, vendored) | — |
| §12 `execute_remote` | `tools/remote.py` | `tools/sandbox.run_code` (bubblewrap, D-11) |
| §13 served set, B3 gate | `evidence/served.py`, `evidence/gate.py`, `tools/remote.DeliverTool` | provenance classifier, calculator (D-07), docx builder |
| §6 client, §6.2 harness | `clients/clawcal/*` | the existing CLI and its HTTP client |

The node's own harness (the web workbench and `clawcal "<task>"`) is unchanged
and keeps running on the node. Attached mode adds a second loop *on the client*
(opencode), which reaches the same tools through MCP. Both loops are held to one
rule: the internal harness's deliverable check now also reads the served-span
record.

## 2. Grades and the policy

| Grade | Facts the node requires | Default clearance | Detached |
|---|---|---|---|
| A | the node console, or a **managed** Linux/Windows device whose TPM quote a configured verifier accepted | public · internal · confidential · restricted | yes |
| B | a managed device with MDM attestation recorded by an admin | public · internal · confidential | yes |
| C | a recent, passing egress self-check (self-reported) | public · internal | **no** (rule 3) |
| D | anything else: no, failing or stale self-check; or unmanaged *and* detached | public | no |

A device can present evidence. It can't set `managed` or `verified`: the node
discards those fields from anything a device sends. The policy
(`GET/POST /api/admin/trust/policy`) is validated so clearance never *grows* as
trust falls. A task's grade is read live, so a device that drops a grade loses
clearance at its next tool call.

## 3. The flows

**Enrol.** `clawcal device enrol` measures the machine (B5), runs the egress
self-check, generates an Ed25519 key and sends a request signed by that key
(proof of possession). The node derives the device id from the key, grades the
device, and returns the first attached lease plus the repository's root, which
the client pins.

**Signed requests.** Anything that changes a device's standing (report, renew,
log sync) is an envelope signed by the device key over
`canonical({purpose, device_id, ts, nonce, body})`. The node checks it three
ways:

- **Clock skew:** the timestamp must be within ±300 s.
- **Replay:** it must be newer than the last request accepted from the device.
- **Cross-endpoint reuse:** the signature covers the purpose, so a signature made for one endpoint is refused at another.

**Lease.** A lease is a document the node signs, bound to the device key and
(detached) to the manifest hash. The lease token rides on ordinary requests; the
device *key* renews it. Revoking the device stops the token immediately and
makes the next renewal fail. When a lease runs out past its grace window, or
stops verifying, the client **seals**: it removes the manifest and harness
configuration and refuses org work until it renews.

**Admit.** `POST /api/admit` classifies once (router), places (node / client /
split / refused), pins the model, and opens an *attached-loop* task row that the
plan's tool calls, served spans, gate reports and decisions all hang from. The
decision row says "why this model, here".

**Attach.** `clawcal attach "<task>"` admits a plan, writes an opencode
configuration pinned to the node, and runs opencode headless:

- **Providers:** the node is the only provider, and any other base URL is refused in code.
- **Catalogue:** a local models catalogue replaces the remote one, and the fetch is disabled.
- **Upstream services:** update, share, LSP download, plugins and project config are off, by config key *and* environment.
- **Isolation:** opencode's state lives in its own XDG directories.
- **Startup deadline:** the harness must reach the node, whether by an event *or* a node-confirmed contact, within 60 s, or it is killed with the reason (T10, #38723).

Every harness event is appended to the device's chained log.

**Deliver.** The client drafts claims that cite span ids. The node gates every
number, equipment tag and date against the spans *it* served to the session:

- **Kept:** a value found in a cited served span.
- **Re-cited:** a value found in a different served span is kept, and its citation is corrected to that span.
- **Derived:** a result of the node's calculator, from established inputs.
- **Stripped:** anything else is removed and reported.

The `.docx` is built on the organisation's template, and each kept figure links
to `/evidence/<span>`: the scanned page with the value's region outlined.

**Sync and tamper.** `clawcal device sync` uploads the log since the node's
anchor. The node treats any of the following as a **tamper event**:

- a fork from the anchor,
- a gap in the sequence,
- a hash that doesn't recompute,
- a signature the device key didn't make.

A tamper event quarantines the device and revokes its lease. Only an admin can
clear it, and the clear must carry a written reason. The admin may choose to
**re-baseline**: the device then continues its chain from a signed
`rebase_ack`, and the skipped entries are recorded as never accepted. The
device can't repair its own history, because re-signing would be forgery. A
device that stays silent past its lease is reported once.

**Bundles.** The repository is TUF-style: root (offline key), targets, snapshot
and timestamp, each with its own key. The client refuses:

- **rollback** — a version older than one it already trusts;
- **freeze** — an expired timestamp;
- **mix-and-match** — metadata whose versions or hashes don't line up;
- **forgery** — a signature below the role's threshold.

Root rotation walks N → N+1, with each new root signed by both the old root key
and the new one. Targets carry length + SHA-256. Weights are served from where
the node already keeps them; Ollama names blobs by their own hash, which the
node's hash confirms.

**B5 classes.** Model facts are read from each GGUF file's header and tensor
table: parameters, active parameters, experts, layers, KV geometry, licence. For
the manifest the node picks the best model that is licensed for redistribution
(§9.3) and in a *shipped* class (FIT-FAST, SPLIT-PCIE, UNIFIED; rule 2). The
research tiers (CPU-ONLY, STREAM-NVME, MOBILE) are reported but never issued.
Engine flags are pinned, and a `never` list blocks what the survey found
dangerous: llama.cpp's `-hf` / `--model-url` (internet downloads) and `--rpc`
(remote tensors), and FreeToken's `--moe-cache-auto` (T8).

## 4. What was verified, on this workstation

`python3 scripts/verify_trust_domain.py --harness` starts a real node on its own
port and data directory and ingests the scanned inspection report through OCR.
It then runs the **published client zip on the system Python 3.14** (no venv,
no node source tree) inside an unprivileged `pasta` network namespace. The results of the final 26 Sep 2026 run (T1–T17, all passed) are in
`docs/trust-domain-results.json`. Highlights:

- **Client egress.** Before the policy, 1.1.1.1:443 connects. After it, every probe is refused in ≤ 1 ms (`ECONNREFUSED`, reject not drop) and the node stays reachable.
- **This laptop's class.** 8 GB RTX 4060, 15.6 GB RAM, PCIe Gen4 ×8:
  - gpt-oss-20b is **SPLIT-PCIE**: 1.9 GB of shared weights plus KV in VRAM, 10.2 GB of experts in RAM. It comes with the ×8-slot warning.
  - qwen3-8b is **NONE** at the 16k agentic context: a dense model that doesn't fit the 7.5 GB fast tier.
- **Weights hash.** The node's SHA-256 of the gpt-oss-20b weights equals Ollama's content address for the blob.
- **Clearance.** A document classified *confidential* is absent from a grade-C device's listing and refused by name. The gate keeps the served nominal thickness and strips an invented corrosion allowance.
- **The reference task.** The real opencode 1.16.2 drives qwen3-8b on the node through `/v1` and `/mcp`: extract values, then deliver an approval note. In the final full run (26 Sep, 17 of 17 checks passed) it completed **4 of 5** unattended runs **with the organisation's own opencode build** (stopping rule 1 asks for 3 of 5). In the fifth, the model extracted the values but stopped before delivering, and T8 counts that run as incomplete. An earlier session with upstream opencode completed 5 of 5, taking 48–76 s each. One run took 18 minutes: host swap filled while it ran, and the node refused the model load until memory recovered rather than risk the OOM killer. In every run the offline check sampled every socket of the harness's process tree (opencode, its `git` child, python) and found only the node.
- **Tamper, quarantine, clear, re-baseline; revocation; audit and decision chains verify.**
- **Detached (T13–T17):** a software TPM with a vendor-style EK chain attests the device to grade A. The detached manifest, weights, engine and harness are fetched and verified. qwen3-4b runs on the RTX 4060 while opencode touches only loopback. An encrypted slice is gated offline and the node's re-gate agrees. Everything is anchored on rejoin.

In a live run the gate also stripped values a model *invented*. Prompted about
"vessel V-204", which this report never mentions, an 8B model wrote V-204 and a
made-up inspection date into the note. Neither was in a served span, and both
were stripped.

## 5. Deviations from the spec, and why

| Spec says | Built | Why |
|---|---|---|
| Postgres, pgvector (§5.3) | SQLite (D-01) | the node is still one appliance; the seam is unchanged |
| vLLM on the node | any backend in the registry; vLLM is an `openai_compat` row | the gateway was already engine-neutral (A15); this host has 8 GB of VRAM |
| mTLS with device certificates on :8443 | Ed25519 proof of possession at the application layer, plus lease tokens; TLS terminator in front | identity then holds behind any proxy, and every row names user *and* device regardless of transport. Terminate TLS on :8443 with `SOVEREIGN_PUBLIC_URL` pointing at it |
| LiteLLM virtual keys, quotas | the control plane's principals, roles and the external-slot limiter | the existing identity already sits where LiteLLM would; no second identity system |
| gVisor `runsc` for `execute_remote` | bubblewrap (D-11) over a staged *copy* | no Docker/gVisor dependency on the appliance; same guarantees: no network, no host files, deadline |
| Keylime TPM attestation | a native, offline TPM 2.0 verifier (`control/attestation.py`): EK chain to installed vendor roots, credential activation, PCR quote over a device-bound nonce, measured-state baseline; Keylime kept as an alternative verifier | the same guarantees with the tools every Linux node ships (`tpm2-tools`, `openssl`) and no registrar service. Proven against a software TPM with a vendor-style EK chain; this laptop's hardware TPM needs `sudo usermod -aG tss $USER` for the client to open it |
| our own opencode build, catalogue baked in | built: opencode v1.16.2 patched by `bundle/opencode/patch.py` (models.dev fetch, refresh loop, npm installs, update, share, `.well-known` config removed; only the `node` and a loopback `local` provider can exist), catalogue baked in, shipped as a signed target | measured: upstream reached Cloudflare-hosted services and hung 275 s; ours, egress open, **100 of 100 cold starts touched nothing but loopback** |
| WFP (Windows), pf + NE (macOS) client egress | apply, remove and verify implemented for all three (`egress.apply`); Linux verified here; Windows Firewall/WFP and pf verified by `.github/workflows/client-platforms.yml` on real runners | this workstation cannot run Windows or macOS; the workflow runs when pushed. Per-process attribution on macOS still needs the Network Extension build (RQ12) |
| detached engines (llama.cpp / FreeToken) | llama.cpp shipped as a signed engine target, verified at install and at each launch, pinned to the measured GPU adapter | measured here: qwen3-4b on the RTX 4060 via Vulkan; FreeToken remains unmeasured (no FTW weights) |
| instrument slices | built (`bundles/slices.py`, `clients/clawcal/slices.py`): encrypted to the device key (X25519 + ChaCha20-Poly1305, RFC vectors), bound to the lease, gated offline by the node's own rules, delivered as .docx on rejoin | the offline deliverable is an HTML report; the organisation's .docx is written by the node on rejoin, because the client carries no document toolchain |

## 5a. Added since the first pass

- **Detached work, end to end.** A detached device decides admission itself, from its signed manifest: `extract` and `vision` are refused unless an exported slice covers the documents. The engine and the harness build come from the node as signed targets and are re-verified at every launch. The supervisor pins the engine to the GPU the profiler measured; a laptop's Vulkan build otherwise lists the iGPU first. When the device rejoins, everything it did away (admissions, engine starts, tool and model calls) is anchored.
- **TPM attestation** as in §5, plus admin re-baselining (`/api/devices/{id}/attest/reset-baseline`) and EK pinning, which refuses a different TPM answering for the same device.
- **Instrument slices.** An approver exports (`trustctl slice DEVICE DOC…`) only to the grades in `slice_export_grades`, and only documents the device's grade is cleared for. Export counts as serving. The client's local MCP server (`python -m clawcal.localtools`) gives a detached harness `retrieve` and `deliver` over the slice. Revocation stops further fetches, and sealing wipes the wrapped key.
- **One gate, two places.** The gate's rules live in `evidence/gatecore.py` (standard library only), used by the node and vendored into the client with `sealed.py`. Model drafts in prose, with inline and even backticked `span_id` citations, are read identically on both sides.
- **Robustness.**
  - Attached plans idle past `plan_idle_hours` are closed as abandoned.
  - External inference slots are capped per user as well as in total.
  - Devices per user are capped (`max_devices_per_principal`).
  - The client renews its lease when under a quarter remains.
  - Revocation closes the device's open tamper events on the record.
  - `/api/trust/health` and `trustctl health` say what is not ready and what to do about it; the same list is on the Security page.
- **Operations.** `scripts/trustctl.py` covers the policy, the roster, managed/MDM, revoke, clear, classify, publishing the client, weights, engine and harness, TPM roots, slices and root rotation. `bundle/opencode/build.sh` rebuilds the harness.

## 6. Operating it

```bash
# node: expose to the LAN only behind TLS, with tokens
SOVEREIGN_AUTH=token SOVEREIGN_PUBLIC_URL=https://node.lan:8443 ./run.sh
curl -X POST .../api/admin/bundles/client -H "Authorization: Bearer $ADMIN"   # publish the client
curl -X POST .../api/documents/<id>/data-class -d '{"data_class":"confidential"}' ...

# device (the published zip runs on any Python 3.10+, standard library only)
clawcal login <token>
sudo clawcal device egress apply        # Linux; `egress plan --platform windows|macos` elsewhere
clawcal device enrol --name "$(hostname)"
clawcal device manifest                 # B5: what this machine is, for which model
clawcal plan "draft an approval note for IR-2026-0731"
clawcal attach "draft an approval note for IR-2026-0731"
clawcal device sync                     # anchor the chained log on the node
clawcal device status                   # lease, grade, anchor, tamper, what it cannot do here

# admin
GET  /api/devices                       # the roster: grade, lease, anchor, tamper events
POST /api/devices/<id>/managed          # {"managed": true, "mdm_attested": true}
POST /api/devices/<id>/revoke           # the whole revocation procedure
POST /api/tamper/<event>/clear          # {"reason": "...", "rebase": true}
```

## 7. Known limits

- **Grade C is self-reported.** On a machine the organisation doesn't control, the owner can remove the egress policy and lie about it. The node then serves only what grade C is cleared for, and logs everything it served (§10.1). The honest summary: on hardware the organisation controls, violations are prevented; on hardware it doesn't, they are made visible.
- **Anchors bound tampering; they don't prevent it (RQ7).** A grade-C attacker holding the device key can forge a consistent chain offline. What they can't do is make it agree with the anchor the node already holds.
- **The gate checks values, not judgements.** One live run delivered "9.2 mm is below the minimum of 7.5 mm". Every figure in that sentence was served, and the sentence is still wrong. An engineer signs the note; the gate makes sure the numbers they sign are the page's.
- **Small models skip steps.** Whether an 8B model calls `deliver` or just answers varies from run to run. T8 measures that against rule 1 instead of assuming it.
- **A slice already on a device is not recalled.** Revocation stops further fetches, and the lease bounds how long the device can open what it holds. On an unmanaged device, an owner with the key could keep the plaintext. This is why slices go only to grades A and B by default.
- **The software TPM proves the protocol, not the hardware.** Grade A on a real fleet needs each vendor's EK roots installed (`trustctl add-tpm-root`). Some firmware TPMs (Intel PTT) publish their EK certificate online rather than in NV storage; an air-gapped node cannot fetch it, and the client says so.
