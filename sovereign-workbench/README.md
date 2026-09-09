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

## Quick start

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
