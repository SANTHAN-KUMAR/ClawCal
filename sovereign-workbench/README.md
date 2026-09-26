# Sovereign On-Premise AI Workbench

A self-hosted, air-gapped agentic AI workbench for confidential industrial
knowledge work. It runs open-weight models on the organisation's own GPU,
coordinates them as governed workloads, grounds their answers in the
organisation's own documents, and produces real deliverables — while proving,
through a tamper-evident denial log, that nothing left the building.

Built for the problem statement *"Sovereign On-Premise Agentic AI Workbench using
Open-Weight Multimodal LLMs for Confidential Industrial Work"*.

---

## What it does

**RUN** — decides which agent may execute, with which model, under which resource
budget and priority.
**PROVE** — requires important output to be sourced, calculated, explicitly
interpreted, or refused.
**CONTAIN** — keeps agents and their tools inside the sovereign boundary, and
records every attempt to leave.

The load-bearing custom contribution is the **compute** layer: on one mid-range
GPU the scarce resources are VRAM residency and wall-clock, so model selection is
treated as a memory-eviction decision made once per task rather than a
capability lookup made per step. Measured, that is 14 model loads reduced to 4
across a mixed 16-task shift — 141 seconds of loading avoided, at marginally
better capability fit.

---

## Version 2 (ClawCal)

`docs/ARCHITECTURE-v2.md` is the design; this is what it became.

**A delegation surface, not a chat box.** Work happens in *sessions*: a
conversation with a working set of attached documents, the artefacts it has
produced, its approvals, and a **permission mode**. `review` (the default) asks
before every write, code run or deliverable. `trusted` runs them and logs them.
`locked` permits reads only. The mode is per session and takes effect at the
running task's next tool call. The same session can be driven from the web
workbench or from the terminal:

```bash
./clawcal "summarise the attached report" --attach IR-0731.pdf --mode review
./clawcal resume <session>        ./clawcal approve <id>        ./clawcal status
```

Both render one server-side transcript
(`GET /api/sessions/{id}/transcript`), so a task looks the same in either.

**An explicit control plane.** Eight authorities (admission, residency,
routing, registry, tool policy, evidence, sovereignty, trust) each write to one
`decisions` table. It is hash-chained and cross-referenced into the audit log,
so deleting or truncating a decision is detected at both ends. Every task,
approval, decision and audit row names a **principal** with a role. The API
reaches the authorities only through `sovereign.control`, and a test enforces
it.

**The refusal contract.** Every tool result, answer and artefact is
`ESTABLISHED`, `INTERPRETED`, `CANNOT DETERMINE` or `DEGRADED`. A capability
running in a reduced mode is announced, not left for the reader to infer: a
raster drawing, a sandbox without a network namespace, lexical-only retrieval,
an unverified host firewall. Evidence badges sit beside every number in an
answer; a badge opens the source page with the cited region highlighted.

**Spreadsheet work** is a tool pair: `spreadsheet_read` and `spreadsheet_edit`.
It reads merged, multi-row headers and units. It writes formulas as formulas to
a session copy (never to the uploaded evidence), and refuses literal numbers no
source supports. On eight real EIA and UK DESNZ workbooks it read 31 of 31
ground-truth cells correctly.

**Measured, not assumed.** Every performance figure carries its basis
(`measured` / `calibrated` / `prior`, with a sample count). A queued task's
wait says "estimated 40 s (measured, 32 samples)" or "unknown — first run".
Impossible profile rows are flagged and ignored.

**Memory safety.** Host RAM is priced before every model load, including loads
made from inside tools, and a backend's own memory refusals are learned. See
`docs/deployment.md` §7 for the OOM that made this necessary.

**Dirty inputs.** `scripts/fetch_dirty_corpus.py` fetches a held-out corpus of
real documents nobody on this project made: FUNSD scans, CORD receipt photos,
IAM handwriting, public spreadsheets, real P&IDs, and injected pages.
`scripts/eval_dirty.py` scores the product's own pipeline on it, per class, as
**quality, refusal rate and confidently-wrong rate**. Results:
`docs/dirty-eval.md`.

**Sovereignty you can file.** The self-test produces a PDF signed with the
appliance's Ed25519 key. The audit export is signed JSONL. Both verify offline:
`./clawcal verify <file>`.

**Production.** `sudo ./ops/install.sh` takes a clean Fedora, Ubuntu or Amazon
Linux host to a running, self-tested appliance. It installs a hardened unit,
token auth, a model set sized to the GPU, and a rollback-guarded egress policy
that keeps SSH, DHCP and NTP working and blocks the cloud metadata endpoint.
Read `docs/deployment.md` before putting it on a network.

## The trust domain (Architecture v2, re-based)

