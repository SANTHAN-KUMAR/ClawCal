# The Sovereign Workbench — Architecture v2, Re-based

An organisation-provisioned inference server on a protected LAN, a native client on
every user's own machine that carries the agent loop, and a signed per-hardware-class
bundle for the exceptional case where a user has to compute without the server.

Everything in this document is downstream of one rule: **compute and data never cross
a boundary the organisation cannot revoke.** Version 1 enforced that rule by putting
everything on one air-gapped box. Version 2 enforces it with a *trust domain* — a
policy that says which task classes and data classes may run on which device grade —
because the problem statement never said what the hardware was, and a design that
assumes a rack is wrong on a laptop and meaningless on a phone.

| | |
|---|---|
| **Deployable unit** | one *trust domain*: ≥ 1 compute node on a protected LAN, ≥ 0 member devices, one policy |
| **Modes** | **Attached** (default: client on LAN, inference on the node) · **Detached** (exception: signed bundle, inference on the user's own hardware) |
| **Server engine** | vLLM, weights fully VRAM-resident, continuous batching |
| **Client** | native app (Windows `.exe`, macOS `.app`, Linux); our own build of **opencode** (MIT) as the harness; MCP tools served by the node |
| **Inbound to node** | one TLS port `:8443`, mTLS with device certificates |
| **Outbound from node** | none — `nftables` `OUTPUT policy drop`, logged and attributed |
| **Outbound from client** | allowlist: the node, localhost, and (detached only) hosts pinned in a signed manifest |
| **Status** | architecture spec v2 · v1 measured MVP carried forward · v2 research questions open |
| **Sources** | v1 `arc.md` (87-repo survey, build spec, MVP measurements) · web verification 22 Sep 2026, listed in §20 |

The two properties an auditor can inspect without reading source are still the point.
They have changed shape: the *node's* no-egress claim is still a firewall and a
non-empty denial log; the *client's* claim is now a **grade** (§11) — hardware-attested
on a managed device, self-reported on an unmanaged one — and the node's policy decides
what each grade may receive. The design never asks a user's laptop to be trustworthy.
It asks the node to know exactly what it served, to whom, and under which attestation.

---

## Contents

1. [The invariant and the trust domain](#1-the-invariant-and-the-trust-domain)
2. [Two modes, one client](#2-two-modes-one-client)
3. [What changed from v1, and what the review found](#3-what-changed-from-v1-and-what-the-review-found)
4. [Reference tasks](#4-reference-tasks)
5. [The node — hardware, engine, gateway, store](#5-the-node--hardware-engine-gateway-store)
6. [The client — a native app with the agent loop](#6-the-client--a-native-app-with-the-agent-loop)
7. [B5 — Hardware classification and placement](#7-b5--hardware-classification-and-placement)
8. [B1 — Admission router, extended with placement](#8-b1--admission-router-extended-with-placement)
9. [Distribution — signed bundles per hardware class](#9-distribution--signed-bundles-per-hardware-class)
10. [B6 — Lease, revocation, attestation, tamper evidence](#10-b6--lease-revocation-attestation-tamper-evidence)
11. [B4 — Egress proof, now a grade per device](#11-b4--egress-proof-now-a-grade-per-device)
12. [B2 — Sandbox: remote by default, local by exception](#12-b2--sandbox-remote-by-default-local-by-exception)
13. [Data plane and B3 — the provenance gate](#13-data-plane-and-b3--the-provenance-gate)
14. [Detached mode — what it is and is not](#14-detached-mode--what-it-is-and-is-not)
15. [The complete tech stack table](#15-the-complete-tech-stack-table)
16. [Repository layout and service map](#16-repository-layout-and-service-map)
17. [Research questions](#17-research-questions)
18. [Build order and stopping rules](#18-build-order-and-stopping-rules)
19. [Known limits and traps](#19-known-limits-and-traps)
20. [Sources consulted for this revision](#20-sources-consulted-for-this-revision)

---

## 1. The invariant and the trust domain

> **Compute and data never cross a boundary the organisation cannot revoke.**

Test every environment against it, and the design falls out:

| Environment | Passes? | Why |
|---|---|---|
| Node on the org's LAN | yes | the org owns it, unplugs it, audits it |
| Client on the org's LAN, inference on the node | yes | the node decides what it serves; the client holds a revocable lease |
| Client off-site, inference on the client's own hardware | yes, **conditionally** | the org can revoke the lease, the config, any exported data; it cannot revoke public model weights, and does not need to |
| Org-controlled VPS | conditionally | control passes, hypervisor trust does not; treated as a node of lower grade, org decision |
| Third-party inference API | **no** | nothing to revoke; the only thing the "no compute over the internet" rule strictly forbids |
| Bytes over the internet to org-owned endpoints (VPN, manifest-pinned downloads) | yes | transport is not compute; the manifest, not the channel, is the control |

A **trust domain** is one or more nodes plus zero or more member devices plus one
policy. The policy has two axes — *device grade* (§11) and *data class* — and says
which grades may attach, which data classes each grade may retrieve, and whether a
grade may run detached. That policy is the deployable unit's configuration; the
hardware is whatever the profiler (§7) finds.

**The security boundary is the domain, not the machine.** v1's sentence "need-to-know
is enforced by which box holds which corpus" survives inside the domain: the corpus
lives on the node, retrieval is a node-side tool, and by default **the corpus never
leaves the node** (§14). What moves to devices is compute capability, not data.

---

## 2. Two modes, one client

```
             ┌──────────────────────── PROTECTED LAN ───────────────────────────┐
             │                                                                  │
             │   NODE (Linux, ≥1 GPU)          nftables OUTPUT drop + Tetragon  │
             │   ┌───────────────────────────────────────────────────────────┐  │
             │   │  control plane · identity · leases · policy · bundle store │  │
             │   │  vLLM (resident MoE) · vLLM-VLM (OCR) · ONNX embed/rerank  │  │
             │   │  Docling+Presidio ingest · Postgres (corpus·spans·audit)    │  │
             │   │  MCP tool server: retrieve · execute_remote · deliver (B3)  │  │
             │   │  gVisor sandboxes (B2) · LiteLLM quota · denial log (B4)    │  │
             │   └────────────────────────────▲──────────────────────────────┘  │
             │                    TLS :8443, mTLS│device cert                    │
             │   ┌────────────────────────────┴──────────────────────────────┐  │
             │   │  CLIENT — ATTACHED MODE  (Windows / macOS / Linux)         │  │
             │   │  agent loop (our opencode build) · local read/edit tools    │  │
             │   │  egress allowlist = {node, localhost} · lease + attest agent│  │
             │   │  hash-chained audit log → synced to node                    │  │
             │   └───────────────────────────────────────────────────────────┘  │
             └──────────────────────────────────────────────────────────────────┘

             ┌──────────────── ELSEWHERE (other site, home, travel) ─────────────┐
             │   CLIENT — DETACHED MODE  (same binary, different manifest)        │
             │   local engine chosen by hardware class (§7): llama.cpp / MLX /    │
             │   FreeToken · local files only · no team corpus by default          │
             │   egress allowlist = {localhost} ∪ manifest-pinned download hosts   │
             │   lease TTL counts down; expiry seals org config + any exported data│
             └────────────────────────────────────────────────────────────────────┘
```

**Attached mode** is v1 with one move: the agent loop leaves the node's `worker`
container and runs in the client. Inference, corpus, retrieval, ingestion, the
provenance gate and the hardened sandbox all stay on the node and are exposed to the
client as MCP tools. The client is a *thick UI with a loop in it*, not a second copy of
the product.

**Detached mode** is the exception. The same client, with a signed manifest for its
hardware class, runs a local engine against local files. It is the workbench without
the instrument (§14) unless the org deliberately exports more.

**Read the diagram in two directions.** Downward is the request path. Upward is the
trust path: the further up a claim travels, the more of it has been checked — a span
id minted at OCR on the node is verified at the node's gate before it reaches a
`.docx`, and a client's audit log is verified against the node's last anchor before
it counts as evidence.

---

## 3. What changed from v1, and what the review found

### 3.1 Survives untouched

- **B3, the provenance gate.** Ordinary code; hardware-independent; still the
  difference between a chat assistant and an instrument.
- **The OpenAI-compatible API as the seam.** The harness never knows which engine it
  is talking to. This is what lets one client binary span vLLM, llama.cpp, MLX and
  FreeToken.
- **The sparsity argument.** The MVP's result — a 30B-A3B model at 2-bit scored F1 1.0
  where a dense 4B at 4-bit scored 0.0, same harness, same task — is what makes one
  model *family* span the hardware ladder. Knowledge at low active compute is the
  thing that scales down.
- **Postgres as the node's single store.** Corpus, spans, sessions, queue, audit.

### 3.2 Promoted

- **B1** gains a placement dimension. It still classifies the task once and pins the
  model for the plan; it now also takes the device profile as input and decides
  *where* each stage runs.
- **The MVP's laptop findings** stop being a caveat. v1 said "the laptop is not the
  team box". In v2 the laptop *is* a target tier, so the 16 GB unified-memory sweep is
  the only measured data point for the UNIFIED class, and "capacity is not
  throughput" is a design rule rather than an apology.
- **The fleet problem** is no longer out of scope. Signed offline bundles and one-way
  audit replication are the distribution path for every device, not a second-sale
  concern.
- **opencode** becomes *the* client harness instead of a bench tool beside a
  pydantic-ai product loop. With the provenance-critical code on the node, the client
  does not need a bespoke loop; it needs a good harness with MCP and a permission
  model, which is what opencode is.

### 3.3 Broken by the pivot

| v1 assumption | Why it breaks | v2 replacement |
|---|---|---|
| one air-gapped box per team | clients are on ordinary machines with internet | trust domain + device grades (§1, §11) |
| browser-only clients, zero local compute | the agent loop and, detached, the engine run on the client | native client (§6) |
| FreeToken as the shared generalist | its expert cache needs single-user locality; concurrency destroys it (v1 said so) | vLLM on the node; FreeToken moves to the SPLIT-PCIE client class (§7) |
| "buy RAM before VRAM" | true for a single-user client; inverted for a multi-user node that must hold weights resident | per-class purchase rules (§5, §7) |
| gVisor as *the* sandbox | Linux-only | remote gVisor by default; OS-native isolation or WASI locally (§12) |
| nftables + Tetragon as *the* proof | Linux-only | node keeps it; clients get a per-platform grade (§11) |
| one audit table | client actions happen off the node | hash-chained device logs anchored to the node's table (§10) |
| Ubuntu as *the* host OS | clients run Windows and macOS | node stays Ubuntu; client is multi-platform |
| LiteLLM's free SSO cap of five | a shared node serves more than five | identity lives in our control plane; LiteLLM sees virtual keys only (T3) |

### 3.4 What the review found (22 Sep 2026)

Each of these changes a decision below and is sourced in §20.

1. **opencode can hang at startup with no error.** Issue #38723 (v1.18.4, July 2026):
   `opencode run` intermittently stalls at `init`, holding an established connection
   to `models.dev` with no timeout; a controlled 40-run trial showed
   `OPENCODE_OFFLINE=1` made no difference to the fetch. Two community offline forks
   exist and both do the same thing: build the binary with the model catalogue baked
   in (`MODELS_DEV_API_JSON=… bun run build --single`), bundle ripgrep and LSPs, serve
   the web UI locally. **Decision:** ship our own build (§6). Never ship an upstream
   release binary to a client. On clients, the egress policy must **reject** (RST /
   ICMP unreachable), not silently **drop**, so a stray fetch fails in milliseconds
   rather than hanging — the node's `drop` policy is correct for the node, wrong for
   the client. This is the same failure shape as FreeToken's T8: the dependency does
   not error, it waits. A wall-clock deadline on harness launch is mandatory.
2. **The node engine is settled.** gpt-oss-120b (MoE, ~5 B active, MXFP4, ~60–65 GB)
   runs on a single 96 GB RTX PRO 6000 under vLLM with published multi-user
   throughput; FP8 KV cache is the default there. Dense 70B is the *worst* fit for a
   shared card: its KV per token is roughly 4–8× gpt-oss-120b's (80 layers × 8 KV
   heads × 128 head-dim vs 36 × 8 × 64 with half the layers sliding-window), so five
   users at 32k context is ~25 GB of KV beside ~70 GB of FP8 weights and does not fit
   96 GB, where the same five users under gpt-oss-120b need ~6 GB. **Decision:**
   "70B-class" means capability class; the shipped node model is a mid-size MoE with
   small KV. Verify in week zero.
3. **Blackwell workstation kernels are rough.** Multiple 2026 reports of vLLM on
   SM120 needing specific versions, `VLLM_MOE_FORCE_MARLIN=1`, or patched FlashInfer
   for NVFP4 MoE paths; a CUTLASS bug open without vendor response as of March 2026.
   MXFP4 gpt-oss paths are reported working. **New trap T9** (§19): pin the exact vLLM
   version and kernel backend per node profile, and treat NVFP4 as unproven on SM120.
4. **Attestation is a ladder, and most rungs need a vendor's server.** Apple's Managed
   Device Attestation requires MDM, a live call to Apple's attestation servers, and is
   rate-limited to one new attestation per seven days; App Attest and Play Integrity
   likewise depend on Apple/Google servers. Only TPM 2.0 attestation (Keylime, CNCF)
   verifies with the vendor keys hosted locally and no internet at all. **Decision:**
   the design never depends on client attestation for enforcement. Enforcement is on
   the node; attestation sets the client's *grade*; the policy decides what each grade
   receives (§10, §11). New trap T11.
5. **The small-model floor is measured, not guessed.** On BFCL, Qwen3-4B scores ~35 %
   multi-turn, Qwen3-1.7B ~17 %, Qwen3-0.6B ~1 %; an agent-tuned 3B reaches ~56 %.
   The reference task in the MVP needed 11 turns at precision 1.0 and recall 1.0.
   **Decision:** phones and tablets are thin clients for agentic work in attached
   mode; detached agentic work on mobile is not a v2 target (§14, §17 RQ1). New trap
   T12.
6. **A portable sandbox exists for pure code only.** CPython on WASI is a tier-2
   platform (PEP 816 accepted March 2026 for 3.15), with no sockets, no threads, and
   no wheel platform tag — so no numpy, pandas, python-docx. **Decision:** WASI is the
   universal `execute_local` for scripts; the data and deliverable tooling stays on
   the node (§12).
7. **NVMe streaming is real on Apple Silicon.** A pure-C/Metal engine streams a 397B
   MoE (209 GB on disk) through a 48 GB MacBook at ~4.4 tok/s with tool calling
   intact; Apple's own "LLM in a flash" (2023) is the reference paper. The STREAM-NVME
   class therefore has engines — one per platform, none shared. **Decision:** it stays
   a class in the profiler and a research question, not a shipped tier (§7, §17).
8. **FreeToken now ships a Windows desktop build** alongside the Linux CLI. What the
   Windows build actually runs on (native or WSL2) is unverified; the SPLIT-PCIE class
   on Windows defaults to llama.cpp `--n-cpu-moe` until measured.
9. **An inconsistency in the intermediate design, corrected.** "The sandbox problem is
   confined to detached mode" was wrong: a coding task on a local repository needs
   local execution in attached mode too. Resolved with two tools, `execute_remote`
   (default) and `execute_local` (exception), in §12.

---

## 4. Reference tasks

**Attached (the instrument).** The brief's own example: scanned inspection report in,
approval note out, on the organisation's letterhead, every figure clickable to the
scanned page it came from. This is the task the stopping rule (§18) is written
against.

```
  client                          node
  ──────                          ────
  drop PDF ─── upload ──────────► ingest: Docling + PaddleOCR-VL → SPAN IDS MINTED
                                  Presidio flags PII · chunk · embed (bge-m3, CPU)
                                  Postgres: pgvector + tsvector
  "draft the approval note"
      │
      ├─ POST /admit ───────────► B1: class = extract; model + tool profile PINNED
      │                            reason written to audit, shown as one line
      ▼
  agent loop (opencode)
      ├─ retrieve(query) ───────► hybrid + rerank → TOP-K PASSAGES WITH SPAN IDS
      │                            node records which spans this session was served
      ├─ draft findings locally
      ├─ deliver(claims+spans) ──► B3 GATE: every number/tag/date resolves to a served
      │                            span or is STRIPPED and reported
      │                            docxtpl on the org template → .docx
      ▼
  approval note, fully cited ◄──── download; audit: served spans, gate report, hashes
```

**Attached (the coding task).** Repository on the user's machine; `read`/`edit`
local; `execute_remote` runs tests in a node-side gVisor sandbox against a staged
copy of the workspace; `execute_local` only where §12 allows it.

**Detached (the workbench).** Same client, local engine, local files, local
read/edit, `execute_local` under the platform's isolation. No `retrieve`, no
`deliver`, no gate — unless the org has exported an *instrument slice* (§14).

---

## 5. The node — hardware, engine, gateway, store

### 5.1 Hardware profiles, re-based

The node holds weights **fully resident** and serves several users concurrently.
That inverts v1's purchase rule for this tier: **VRAM decides the model, host RAM
decides ingestion throughput.** Every row implies its quantisation.

| Profile | GPU | Host RAM | Model (resident) | Concurrency (est.) | Role |
|---|---|---|---|---|---|
| **Team** | 1 × RTX PRO 6000 Blackwell, 96 GB | 128 GB | gpt-oss-120b MXFP4 (~63 GB) · or Qwen3.6-35B-A3B FP8 (~35 GB) | 5–10 agentic sessions at 32k, FP8 KV | **the shipping unit** |
| **Team-lite** | 1 × RTX 5090 / L40S, 32–48 GB | 64–128 GB | Qwen3.6-35B-A3B FP8 (~35 GB) | 3–5 at 16–32k | budget node |
| **Lead** | 2 × RTX PRO 6000, 192 GB | 256 GB | DeepSeek-V4-Flash class (~180 GB FP8, TP=2) | 5–10 | heavy analysis |
| **Bench** | any of the above, plus llama.cpp | — | — | 1 | week-zero measurement only |

```
resident_weights ≈ params × bytes_per_param     BF16 2 · FP8 1 · MXFP4/NVFP4 ≈ 0.5
kv_per_token     = layers × kv_heads × head_dim × 2 × bytes     (FP8 KV: 1 byte)
vram ≥ resident_weights + N_users × ctx × kv_per_token + ~8 GB (VLM, activations, graphs)
```

KV sizing is what separates a shared node from a personal box. Take the two shipped
candidates at FP8 KV: Llama-class dense 70B is ~160 KB/token; gpt-oss-120b is ~36
KB/token before its sliding-window layers are counted. Multiply by users × context
before choosing "70B". These are estimates; week zero measures them.

Vision/OCR (PaddleOCR-VL, 0.9 B) runs in a second vLLM process with a capped
`--gpu-memory-utilization`; embedding and reranking (bge-m3, bge-reranker-v2-m3) run
on CPU via ONNX Runtime, as in v1.

### 5.2 Engine

| Slot | Choice | Licence | Why | Turned down |
|---|---|---|---|---|
| Generalist (node) | **vLLM**, resident MoE | Apache-2.0 | continuous batching, prefix caching, FP8 KV; the only engine here that is designed for concurrent users | FreeToken (single-user cache locality), Ollama, LM Studio |
| Vision / OCR | vLLM + PaddleOCR-VL 1.5 | Apache-2.0 | 0.9 B, layout-aware, beats far larger models on OmniDocBench (v1 survey) | Qwen3-VL-235B, bare Tesseract |
| Embed / rerank | bge-m3 + bge-reranker-v2-m3, ONNX on CPU | MIT | returns ~4 GB of VRAM to KV | GPU-resident embedders |
| Bench only | llama.cpp | MIT | `--n-cpu-moe` / `-ot` express tensor-class placement | — |

### 5.3 Gateway, identity, store, perimeter

- **LiteLLM, MIT core only**, for virtual keys, budgets and TPM/RPM per user. It never
  sees a human identity — it sees a virtual key the control plane minted. The T3 trap
  (SSO, SAML, audit behind `enterprise/`) is sidestepped by construction, not by
  staying under five users.
- **Identity** is the control plane's: local accounts (argon2id), OIDC-ready, and
  **device certificates** bound to a device key (hardware-backed where the platform
  allows, §10). Every client connection is mTLS; every audit row carries user *and*
  device.
- **Postgres 16 + pgvector + tsvector**: corpus, spans, sessions, `SKIP LOCKED`
  queue, audit, denial log, lease table, device registry. One store, one backup.
- **Perimeter** as v1: `nftables` `OUTPUT policy drop` except loopback and the client
  VLAN, every rejected packet logged with a prefix; **Tetragon** attributes each
  attempt to a process; both streams land in the audit table.
- **Segmentation.** The node is reachable only from the client VLAN on `:8443`. The
  bundle store (§9) is a path on the same port. Nothing else is published.

---

## 6. The client — a native app with the agent loop

One installable per platform. It is the same binary in both modes; the manifest it
holds decides which mode it is in.

### 6.1 Components

| Component | What it does | Built or installed |
|---|---|---|
| **Harness** | the agent loop: plan · call · observe · iterate; TUI and desktop UI | **our build of opencode** (MIT), §6.2 |
| **Tool bridge** | MCP client to the node's tool server (attached) or to the local tool server (detached) | opencode-native MCP |
| **Profiler (B5)** | measures the machine on first run; selects the hardware class; requests the matching manifest | built |
| **Engine supervisor** | starts/stops the local engine named in the manifest with pinned flags; hard client-side timeouts on every generation call | built |
| **Egress policy** | installs the OS-level outbound allowlist; verifies it is active before the harness starts | built, per platform (§11) |
| **Trust agent (B6)** | device key, lease renewal, attestation where available, manifest verification at launch, hash-chained audit log, sync | built |
| **Local tools** | `read`, `edit`, `glob`, `grep` on the user's workspace; `execute_local` where §12 permits | opencode tools, curated |

### 6.2 Why our own build of opencode, and what is in it

v1 chose opencode for MIT with no `ee/` gate, real headless surfaces (`serve`,
`run --format json`), `permission` as a config key, managed settings at
`/etc/opencode/`, and native MCP. All of that holds. What the review adds is that the
upstream binary makes a startup fetch that can hang forever (§3.4 item 1), and that
the community has already shown the correct mitigation is at *build* time.

The client build:

- Bakes the model catalogue in at build (`MODELS_DEV_API_JSON`), so no `models.dev`
  request exists in the binary. `OPENCODE_MODELS_URL` still points at the local
  mirror as belt-and-braces; v1's note that it is respected inconsistently stands.
- Bundles `@ai-sdk/openai-compatible`, ripgrep, and the web UI `dist`; disables LSP
  download (`"lsp": false`) and vendors any language servers the org wants.
- Removes the `share` service, the `app.opencode.ai` proxy, auto-update, and the
  `.well-known/opencode` remote-config fetch from the build rather than from config.
  Config keys stay set too (`"autoupdate": false`, `"share": "disabled"`,
  `"disabled_providers": [...]`), because a config key that is missing on the next
  version bump should still be caught by the egress policy, and both layers should
  agree.
- Pins the provider block to the node (attached) or `127.0.0.1` (detached) and
  refuses any other `baseURL` at the code level — "no cloud fallback" is a compile
  rule, not a setting a user can flip.
- Installs the managed config at the platform's highest-precedence path
  (`/etc/opencode/` on Linux; the equivalents on Windows and macOS are set by the
  installer as machine-wide files the user cannot lower).

The **offline check** in CI runs the built client in a network-isolated container
(the Chetic fork's `test/offline/` is the model) and fails on any socket that is not
loopback or the node. It inspects the **runtime child** (`bun`/`node`), not just the
process named `opencode` — v1's lesson, still the easy one to get wrong.

### 6.3 The client configuration

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "node": {                                       // attached
      "npm": "@ai-sdk/openai-compatible",           // vendored in the build
      "name": "trust-domain node",
      "options": { "baseURL": "https://node.lan:8443/v1" },  // mTLS via the client's own cert
      "models": { "generalist": { "name": "Generalist — MoE (node)" } }
    },
    "local": {                                      // detached — filled in from the manifest
      "npm": "@ai-sdk/openai-compatible",
      "name": "local engine",
      "options": { "baseURL": "http://127.0.0.1:8080/v1" },
      "models": { "generalist": { "name": "Generalist — MoE (local)" } }
    }
  },
  "model": "node/generalist",
  "autoupdate": false, "share": "disabled", "lsp": false, "instructions": [],
  "disabled_providers": ["anthropic", "openai", "google", "openrouter"],
  "mcp": {
    "node-tools": { "type": "remote", "url": "https://node.lan:8443/mcp", "enabled": true }
  },
  "tools": { "webfetch": false },
  "permission": { "bash": "ask", "edit": "ask" },
  "server": { "port": 4096, "hostname": "127.0.0.1", "mdns": false }
}
```

The `provider` block no longer points at the MVP's `:8080` model by name (a v1
inconsistency): model ids are the router's, and the manifest rewrites `local` on
install.

### 6.4 Platforms

| Platform | Client | Local engine (detached) | Notes |
|---|---|---|---|
| Windows 10/11 x64 | `.exe` installer, service for the egress policy | llama.cpp (CUDA / Vulkan); FreeToken Windows build once measured | WSL2 optional for `execute_local` |
| macOS (Apple Silicon) | signed `.app` | llama.cpp (Metal) / MLX | egress attribution needs a Network Extension, §11 |
| Linux x64 | `.deb`/`.rpm`/AppImage | llama.cpp (CUDA/ROCm/Vulkan) or FreeToken (CUDA 13, r580+) | closest to the node; TPM attestation via Keylime agent |
| iOS / Android | thin client (attached only) | — | agentic floor not met (T12); retrieval + answer only |

---

## 7. B5 — Hardware classification and placement

**Built, not installed.** Nobody in the survey packages this: engines each assume
their own hardware, and the "any hardware" clause of the problem statement is exactly
the gap. The profiler is to hardware what B1 is to tasks: **one deterministic,
explainable classification, written to the audit log with its reason.**

### 7.1 What is measured (first run, and on demand)

| Measurement | How | Why it matters |
|---|---|---|
| Memory topology | `discrete` (VRAM + host RAM over PCIe) · `unified` (Apple Silicon, AMD Strix Halo, phones) · `cpu-only` | decides whether "offload" means anything |
| VRAM, host RAM, free of each | driver APIs / OS | the two tier sizes |
| PCIe effective GB/s | a pinned H2D/D2H copy benchmark (`ft bench bw` where FreeToken is present) | catches the ×8-slot trap: 11.8 GB/s where ×16 gen 5 gives ~52 |
| NVMe sequential read GB/s | a 4 GB read | cold start for large pools; the only thing that makes STREAM-NVME usable |
| Unified-memory GPU working-set ceiling | Metal `recommendedMaxWorkingSetSize` / equivalent | the MVP hit 12.7 GB on a 16 GB Mac; this is the real fast tier |
| Compute backend | CUDA (major/minor) · ROCm · Metal · Vulkan · CPU (AVX-512 / NEON) · NPU | picks the engine build |
| OS + version, virtualization available, TPM / Secure Enclave present | OS APIs | sandbox and attestation options (§10–§12) |
| Thermal/battery class | mobile only | sustained decode budget |

### 7.2 The classes

The class is a function of *topology* and of *where the candidate model lands in the
tiers*. A machine can be several classes for several models; the profiler reports the
class **per candidate model**, and the router picks.

| Class | Topology | Model vs tiers | Placement | Engine | Purchase rule |
|---|---|---|---|---|---|
| **FIT-FAST** | any | ≤ fast tier | full residency | vLLM (node) · llama.cpp (client) | buy VRAM |
| **SPLIT-PCIE** | discrete | fast < model ≤ fast + host RAM | experts in host RAM, attention in VRAM; PCIe bandwidth is the cost; single-user cache locality is the win | FreeToken (Linux; Windows once measured) · llama.cpp `--n-cpu-moe` | buy RAM, then lanes |
| **UNIFIED** | unified | ≤ working-set ceiling | **no offload**, sparsity only — the MVP measured expert offload at 0.53× decode here because there is no bandwidth cliff to route around | llama.cpp (Metal / Vulkan / ROCm) · MLX | buy unified memory |
| **STREAM-NVME** | any | > RAM | expert paging from NVMe; MoE only; cold start ≈ pool ÷ NVMe GB/s | platform-specific (flash-moe class on Apple Silicon; llama.cpp mmap elsewhere); no shared engine | buy NVMe bandwidth |
| **CPU-ONLY** | no GPU | ≤ host RAM | small MoE on CPU | llama.cpp | buy cores + memory bandwidth |
| **MOBILE** | unified (phone) | ≤ OS-granted budget | NPU / GPU; thermal-bound; ≤ 4 B models | GGUF via llama.cpp, Core ML, LiteRT-LM | — |
| **NONE** | — | cannot meet the task floor | detached mode unavailable on this device for this task class | — | — |

Two classes carry the measured evidence today: **UNIFIED** (the MVP's 12-row sweep on
an M4, 16 GB) and **FIT-FAST** on the node (published vLLM numbers on the RTX PRO
6000). **SPLIT-PCIE** has FreeToken's paper numbers (39 tok/s for a 35B-A3B on an 8 GB
laptop GPU), not ours. **STREAM-NVME**, **CPU-ONLY** and **MOBILE** are unmeasured.

### 7.3 The manifest a class resolves to

```yaml
schema: workbench.manifest/v2
domain: acme-refinery-east
device_class: SPLIT-PCIE
device_fingerprint: sha256:…            # topology + backend + memory sizes, not a serial
mode: detached
engine:
  name: freetoken
  version: 0.1.4
  artifact: sha256:…                    # exact wheel/binary, this platform
  flags: ["--kv-reserve-tokens", "16384", "--max-running-requests", "1"]
  env: { FREETOKEN_DISABLE_JIT: "1" }
  never: ["--moe-cache-auto"]           # T8
model:
  id: qwen3.6-35b-a3b
  quant: fp8
  format: ftw
  artifacts: [{ path: models/qwen3.6-35b-a3b.ftw, sha256: …, size: 35123456789 }]
  sources:                              # any host is acceptable; the hash is the control
    - lan: /bundles/models/…            # preferred
    - internet: https://mirror.example/…   # allowed only when mode == detached
policy:
  task_classes_allowed: [draft, code, calc]   # extract/vision need the node
  execute_local: { runtime: wasi, network: none, timeout_s: 120 }
  egress_allow: [ "127.0.0.1", "mirror.example:443" ]
lease: { ttl_hours: 336, renew_at: "https://node.lan:8443/lease", grace_hours: 24 }
signatures: [ { role: targets, keyid: …, sig: … } ]   # TUF-style roles, §9
```

Everything the profiler and router decide about a device is in this file, signed, and
the client verifies it at every launch. The `never` list is where the survey's traps
become enforcement.

---

## 8. B1 — Admission router, extended with placement

**Built, not installed.** v1's rule stands: one decision per task, never per step;
classification deterministic and explainable, not an LLM call; the model pinned for
the whole plan so the node's prefix cache and a client's expert cache stay warm.

```
  rules pass over file types and verbs
      ↓
  cosine similarity against labelled task exemplars
      ↓
  class ∈ { code · draft · extract · vision · calc }
      ↓
  × device profile (class, grade, mode)
      ↓
  placement ∈ { node · client · split }      # split: retrieve/embed on node, draft on client
      ↓
  model + tool profile + opencode agent, PINNED
      ↓
  audit row: class, placement, reason        # shown in the UI as "why this model, here"
```

Placement rules in v2, all subject to the trust-domain policy:

- **extract** and **vision** always run on the node: they need Docling, the OCR
  model, the corpus and the gate.
- **draft**, **code** and **calc** run wherever the pinned model is: node when
  attached, client when detached.
- **split** is the interesting case and a research question (§17 RQ2): the node
  retrieves and hands passages with span ids to a client-side generation. The gate
  still runs on the node, against the spans *it* served to that session.

The router consults the node's `/v1/stats` for queue depth and KV occupancy (vLLM
exposes Prometheus metrics; FreeToken exposes `GET /v1/stats`) so a heavy task
arriving while the node is saturated **waits rather than evicts**.

---

## 9. Distribution — signed bundles per hardware class

> An installer that works on a laptop with Wi-Fi and fails in a plant room is not an
> installer. (v1.) In v2 it must also work on a laptop *with* Wi-Fi without ever
> trusting it.

### 9.1 The bundle store

The node hosts a bundle store at `/bundles` behind the same `:8443` mTLS endpoint. It
is a **TUF-style repository**: `root`, `targets`, `snapshot`, `timestamp` roles with
offline root keys, so key rotation and revocation work without internet and a client
can detect a rolled-back or frozen repository. Targets are keyed by
`(platform, device_class, engine, model, quant)`.

The build machine (connected) produces:

1. `docker save` of every node image, digests pinned.
2. Python wheels for the node, `--no-index` installable; FreeToken runtime and its
   prebuilt kernel-cache wheel where a SPLIT-PCIE client class is shipped.
3. **The client build per platform** (§6.2), with `@ai-sdk/openai-compatible`,
   ripgrep, the web UI `dist`, and the model catalogue baked in.
4. Model weights as files, pre-converted (FTW for FreeToken, GGUF for llama.cpp,
   safetensors for vLLM), each with a manifest entry and hash. **No Hugging Face repo
   ids anywhere in config.**
5. Fonts, ONNX models, OCR assets — the three that get forgotten.
6. A `models.dev` mirror, best-effort.
7. `install.sh` / `install.ps1` that loads, verifies, and **refuses to report success
   until the node's denial log has an entry in it** (node) or until the client's
   offline check has passed against the installed egress policy (client).

### 9.2 The two channels

| | LAN channel | Internet channel (detached only) |
|---|---|---|
| Client binary, config, manifests, org templates, any exported slice | **only here** | never |
| Public model weights | here, preferred | allowed: the manifest already holds the hash; the client accepts the bytes only if they match |
| Lease renewal | here | optional, org policy: a control-plane-only endpoint that carries no data and no inference |

The internet channel exists so a 35 GB download does not have to happen on the LAN
before a trip. It is safe because **the manifest is the control, not the channel**:
weights are public, the hash is signed, and the client's egress allowlist opens only
for the pinned host, only while the download runs.

### 9.3 Licensing for redistribution

v1 tracked licences for *use*; v2 *redistributes* weights inside an organisation.
Apache-2.0 (Qwen3-series, gpt-oss) and MIT (DeepSeek) are unproblematic; Llama and
Gemma terms carry conditions and are excluded from shipped manifests unless legal
signs them off. This is a checklist item on every model row.

---

## 10. B6 — Lease, revocation, attestation, tamper evidence

**Built, not installed.** This is the subsystem the pivot creates. It answers the
concern that started the pivot: *when a device is no longer inside the trusted
environment, access can be withdrawn, and tampering can be detected when it returns.*

### 10.1 Threat model, stated

| Threat | v2 response | What the org cannot get |
|---|---|---|
| Lost or stolen client device | lease expiry seals org data; device cert revoked at the node; nothing of the corpus on the device by default | cannot un-leak public weights, and need not |
| Departing employee with a detached bundle | same; node refuses renewal; exported slices are encrypted to a device key the lease governs | cannot stop a personal copy of a public model |
| User disables the client's egress policy on an unmanaged machine | client detects and reports (grade drops); node serves only what the policy allows that grade; everything served is logged | cannot *prevent* on hardware the org does not control |
| Modified client binary, config, prompt, or model | manifest signatures verified at every launch; hashes of what ran are in every audit row; mismatch → refuse to start, report on rejoin | a fully forged client on an unmanaged device can lie; TPM/SE attestation closes this on managed devices |
| Forged or trimmed local audit log | hash chain anchored to the node; entries signed by the device key; continuity checked on rejoin | a device that never rejoins produces no evidence — that absence is itself logged |
| Prompt-injected agent exfiltrating corpus text | node-side: the client's allowlist is `{node}`, the node's is `{}`; the node logs what it served; sandbox has no network | on an unmanaged client, an active attacker with local admin can bypass the allowlist |

The honest one-line version: **on hardware the organisation controls, violations are
prevented; on hardware it does not, they are made visible.** The trust-domain policy
lets the org choose which data classes it is willing to make merely visible.

### 10.2 Device identity and keys

| Platform | Device key | Attestation available | Grade cap (§11) |
|---|---|---|---|
| Linux (node or client) | TPM 2.0 key; Keylime agent | **A**: TPM quote verified against locally hosted vendor keys, measured boot | A |
| Windows | TPM 2.0 via Platform Crypto Provider | A-class technically; our own attestation agent against `TBS`, or the org's MDM/Intune | A managed · C unmanaged |
| macOS (Apple Silicon) | Secure Enclave key | **B**: Managed Device Attestation — MDM + Apple servers, one per 7 days | B managed · C unmanaged |
| iOS / Android | Secure Enclave / StrongBox | **B**: App Attest / Play Integrity via vendor servers | B |
| Any, no hardware key | software key in the OS keystore | **C**: self-report signed by a software key | C |

Grades A and B put the device key out of reach of a compromised OS; grade C does
not. The node verifies chains offline where the vendor publishes a root (Apple's
Enterprise Attestation Root CA, TPM manufacturer EK roots bundled into Keylime's
registrar), and needs the *device*, not the node, to have reached the vendor at
attestation time.

### 10.3 The lease

- **Issued** by the control plane on enrolment, bound to the device key and the
  manifest hash; TTL 24 h attached, 14–30 days detached (org policy); a grace window
  after which the client seals.
- **Renewed** by mTLS to the LAN control-plane address, which is not routable from
  outside; optionally over the internet to a control-plane-only endpoint if the org
  enables it (§9.2). Being "on the trusted network" is proven by reaching that
  endpoint with the device cert, never by an IP range.
- **On expiry** the client wipes the data-encryption key for any exported slice,
  refuses to load org config, templates and manifests, and keeps running only what is
  public: the bare harness against public weights, with the sovereignty page showing
  *unleased*. Revoking the device cert at the node makes the next renewal fail; that is
  the whole revocation procedure.

### 10.4 Tamper evidence

1. **Signed manifests** (§7.3, §9.1). The client verifies the binary, config, prompts,
   templates and weights against the manifest at every launch. Weights are hashed in
   full at install and sample-verified (header + random chunks) at launch, full
   re-hash on a schedule — a 35 GB SHA-256 on every start is not acceptable UX.
2. **Hash-chained audit log** on the client: every tool call, model call, gate
   result and policy check appends `H(prev ‖ entry)`, signed by the device key.
3. **Anchoring.** On every sync the node records the client's chain head. On rejoin
   the client uploads the segment since the last anchor; the node checks continuity
   from its recorded head. A fork, a gap, or a segment that does not start at the
   anchor is flagged as a **tamper event** on the sovereignty page and blocks
   re-attachment until an operator clears it.
4. **Measured state** where the platform allows (grade A/B): the attestation carries
   OS version, secure-boot state and, on Linux, the PCRs Keylime watches, so a client
   that was rooted while away fails attestation on return.

Where the survey's `qm` reference paused every tool call for a human, v2 records
every tool call for a chain. The two are compatible: `permission: ask` is still the
gate in front of `execute`.

---

## 11. B4 — Egress proof, now a grade per device

**Built, not installed.** v1's proof was one thing: a non-empty denial log on the
box. v2 keeps that for the node and adds a per-device **sovereignty grade** that the
policy consumes and the UI shows.

| Grade | Where | Enforcement | Attribution | Evidence |
|---|---|---|---|---|
| **A** | node; managed Linux client | `nftables` drop (node) / reject (client), kernel-level | Tetragon per-process | denial log, TPM-attested config |
| **A/B** | managed Windows client | Windows Filtering Platform rule set installed as a service, per-program outbound block-by-default | WFP per-process | denial events → chained log; TPM (A) or MDM (B) attests the rule set is present |
| **B** | managed macOS client | `pf` for the allowlist **plus** a Network Extension content filter for per-process attribution (requires the signed app + system-extension entitlement) | NE per-process | denial events → chained log; MDA attests the OS, not the rules — hence B |
| **C** | unmanaged client, any OS | the same allowlist, installed by the client, **defeatable by the machine's owner** | as above where present | self-reported; the node treats it as untrusted |
| **D** | detached, unmanaged | as C, with manifest-pinned download hosts allowed while a download runs | — | chained log only, anchored on rejoin |

The node's **Sovereignty page** keeps v1's live tail and its **Test egress** button,
and adds a device roster: every enrolled device, its grade, its lease state, its last
anchor, and any tamper events. The provocative demo moves from "watch the box refuse
a packet" to "watch this laptop's *own* policy refuse a packet, and watch the node
record that it did."

> An empty packet capture proves nothing happened for five minutes.
> A non-empty denial log proves nothing can. (v1.)
> A denial log on every device, chained and anchored, proves *where* nothing can — and
> is honest about where it can only be seen.

---

## 12. B2 — Sandbox: remote by default, local by exception

**Built, not installed.** The E2B and Daytona findings (T1, T6) stand; gVisor
`runsc` is still the hardened sandbox — on the node.

### 12.1 Two tools, not one

| Tool | Where it runs | Isolation | Default | When |
|---|---|---|---|---|
| `execute_remote` | node, one gVisor container per session | `--runtime=runsc --network=none --read-only --tmpfs /work --cap-drop=ALL`, seccomp profile, wall-clock timeout | **on** (attached) | the reference task; tests against a staged copy of a local repo; anything that needs the Python data/deliverable stack |
| `execute_local` | client | per platform, §12.2 | **off**; enabled by policy per grade | detached mode; attached coding where round-tripping a workspace is impractical |

Files reach `execute_remote` through an **explicit staged directory** synced from
the client's workspace, never a mount of anything real. Results come back the same
way. `permission: { bash: "ask" }` in the harness is the human gate in front of both
tools.

### 12.2 `execute_local` per platform

| Platform | Runtime | Network | What runs | Trust note |
|---|---|---|---|---|
| any | **wasmtime (WASI)** | none by construction | pure-Python scripts (CPython WASI, tier 2), Rust/C/Go compiled to wasm, shell-like utilities | the only isolation that is identical on every platform; no numpy/pandas/docx (no wheel ecosystem yet) |
| Linux | gVisor or a rootless container | `--network=none` | anything | same as the node |
| macOS | Apple Virtualization framework micro-VM (Linux guest) | no NIC attached | anything | a VM per session is heavier than gVisor; acceptable for coding tasks |
| Windows | WSL2 distro with no network, or Windows Sandbox | none | anything | WSL2 gives GPU passthrough for CUDA when the org wants FreeToken on Windows |
| mobile | — | — | — | not offered |

The engine (§7) is never inside the sandbox; the sandbox is for what the agent
*writes and runs*, not for inference.

**The wall-clock timeout is not optional**, on both tools. T8 (engine) and #38723
(harness) both fail by waiting; a deadline is what turns that into an error.

---

## 13. Data plane and B3 — the provenance gate

Unchanged from v1 in substance, relocated in one respect: **the retrieved set is
recorded per session by the node.** Because the loop now runs on the client, the gate
must not trust the client's account of what it retrieved. It checks claims against
the spans the node itself served to that session.

```
scanned PDF ─► Docling + PaddleOCR-VL ─► chunk + embed (bge-m3, CPU)
                       │                          │
                  SPAN IDS MINTED             pgvector + tsvector
                       └──────────────────────────┘
                                   ▼
                    hybrid retrieval + cross-encoder rerank
                                   ▼
                  TOP-K PASSAGES, EACH CARRYING ITS SPAN ID   ──► recorded: session ↔ spans served
                                   ▼
                    client drafts; submits claims + span refs
                                   ▼
                  B3 GATE on the node: every numeric claim, equipment tag and date
                  must resolve to a span id IN THE SERVED SET and the value must
                  appear in that span's text; unresolvable → STRIPPED and reported
                                   ▼
                    docxtpl on the org's template ─► .docx, fully cited
```

The gate is ordinary code, not a model; that is why it can be trusted, and why it can
sit on the node while the loop sits on a laptop. The span id is still the load-bearing
object: minted once at OCR, verified once at the gate, carried in between. Remove that
thread and the pipeline still produces a document — one nobody can check.

**Presidio** flags identifiers at ingestion, not at answer time. **Permission-aware
retrieval is still solved by topology**: one corpus per node, one node per domain,
and the policy says which grades may retrieve which data classes.

---

## 14. Detached mode — what it is and is not

Detached mode is where the pivot's hard problems live, so its scope is set narrowly
and deliberately.

**Default: the workbench without the instrument.**

- Local engine chosen by class; local files; `read`/`edit`; `execute_local` under
  the platform's isolation; the harness's own session history.
- **No team corpus.** The node's corpus does not leave the node. Revocation therefore
  covers software, configuration, templates and identity — a tractable problem —
  rather than data on a device the org does not hold.
- No `retrieve`, no `deliver`, no gate: those need Docling, the OCR model, the
  corpus and the org templates, which are node-side.
- Task classes `draft`, `code`, `calc`. `extract` and `vision` are refused with a
  reason.

**Optional: an instrument slice.** An org may export, under lease, a *slice* —
selected documents, already ingested, with their spans, plus the gate code and
templates — encrypted to the device key. That makes B3 work off-site for those
documents alone. It requires the client to carry the deliverable tooling (Python +
docxtpl) and is a later milestone (§18), not v2's default.

**Not in scope:** ingestion of new scanned material off-site (no Docling/OCR on the
client); mobile detached agentic work (T12); STREAM-NVME as a supported tier.

The client's UI says which mode it is in, which lease it holds, and what it cannot
do here and why. A refused capability with a reason beats a degraded one without.

---

## 15. The complete tech stack table

Every rejection traces to a survey finding or a review finding.

| Layer | Choice | Licence | Because | Turned down |
|---|---|---|---|---|
| Node OS | Ubuntu 24.04 LTS | — | driver r580+, CUDA 13; Keylime, gVisor, Tetragon all native | — |
| Node orchestration | Docker + Compose v2 | Apache-2.0 | one host, one domain, no cluster | Kubernetes, k3s |
| Node engine | **vLLM**, resident MoE, FP8 KV | Apache-2.0 | designed for concurrent users; proven on the 96 GB workstation card | FreeToken (single-user locality), Ollama, LM Studio |
| Node vision / OCR | vLLM + PaddleOCR-VL 1.5 | Apache-2.0 | 0.9 B, layout-aware | Qwen3-VL-235B, Tesseract |
| Embed / rerank | bge-m3 + bge-reranker-v2-m3, ONNX on CPU | MIT | frees VRAM for KV | GPU embedders |
| Gateway | LiteLLM, MIT core | MIT | virtual keys, budgets, TPM/RPM; sees no identity | `enterprise/` SSO/audit |
| Identity | control plane: local accounts (argon2id), OIDC-ready, mTLS device certs | — | user *and* device on every row | Keycloak on day one |
| **Client harness** | **our build of opencode** | MIT | headless, MCP-native, `permission` as config, managed settings; **built with the catalogue baked in** | upstream release binaries (#38723); goose; OpenHands |
| Client engines | llama.cpp · MLX · FreeToken | MIT · MIT · Apache-2.0 | one per class; all OpenAI-compatible | Ollama, LM Studio |
| **Hardware profiler (B5)** | built | — | per-class manifests with reasons | engine autoconfig (T8) |
| Sandbox, node | Docker + gVisor `runsc` | Apache-2.0 | kernel-level isolation | E2B (T1), Daytona (T6), Firecracker |
| Sandbox, client | wasmtime (WASI) · Apple Virtualization · WSL2 | Apache-2.0 · — · — | the only portable option, plus native VMs | Docker Desktop (licence, weight) |
| Doc pipeline | Docling | MIT | emits the spans B3 needs | MinerU (T7), unstructured |
| PII | Presidio at ingestion | MIT | flag on entry | NeMo Guardrails in v1 |
| Store, node | PostgreSQL 16 + pgvector + tsvector | PostgreSQL | one ACID store | Qdrant + Redis + Elastic |
| Store, client | SQLite (+ sqlite-vec, FTS5; SQLCipher for exported slices) | public domain / MIT | runs everywhere; encrypted at rest | Postgres on a laptop |
| Queue | Postgres `SKIP LOCKED` | — | ten users do not need a broker | Celery + Redis |
| Deliverables | docxtpl · python-docx · python-pptx · openpyxl | MIT | org templates filled | markdown-to-PDF |
| API | FastAPI + uvicorn | MIT | one language for the ML surface | Node |
| Web / desktop UI | React + Vite + TS + Tailwind + shadcn/ui; opencode's UI for the harness | MIT | plan · corpus · deliverables · provenance · sovereignty · devices | Open WebUI (T2), LibreChat |
| Egress, node | nftables drop + Tetragon | Apache-2.0 | enforcement + attribution | tcpdump |
| Egress, client | WFP rules (Win) · pf + Network Extension (mac) · nftables (Linux) | — | allowlist + attribution where the OS allows | app-level only |
| **Trust agent (B6)** | built; Keylime as the Linux attestation reference | Apache-2.0 | leases, chained log, manifest verification | MDM-only solutions (lock the design to one vendor) |
| Distribution | TUF-style signed repository on the node | Apache-2.0 (reference impls) | offline root keys, rollback and freeze detection | ad-hoc signed tarballs |
| Telemetry | OpenTelemetry → local collector → Postgres | Apache-2.0 | beside the audit log | Logfire, Grafana stack |

---

## 16. Repository layout and service map

```
workbench/
├── node/
│   ├── compose.yaml            9 services, one network, no published ports but :8443
│   ├── services/
│   │   ├── api/                FastAPI: auth, sessions, queue, audit, SSE, /admit, /lease, /bundles
│   │   ├── control/            identity, device registry, leases, policy, TUF repo, anchors
│   │   ├── router/             B1 admission router (task class × device profile → placement)
│   │   ├── tools/              MCP server: retrieve · execute_remote · deliver · stage
│   │   ├── ingest/             Docling + Presidio + chunk + embed
│   │   ├── deliver/            docxtpl / pptx / xlsx + B3 gate (checks against served spans)
│   │   └── sandbox/            B2 gVisor lifecycle + exec protocol
│   ├── web/src/routes/         plan · corpus · deliverables · provenance · sovereignty · devices
│   └── infra/                  nftables.conf · tetragon/ · keylime/ · otel-collector.yaml
├── client/
│   ├── harness/                our opencode build: patches, MODELS_DEV_API_JSON, vendored deps
│   ├── profiler/               B5: measurements → class → manifest request
│   ├── supervisor/             engine lifecycle per manifest; timeouts
│   ├── egress/                 WFP rules · pf + NetworkExtension · nftables; self-check
│   ├── trust/                  B6: device key, lease, attestation (TPM/SE), chained log, sync
│   ├── exec-local/             wasmtime host; Apple VZ / WSL2 adapters
│   ├── ui/                     desktop shell around the harness
│   └── installers/             win/ (.exe) · mac/ (.app, notarised) · linux/
├── bundle/
│   ├── build.sh                connected machine: images, wheels, client builds, weights, TUF sign
│   ├── models-dev/api.json     baked into the client; also served as a mirror
│   └── manifests/              per (platform, class, engine, model, quant)
├── templates/                  the customer's own .docx / .xlsx forms
├── test/offline/               network-isolated CI for node and client (fails on any non-loopback socket)
└── bench/                      week-zero: ft bench bw · decode curves per class · KV probe · N=1,2,4,8
```

### Service map (node)

| Service | Role |
|---|---|
| `api` | the **only** container bound to a host port; FastAPI |
| `control` | identity, leases, policy, device registry, TUF repository |
| `tools` | MCP server the clients attach to |
| `worker` | ingestion; polls the Postgres queue |
| `vllm` | GPU, resident generalist, FP8 KV, `--max-num-seqs` sized in week zero |
| `vllm-vlm` | GPU, PaddleOCR-VL, capped `--gpu-memory-utilization` |
| `litellm` | quota; internal network only |
| `postgres` | the single store |
| `otel` | collector writing traces beside the audit log |

The client is not a service; it is an installed application that connects to `api`
over mTLS and to nothing else.

---

## 17. Research questions

Numbered so the build order can name them. Each states what it decides and how it is
answered.

| # | Question | Decides | Answered by |
|---|---|---|---|
| **RQ1** | What is the smallest model that completes the attached reference task unattended, three of five runs? The MVP brackets it: 30B-A3B passes, dense 4B fails; BFCL puts ≤ 4B multi-turn at ≤ 35 %. | which device classes can be *agents* rather than clients; the MOBILE verdict | the reference task run against Qwen3.6-35B-A3B, a 26B-A4B class MoE, an agent-tuned 8B, and Qwen3-4B, on the node, scored by `oracle.py` |
| **RQ2** | Can the pipeline split at stage boundaries — retrieve on the node, draft on the client — without weakening B3? | the `split` placement; whether detached-with-slice is viable | build `split` for the `draft` class; measure gate strip rate vs node-only |
| **RQ3** | What is the decode/prefill/context curve per hardware class for the shipped model, and where does each class's policy flip? | the manifest per class; the UNIFIED "no offload" rule beyond the M4 | the MVP sweep re-run on SPLIT-PCIE (RTX 4070/5080 + 64 GB), UNIFIED (M4 Pro 48 GB, Strix Halo 128 GB), CPU-ONLY |
| **RQ4** | What is the node's real concurrency at agentic context lengths — KV per token, `--max-num-seqs`, TTFT under N = 1, 2, 4, 8? | the Team profile's user count; the "70B-class" model choice | week-zero bench on the RTX PRO 6000 with gpt-oss-120b and Qwen3.6-35B-A3B |
| **RQ5** | Does the vLLM SM120 path for the chosen quant run without patches at the pinned version? | T9; whether the node ships MXFP4 or FP8 | week zero |
| **RQ6** | What can each platform attest offline, and what does the org accept per grade? | the policy defaults; whether unmanaged clients may attach at all | Keylime on Linux; a TPM quote agent on Windows; MDA on a managed Mac; a written policy table |
| **RQ7** | Is the chained-log anchoring scheme sufficient tamper evidence on a grade-C device, or does it need a hardware-signed log head to mean anything? | B6 design on unmanaged devices | adversarial test: modify the client offline, rejoin, confirm detection |
| **RQ8** | Does WASI cover the `execute_local` tool set the `code` and `calc` classes need, and what falls to native VMs? | the client sandbox matrix | run the coding reference task with WASI-only exec, then with Apple VZ / WSL2 |
| **RQ9** | Does our opencode build make zero non-LLM connections across 100 cold starts on each platform, including the runtime child? | whether the harness is shippable | `test/offline/` per platform, 100 runs, `lsof`/ETW sampling at 100 ms |
| **RQ10** | How do multi-GB weights reach a client with no LAN and no sideloading (iOS), and is mobile attached-only in practice? | mobile scope | enterprise distribution trial; likely answer: attached-only |
| **RQ11** | Which engine, if any, should be supported for STREAM-NVME, and on which platform? | whether the class ships | flash-moe-class on Apple Silicon vs llama.cpp mmap; measure cold start and sustained decode |
| **RQ12** | What is the per-user cost of the Network Extension route on macOS (signing, entitlement, MDM) versus accepting grade C for Macs? | macOS grade | a build with the entitlement; the org's answer on MDM |

---

## 18. Build order and stopping rules

Ordered so that the things that can invalidate the architecture are found in the
first two weeks, and so that v1's proven spine is standing before the new subsystems
are attached to it.

| Week | Theme | Deliverable | RQs closed |
|---|---|---|---|
| **0** | **Measure the node** | `bench/`: KV per token, N = 1…8 at agentic context, SM120 kernel path, VLM co-residency. **No product code.** Picks the node model and confirms vLLM. | RQ4, RQ5 |
| **1** | **The spine + the perimeter** | Compose up: Postgres, LiteLLM, vLLM, FastAPI, control plane, bare UI. **nftables + Tetragon + denial log on day one.** Our opencode build passes `test/offline/` on Linux and makes a first request to the node over mTLS. | RQ9 (Linux) |
| **2** | **Ingestion, retrieval, the gate** | Docling + PaddleOCR-VL, spans minted and stored, hybrid retrieval, `retrieve` and `deliver` as MCP tools, B3 checking against *served* spans. The test: a figure resolves back to a bounding box. | — |
| **3** | **Attached mode end to end** | Windows and macOS clients attach; `execute_remote` with staging; B1 with placement; the reference task completes unattended from a laptop. | RQ1 (node side) |
| **4** | **Trust and grades** | B6: device keys, leases, chained log, anchoring; egress policies on Windows/macOS; Sovereignty page with the device roster; Keylime on Linux. | RQ6, RQ7, RQ12 |
| **5** | **Profiler and detached mode** | B5 on three machines (SPLIT-PCIE, UNIFIED, CPU-ONLY); TUF bundle store; manifests; a laptop leaves the LAN, works detached for a day, returns, anchors. | RQ3, RQ8, RQ9 (all platforms) |
| **6** | **Demo surface and the coding task** | `execute_local`, the coding task in both modes, "why this model, here" everywhere, four demo requirements as one product. | RQ2 |

> ### Stopping rules
>
> **Rule 1 (the instrument).** If by the end of week three the attached reference
> task — scanned report in, cited approval note out — does not complete unattended
> in three of five consecutive runs on the node, cut engineering-drawing
> understanding entirely and ship the document path alone. (v1, unchanged.)
>
> **Rule 2 (the hardware ladder).** If by the end of week five the profiler cannot
> produce a working manifest for **SPLIT-PCIE** and **UNIFIED** on real machines,
> ship attached mode only and defer detached mode. STREAM-NVME, CPU-ONLY and MOBILE
> are never on the critical path.
>
> **Rule 3 (the grade).** If unmanaged clients cannot be brought above grade C, the
> shipped policy defaults to: grade C may attach, may retrieve `internal` data
> classes only, may not run detached. A smaller promise that holds beats a larger one
> that depends on the customer's laptops.
>
> One capability that works without a babysitter beats three that need a rehearsed
> operator.

---

## 19. Known limits and traps

### Of the architecture

- **Enforcement stops at the org's hardware.** On unmanaged devices the system makes
  violations visible, not impossible (§10.1). Every sales conversation should say so
  in the first ten minutes.
- **The node is the instrument.** Detached mode is a workbench unless a slice is
  exported; a customer who wants B3 everywhere is asking for the client to carry
  Docling, OCR and the deliverable stack, which is a later product.
- **"Any hardware" is bounded by Rule 2.** Two client classes ship; four are
  research. The profiler reports NONE honestly rather than trying.
- **Concurrency on the node is KV-bound**, and the shipped model is chosen for its
  KV footprint as much as its quality. Dense 70B on one card is the wrong instinct.

### Of the harness

- **opencode has no single offline switch, and the upstream binary can hang on a
  fetch.** Our build removes the call sites; the offline CI proves it per platform
  and per version. Re-run on every bump.
- **A TypeScript/Bun runtime beside a Python node.** Contained because the harness
  never imports `node/services/`; it speaks HTTP and MCP.

### Of the MVP carried forward

- No expert cache, no PCIe, no sandbox, 2-bit weights, one slot. The measured numbers
  are a floor for UNIFIED, not a forecast for any other class.

### Traps (T1–T8 from the survey; T9–T12 from this review)

| | Trap | Consequence here |
|---|---|---|
| T1 | E2B cannot be air-gapped (needs Cloudflare) | gVisor on the node; WASI/VMs on the client |
| T2 | Open WebUI branding clause above 50 users | UI is built |
| T3 | LiteLLM / Onyx / Langfuse gate SSO, audit, RBAC behind `enterprise/` | identity in our control plane; LiteLLM sees keys only |
| T4 | "what does it do on first run, with no configuration?" | answered per dependency; the client build removes call sites |
| T5 | RouteLLM unmaintained since 2024 | B1 is built |
| T6 | Daytona ships no licence | excluded |
| T7 | MinerU licence thresholds | Docling |
| T8 | FreeToken autoconfig starves KV and hangs silently | `--kv-reserve-tokens` pinned; `--moe-cache-auto` in every manifest's `never` list; client-side timeouts |
| **T9** | vLLM on SM120 (RTX PRO 6000 / RTX 50) needs pinned versions and kernel backends; NVFP4 CUTLASS path broken as of March 2026 | week-zero verification; MXFP4/FP8 preferred; exact version pinned per node profile |
| **T10** | opencode startup can wait forever on `models.dev`; `OPENCODE_OFFLINE` ineffective (v1.18.4) | own build with the catalogue baked in; client egress **rejects**; launch deadline |
| **T11** | Hardware attestation on Apple and mobile needs MDM and the vendor's servers; only TPM 2.0 verifies fully offline | grades, not a boolean; enforcement on the node |
| **T12** | ≤ 4B models score ≤ 35 % on multi-turn tool use; the MVP's dense 4B scored F1 0.0 | mobile is a thin client; RQ1 sets the floor per task class |

---

## 20. Sources consulted for this revision

Verified 22 Sep 2026. v1's survey sources (`docs/problem-definition.html`,
`docs/architecture.html`, `docs/results.html`, `mvp/results/`) are carried forward.

- FreeToken — `github.com/FlashML-org/FreeToken` (Apache-2.0; Windows and Linux
  builds; FTW format; elastic VRAM manager); InfoQ, "FreeToken Unlocks Frontier MoE
  Inference on Consumer Hardware", Aug 2026; PR #399 (hybrid CPU/PCIe backend
  measurements).
- opencode — issue #38723, "`opencode run` intermittently hangs during init"
  (24 Jul 2026, v1.18.4; `OPENCODE_OFFLINE=1` trial); issues #16117, #18233, #18492
  (offline-mode requests); PR #18521 (`OPENCODE_APP_DIST`); forks
  `amitok2/opencode-offline` and `Chetic/opencode-offline` (build with
  `MODELS_DEV_API_JSON`, bundled ripgrep/LSPs, isolated-container tests).
- Node sizing — DatabaseMart, "Pro 6000 vLLM Inference Benchmark" (Jan 2026:
  gpt-oss-120b ~60–65 GB on a single 96 GB card); vLLM issue #34817 (gpt-oss-120b on
  RTX PRO 6000, FP8 KV); Hugging Face discussions on SM120 kernel state
  (`nvidia/Qwen3.5-397B-A17B-NVFP4` #7: Marlin fallback, CUTLASS #3096;
  `poolside/Laguna-S-2.1-NVFP4` #3: vLLM 0.25 on SM120).
- Apple Silicon SSD streaming — `fiveangle/flash-moe` (397B-A17B, 209 GB, 48 GB
  MacBook, 4.4 tok/s with tool calling); `ssd-moe/deepseek-v4-flash-mlx`; Apple,
  "LLM in a flash" (arXiv 2312.11514).
- Attestation — Apple, "Managed Device Attestation for Apple devices" (deployment
  guide; MDM required; Apple silicon Macs only); WWDC22 session 10143 (seven-day
  rate limit; validation order); Keylime (CNCF) README and threat model (TPM 2.0,
  registrar hosts vendor keys, encrypted payload delivery).
- Small-model floor — "TinyLLM: Evaluation and Optimization of Small Language Models
  for Agentic Tasks on Edge Devices" (arXiv 2511.22138: BFCL multi-turn 35.25 %
  Qwen3-4B, 16.88 % Qwen3-1.7B, 1.38 % Qwen3-0.6B, 55.62 % xLAM-2-3b); APIGen-MT
  (arXiv 2504.03601); LiquidAI, "Deploy local agents everywhere with LFM2.5-2.6B"
  (Aug 2026); BFCL v4 (Apr 2026).
- WASI — Brett Cannon, "State of WASI support for CPython: March 2026" (PEP 816
  accepted; no sockets, no wheel tag yet); CPython `Platforms/WASI` (tier 2).

*Architecture spec v2. Every figure for the node is an estimate pending week-zero
measurement; every MVP figure is read from a v1 measurement file; every claim marked
"reported" is the cited project's own number, not ours.*