`sovereign-workbench-v2.md` is the design, and `docs/trust-domain.md` describes
what was built. The appliance becomes the **node** of a trust domain. Laptops
and desktops enrol as **member devices**. A policy on the node says what each
device's **grade** may receive.

- **Devices are keys.** Enrolment proves possession of an Ed25519 key. Leases are signed by the node and renewed by the device key. Revoking a device stops its lease at once, and its next renewal fails.
- **Grades come from facts the device can't set:** an admin's "managed" flag, a verified attestation, a passing egress self-check. Grade C (unmanaged) may attach, retrieves public and internal documents only, and may not work detached. A confidential document is invisible to it and refused by name.
- **Every device keeps a hash-chained, signed log**, anchored on the node at each sync. A fork, gap, edited entry or forged signature quarantines the device until an admin clears it with a reason.
- **B5 classifies each machine per model** from the weights file's own GGUF header: FIT-FAST, SPLIT-PCIE, UNIFIED, or research tiers that are reported but never shipped. B1 decides where each task runs: node, client, or split.
- **A TUF-style signed bundle store** serves the client package and per-device manifests, refusing rollback, freeze and forgery.
- **Attached mode:** `clawcal attach "<task>"` runs opencode on the laptop, pinned to the node's `/v1` and its MCP tools (`retrieve`, `extract_values`, `stage`, `execute_remote`, `deliver`). The node's gate checks every figure against the spans *it* served, strips invented ones, and links every kept figure in the `.docx` to its scanned page.
- **Detached work:** with a detached lease (grades A and B), a laptop runs a node-shipped, hash-verified llama.cpp pinned to its real GPU, decides admission itself from its signed manifest, and anchors everything it did when it rejoins. **Instrument slices** carry chosen documents off-site, encrypted to the device and gated by the node's own rules, delivered as `.docx` on rejoin.
- **TPM attestation**, verified on the node offline: EK chain, credential activation, a PCR quote against a baseline. A laptop rooted while away fails on return.
- **The organisation's own opencode build** (`bundle/opencode/`) has its network call sites removed from the binary. With egress open it made 100 of 100 cold starts touching nothing but loopback; upstream reached Cloudflare-hosted services and hung.
- **Operations:** `scripts/trustctl.py` (roster, revoke, slices, bundles, root rotation) and `trustctl health`.

```bash
./clawcal device enrol && ./clawcal device manifest && ./clawcal attach "draft an approval note for IR-2026-0731"
python3 scripts/verify_trust_domain.py --harness   # T1–T17 on real processes (see --help)
python3 scripts/trustctl.py health
```

---

## Quick start

```bash
./ops/install.sh --user            # workstation: venv, models sized to the GPU, service
./clawcal status                   # or open http://127.0.0.1:8794
./clawcal selftest --report        # signed sovereignty report
```

Or by hand:

```bash
# 0. A local inference backend. Ollama is the reference; anything speaking the
#    OpenAI API on loopback works (see "Adding a backend").
ollama serve &
ollama pull qwen3:8b && ollama pull qwen2.5:7b && ollama pull qwen2.5vl:3b
ollama pull gpt-oss-20b          # optional deep reasoner

# 1. Build the synthetic industrial corpus (SOPs, scanned reports, a P&ID).
python3 scripts/seed_corpus.py

# 2. Check what this machine can actually serve.
python3 -m sovereign.server --probe        # run from backend/, or set PYTHONPATH

# 3. Start the workbench.
cd backend && python3 -m sovereign.server
#    → http://127.0.0.1:8794

# 4. Prove it works.
python3 scripts/verify_e2e.py              # 20 acceptance criteria, nothing mocked
python3 scripts/benchmark_scheduling.py    # residency scheduling vs naive routing

# 5. Optional but recommended: host-level default-deny egress (needs root).
sudo ./ops/egress-policy.sh apply
```

---

## Architecture

```
                         WEB WORKBENCH
        chat · files · tasks · evidence · deliverables
        agent trace · resource view · drawings · audit · network
                              │
                    SOVEREIGN CONTROL PLANE
        task manager · model registry · capability router
        tool policy · evidence/provenance · audit · egress policy
                              │
                RESOURCE-GOVERNED AGENT RUNTIME
        admission · priority + aging · concurrency · quotas
        residency manager · checkpoint/resume · health · telemetry
                              │
          ┌───────────────────┼───────────────────┐
     AGENT HARNESS      MODEL GATEWAY        TOOL GATEWAY
     reason/act loop    ollama · vLLM ·      files · search · calculator
     checkpointable     llama.cpp · any      sandbox · documents · drawings
     policy-gated       OpenAI-compatible    approval · egress probe
                              │
                    LOCAL SOVEREIGN DATA PLANE
        OCR/VLM · embeddings · SQLite+FTS5 · evidence store
        templates · sandboxed workspaces · drawing graphs
                              │
                    HOST SECURITY FOUNDATION
        Linux · bubblewrap · nftables default-deny · GPU telemetry
```

Every layer is documented in `docs/decisions.md`, which records what was decided,
why, and what it costs — including the four places this deliberately departs from
the locked architecture.

---

## The compute contribution

On a single mid-range GPU, two models of this class cannot be co-resident with
their KV caches. So a routing decision is really an **eviction** decision, and the
scheduler treats it as one:

* **Measured residency cost.** Cold-load time and VRAM footprint are measured on
  the target machine and fed back after every load, so admission uses real
  numbers rather than published estimates.
* **Residency batching.** Queued work is ordered so tasks needing an
  already-resident model run together.
* **Priority-dependent tolerance.** Reusing a resident model saves seconds but may
  cost capability. Urgent work trades almost none (0.02 fit at CRITICAL); batch
  work trades freely (0.25). This is not cosmetic — a weaker resident model was
  observed conflating a pressure with a wall thickness on an approval note.
* **Minimum dwell.** A freshly-loaded model cannot be evicted for 45 s, which stops
  the scheduler oscillating under an interleaved workload.
* **Priority aging.** A queued task gains priority as it waits, so batch work
  cannot starve.
* **A real KV budget.** Context capacity is computed from each model's actual
  attention geometry (layers × KV heads × head dim). A task that will not fit is
  routed to a model with a cheaper KV cache, or refused with an explanation —
  never queued into the silent stall that is the documented worst failure mode of
  local MoE serving.
* **Preemption at workflow boundaries.** A batch job checkpoints between documents
  and yields to a safety question, then resumes from where it stopped. This needs
  nothing from the inference engine, so a backend change cannot break it.

`scripts/benchmark_scheduling.py` measures naive routing against this policy on
an identical workload and reports loads, seconds and the capability fit actually
delivered — because a scheduler that wins on time by routing everything to the
weakest model has won nothing.

```
                                   loads    load s    wall s  mean fit
naive per-task routing                14     231.5     238.5     0.821
residency-aware scheduling             4      89.9      96.3     0.825
```

---

## Evidence and refusal

Every significant number in generated output is classified:

| | Class | Meaning |
|---|---|---|
| **A** | SOURCE | present in a cited document region |
| **B** | DERIVED | reproducible calculation over Class A/B inputs |
| **C** | INTERPRETATION | a judgement from evidence, labelled as one |
| **D** | UNSUPPORTED | not establishable — refused |

Three properties make this more than a citation feature:

1. **Provenance propagates.** A calculation confers Class B only if *its own
   inputs* are established. Otherwise the calculator becomes a laundering machine
   that converts an invented number into a trusted one — observed happening
   before the check existed.
2. **Enforcement is at generation time.** The approval-note generator *refuses* to
   write a document containing Class D values and names them, rather than
   trusting the prompt. Prompted not to do mental arithmetic, a model did it
   anyway and produced "remaining life > 5.0 years, no action required" when the
   figure was 4.25 years, which under that clause requires escalation.
3. **Critical values are read by pattern, not by model.**
   `extract_document_values` pulls labelled engineering values off the page with
   their page and region, because a 7-8B model asked to read "Nominal Thickness ©
   12.0 mm" off a scan produces 12.4 often enough to matter.

---

## Engineering drawings

The capability the survey calls "the one genuinely unsolved". Two paths, honest
about the difference:

* **Vector** (a CAD-exported PDF) — connectivity is *in the file* as exact line
  geometry, so it is read rather than inferred. Scored against ground truth on
  the corpus P&ID: **1.00 F1 on tags, 1.00 F1 on connectivity.**
* **Raster** (a scan or photograph) — recovers the equipment inventory (0.80 F1 on
  tags) but produces **zero** CONFIRMED connections, and says so.

Every edge carries a status: `CONFIRMED` may be stated as fact, `PROBABLE` must be
labelled as interpretation, `UNRESOLVED` is reported as a gap needing a human.
Instrument signal leads are separated from process flow, so a measurement
connection is never reported as a pipe.

**Measured on real drawings, not just ours.** On a genuine utility P&ID (King
County WW510-P-60003) tag reading scores **F1 0.57**, against 1.00 on the
synthetic corpus drawing — and the same drawing scores 0.27 when supplied at
screenshot resolution instead of its native 2200 px. The binding constraint is
scan resolution, and the pipeline says so rather than guessing. A page of tables
that once produced 437 phantom "symbols" now produces 2. See
[`docs/drawings-real-world.md`](docs/drawings-real-world.md) and
`scripts/benchmark_drawings.py`.

---

## Sovereignty

Five layers, each producing an auditable event:

| Layer | Mechanism |
|---|---|
| 1 | nftables default-DROP outbound with logging (`ops/egress-policy.sh`) |
| 2 | in-process guard on `connect()` **and `getaddrinfo()`**, attributed to a task |
| 3 | agents are granted no network tool at all |
| 4 | sandbox runs in an empty network namespace — no interfaces exist |
| 5 | every refusal enters the hash-chained audit log |

Layer 2 hooks name resolution as well as connection because an external hostname
that fails to resolve — the common case on an isolated host — otherwise raises a
DNS error and never appears in the denial log at all.

The proof is the **non-empty denial record**, not an empty packet capture. The
Sovereignty view has a self-test that attacks the boundary on demand.

---

## Layout

```
backend/sovereign/
  config.py            every path and tunable, in one auditable file
  db.py                SQLite schema; every decision row records its reason
  audit.py             hash-chained append-only log + the live event bus
  hardware.py          capability probe and continuous telemetry
  router.py            task classification + capability/residency selection
  gateway/             backend-neutral inference; harmony/qwen/chat adapters
  runtime/             admission, residency, scheduling, checkpoints, recovery
  agent/               the governed reason/act loop and its prompts
  tools/               calculator, search, extraction, files, sandbox, docgen…
  policy/              tool policy, egress enforcement, injection defence
  evidence/            the four evidence classes and the number rule
  knowledge/           OCR, ingest, embeddings, hybrid retrieval, extraction
  drawings/            vector + raster P&ID understanding and graph assembly
  deliverables/        organisation-templated docx / xlsx / pptx
  workflows/           the runners the scheduler executes
  api/                 FastAPI surface, SSE stream
frontend/              the workbench SPA — no framework, no CDN, no build step
corpus/                synthetic SOPs, scanned reports, a P&ID + ground truth
scripts/               seed_corpus · verify_e2e · benchmark_scheduling
ops/                   nftables egress policy · systemd unit
docs/                  decisions.md · harmony.md
```

---

## Adding a model

A registry row, not a code change:

```python
db.upsert("model_registry", ModelCard(
    name="my-model", backend="ollama", backend_ref="my-model:latest",
    prompt_adapter="chat",              # or "harmony", or "qwen"
    ctx_max=32768, weights_mb=5000, kv_mb_per_1k=60.0,
    caps={"text": 1.0, "reasoning": 0.8, "coding": 0.7, "extraction": 0.8,
          "tool_use": 0.8, "speed": 0.8, "structured": 0.8},
).to_row(), key="name")
```

The router scores it against every task profile immediately. `kv_mb_per_1k` comes
from the model's attention geometry — `2 × layers × kv_heads × head_dim × 2
bytes` — and is what makes its context budget honest.

## Adding a backend

```bash
OPENAI_COMPAT_URL=http://127.0.0.1:8000 \
OPENAI_COMPAT_MODELS=meta-llama/Llama-3.1-8B-Instruct \
python3 -m sovereign.server
```

Anything speaking `/v1/chat/completions` on loopback — vLLM, llama.cpp's server,
LM Studio, FreeToken. Verified by acceptance criterion A15.

---

## Verification

```
$ python3 scripts/verify_e2e.py
 21 passed · 0 failed · 0 skipped

$ python3 -m pytest tests/ -q
 138 passed

$ python3 scripts/smoke_api.py
 All API surfaces responded correctly.
```

Nothing in the acceptance suite is mocked: models are loaded, documents are
OCR'd, code executes in the sandbox, and network connections are genuinely
attempted and genuinely refused. `docs/build-report.md` lists the defects this
testing found — several were invisible from the code and only appeared when
something real was run through it.

---

## Known limits

Stated plainly, because a system that hides these is not trustworthy:

* **Raster drawings give inventory, not connectivity.** Obtain the vector PDF for
  confirmed topology. The system refuses rather than guessing.
* **Instrument balloon text on a rasterised drawing is often illegible** at
  typical scan resolution; those tags are missed rather than invented.
* **Model quality is the residual risk.** The provenance and enforcement layers
  catch wrong *numbers*; they cannot catch every wrong *judgement*. Approval notes
  are drafts for an engineer to sign, and the template says so.
* **A 21B MoE runs part-offloaded on 8 GB** and decodes at roughly 28 tok/s.
  Recorded in the registry rather than hidden.
* **Dense retrieval is O(n) per query.** Fine to ~10^5 chunks; beyond that fill the
  vector-index seam with pgvector or sqlite-vec.
* **Tesseract carries English only** on this host. Add language packs for other
  scripts.
