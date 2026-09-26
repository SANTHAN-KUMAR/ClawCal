# ClawCal — product architecture v2

*Status: proposed, 2026-09-22. Target: SIH 2026 PS #26117 (MRPL). Supersedes the "locked" 3 September architecture
where the two differ; every difference is listed in §9. Written for the team that
will turn the current codebase into the shipped product. Every path below exists
today unless marked **new**.*

---

## 0. Motto

> **Delegate confidential work to an agent you can audit, on hardware you own,
> and get back documents you can sign.**

Three words carry the product: **delegate** (it is an agent, not a chat box),
**audit** (every number, tool call and network refusal is traceable), **sign**
(the output is a deliverable an engineer puts their name on, so a wrong number is
a liability, not a typo).

## 1. What the author of the problem statement is actually asking for

The PS is SIH 2026 **#26117**, from **Mangalore Refinery and Petrochemicals
Limited (MRPL)**. Its own words carry the intent better than any paraphrase:

> "None of this can go through cloud AI assistants like Claude or Codex because
> the underlying data is confidential … people either do the work manually … or
> they **quietly paste confidential material into public tools anyway**."

> "nothing deployable exists today that industrial users can actually work with
> **the way they use Claude or Codex**."

> "That's the **actual proof** of the sovereign claim, not just a statement of it."

| What they wrote | What they mean | The pain behind it |
|---|---|---|
| "the way they use Claude or Codex" | *the interaction model of an agentic coding assistant: delegate, watch it plan and act, approve, get an artefact, resume* | engineers already use those tools at home; a chat widget will not displace the quiet pasting |
| "not locked to one model … addable later without redesigning" | *procurement buys once; the model market moves monthly* | every vendor demo is welded to one model |
| "approval notes, board presentations, engineering calculations, code for internal tools, review of scanned drawings and inspection reports" | *the six document types of refinery office work; each must come out as a real file* | a summary in a chat box still has to be retyped into the template |
| "scanned PDFs, handwritten notes, engineering drawings, photographs" | *our archive is dirty; the demo PDF is not our PDF* | every assistant they have seen died on a 1998 fax-quality inspection report |
| "calculations with steps shown" | *an engineer signs the calculation, so the steps are the deliverable* | a number without its derivation cannot be checked, and a wrong one propagates into a signed note |
| "manuals, SOPs and past correspondence through a local knowledge base connector" | *ground in our procedures, cite the clause* | generic RAG returns plausible text; the refinery needs the clause number |
| "use a smaller open weight model if 120B class hardware isn't available at the venue" | *the reference machine is a 120B-class server; the venue is a laptop* | the design must scale down honestly and scale up without redesign |
| "logs or a visible network monitor … the actual proof" | *the security officer signs this off, and "trust us" is not evidence* | procurement dies at the security review, not at the feature list |

So the product is a **governed delegation surface**. The model is interchangeable
and the loop is commodity. What is not commodity, and what MRPL cannot buy, is:
(a) the proof that nothing leaves, (b) numbers that carry their origin and their
steps into the signed document, (c) an agent that refuses instead of guessing when
the scan is unreadable, and (d) all of that on whatever GPU the unit owns without
hanging.

Those four are the product. Everything else is plumbing we reuse.

### 1.1 The "Expected Solution" paragraph, as acceptance criteria

The PS names five demonstrations. Each maps to something that must be runnable
by a judge, not narrated:

| PS demonstration | acceptance | exists today |
|---|---|---|
| model auto-selection across ≥ 2 task types | a coding request and a summary request visibly route to different models, with the recorded reason shown | A2 |
| agentic task end to end: scanned inspection report → findings → approval note as Word | the six-act demo, from upload to `.docx` with evidence appendix | A3, A4, A12 |
| coding task run and verified in a sandbox | code written, executed under bubblewrap with no network, output checked by a test the agent wrote | A11 |
| multimodal: image or scanned document understanding | a photographed nameplate or handwritten field note read by the VLM, with a refusal where illegible | A4 (scan); **photo + handwriting need the dirty corpus, §6** |
| no external calls, through logs or a visible network monitor | the sovereignty strip, the self-test button, the attributed denial log, the verified audit chain | A13, A14 |

## 2. Principles that decide every trade-off

1. **A capability the system does not have must say so.** Refusal is the feature.
   Already the house rule; it now becomes an interface contract (§5.3).
2. **Never ask the model for a fact the control plane already holds.** Inject it.
3. **Enforce at the last gate, not in the prompt.** The document generator, the
   calculator and the egress guard are where rules are enforced; prompts are advice.
4. **One core, thin clients.** Web, terminal and API share one headless runtime.
   No client owns state (this is the Codex / Claude Code shape).
5. **Measured, not assumed.** Any number the scheduler or the UI shows about
   performance carries its basis (measured / calibrated / prior) or is not shown.
6. **The optimisation layer is a servant.** It exists so the product does not
   hang or thrash on one GPU. It is never the pitch, and it is never on the
   critical path of a feature.

## 3. System shape

```
clients        ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
               │ web workbench│  │ clawcal (CLI)│  │ REST + SSE   │   thin, stateless
               └──────┬───────┘  └──────┬───────┘  └──────┬───────┘
                      └─────────────────┼─────────────────┘
core                       ┌────────────▼────────────┐
  RUN                      │  session + task runtime │  api/app.py, runtime/scheduler.py
                           │  admission · residency  │  runtime/residency.py, router.py
                           │  preemption · quotas    │
                           ├─────────────────────────┤
  agent                    │  governed ReAct harness │  agent/harness.py, agent/prompts.py
                           │  checkpoint · compaction│
                           ├─────────────────────────┤
  PROVE                    │  evidence + provenance  │  evidence/provenance.py, tools/calculator.py
                           │  number rule · classes  │  deliverables/*, tools/docgen.py
                           ├─────────────────────────┤
  knowledge / perception   │  ingest · OCR · VLM     │  knowledge/*, drawings/*
                           │  retrieve · extract     │
                           ├─────────────────────────┤
  CONTAIN                  │  tool policy · approval │  policy/*, tools/sandbox.py, audit.py
                           │  sandbox · egress · audit│
                           ├─────────────────────────┤
  gateway                  │  model gateway          │  gateway/* (ollama, openai-compat, llamacpp)
                           └────────────┬────────────┘
backends                       Ollama · llama.cpp server · any /v1 endpoint
```

Nothing above is new. The changes in v2 are in the **clients** row, in the
contracts between rows, and in the hardening of the perception row for dirty
inputs.

### 3.1 The control plane

The locked architecture names a "Sovereign Control Plane" as the product's
centre. The code has one, but it is **implicit**: seven authorities, each real,
each in its own module, with no shared boundary, no shared identity model and no
uniform record of the decisions they make. v2 makes it explicit without moving
the code that works.

**Definition.** The control plane is the set of components that *decide* rather
than *do*. It never touches a model, a document or a socket itself. It answers
seven questions, and every answer is a persisted, attributable, auditable record:

| authority | question it answers | lives in today | state it owns |
|---|---|---|---|
| admission | may this task run now, in which slot, at what context budget | `runtime/scheduler.py::evaluate` | `tasks`, `task_events`, `checkpoints` |
| residency | which model is resident, what must be evicted, at what cost | `runtime/residency.py` | `residency_log`, `model_profiles`, `hardware_profile` |
| routing | which model serves this task, and why | `router.py::select_model` | recorded on the task row (`selected_model`, reason) |
| registry | what models exist, what they can do, whether the backend actually serves them | `gateway/registry.py`, `gateway.sync_registry` | `model_registry`, `model_health` |
| tool policy | may this tool call happen, does a human have to approve it | `policy/tool_policy.py`, `tools/approval.py` | `tool_calls`, `approvals` |
| evidence | is this claim established, derived, interpreted, or unsupported | `evidence/provenance.py`, `tools/calculator.py` | `evidence`, `claims`, `calculations` |
| sovereignty | did anything try to leave, and can the log be trusted | `policy/egress.py`, `audit.py` | `network_events`, `audit_log`, `audit_anchor` |

The data plane (gateway, harness, tools, knowledge, drawings, deliverables) does
the work and is *told* what it may do. That split already holds in the code: the
harness runs only inside a granted slot, every tool call passes `ToolPolicy`,
the document generator asks the evidence layer before it writes. What is missing
is the layer that makes the seven authorities one plane.

**What is missing, and what B1 adds** (`backend/sovereign/control/`, **new**):

1. **Identity.** Every actor today is the string `"operator"`. The plane needs a
   principal (`user`, `department`, `role`) on every task, approval and audit
   row, from a pluggable source: a local user table first, the refinery's LDAP or
   AD later. No approval is meaningful until the record says *who* approved.
   Quotas (`max_concurrent_per_user`) become real once the owner is real.
2. **Policy per session, not per process.** `ToolPolicy.set_mode` is global.
   The permission mode (§5.2) is a property of the session, evaluated with the
   principal, the tool's risk class and the session's working set.
3. **A uniform decision record.** Each authority already writes *something*, in
   its own shape. One `decisions` table — `(authority, subject, outcome, reason,
   basis, principal, ts, prev_hash)` — that every authority writes to, chained
   into the audit log. This is what the transcript (§5.1) and the sovereignty
   report (§7) read from, and it is what a reviewer asks for: "show me every
   decision this system made about my task".
4. **An administration surface.** Registry rows, policy modes, quotas and the
   egress state are edited today by SQL, environment variables or a shell script.
   They become `/api/admin/*` routes behind the `admin` role, and every change is
   itself a decision row. Adding a model stays "a registry row, not a code
   change", but through the plane, with a health check and a recorded basis.
5. **One boundary in code.** `control/__init__.py` exposes the seven authorities
   as one façade. The API layer calls the façade, never a module directly. The
   test that enforces it is a grep: no import of `runtime`, `router`, `policy`
   or `evidence` outside `control/` and the modules themselves.

**Invariants the plane guarantees**, and tests that hold them:

- No work runs without an admission record. (`test_router_scheduler`)
- No tool call executes without a policy record. (`test_sovereignty`)
- No number reaches a deliverable without an evidence class. (`test_provenance`)
- No socket opens outside loopback without a denial record. (`test_sovereignty`)
- No decision row can be removed or truncated without the audit anchor failing. (D-13)
- No performance figure is shown without its basis. (**new**, B5)

**What the plane is not.** It is not Kubernetes, not a multi-node scheduler and
not a general policy engine. It governs one server, one GPU and a small fixed
tool surface. That narrowness is what makes it auditable.

## 4. Where the optimisation engines belong — and where they do not

Two engines exist: the **residency-aware scheduler** in this repo, and the
**moe-phone performance-model discipline** (device probing, measured/calibrated/
prior bases, refusal when uncalibrated, pre-registered predictions).

| Engine | Used for | Not used for |
|---|---|---|
| Residency scheduler (`runtime/`, `router.py`) | keeping the product responsive on one GPU: admission, no-hang KV budget, batching queued work by model, preempting a batch job for an urgent question | any user-visible feature; the pitch; any claim beyond "fewer loads, same answer quality" |
| moe-phone performance model (**new**, `runtime/perfmodel.py`) | giving every latency number the scheduler and the UI use a *basis*; refusing to promise a wait time when uncalibrated; probing the filesystem the weights live on | streaming experts from disk on this hardware (every registry model fits RAM+VRAM; the streaming niche is 80B+ models, which this machine cannot store) |

Concretely, the performance model is applied in three places and nowhere else:

1. **`ModelCard` gains provenance.** `cold_load_s`, `decode_tps`, `residency_mb`
   each carry `basis ∈ {measured, calibrated, prior}` and a `samples` count. The
   size-derived fallback in `registry.py` becomes `basis=prior` and is displayed
   as a range, never a point. A profile row with an impossible value (the current
   53 s / 0 MB vision-model row) is flagged invalid and ignored by admission.
2. **The admission decision reports its basis.** "Estimated wait 40 s (measured,
   32 samples)" or "wait unknown, first run of this model". The client renders it.
3. **The hardware probe records the model directory's filesystem** and warns when
   it is FUSE. That is one line and it is the difference between a measured claim
   and a spec-sheet claim.

**Scaling up to the reference hardware.** The PS assumes a 120B-class server
and allows a smaller model at the venue. That is a registry row, not a redesign:
on a server with the RAM, `gpt-oss-120b` is added exactly as `gpt-oss-20b` is
today, served part-offloaded by llama.cpp's CPU-MoE path through the gateway's
`llamacpp` seat, and the residency scheduler prices its load cost like any other
card. The demo laptop and the refinery server run the same code with different
registry rows, which is the honest answer to "use a smaller model at the venue".

The streaming engine (BigMoeOnEdge fork) is **not** wired in. The gateway's
OpenAI-compatible seat stays open for it, exactly as it does for FreeToken, and
the README says why it is not used.

## 5. The interface: the Codex / Claude Code contract, sovereign

The user experience the author wants is the one they know from agentic coding
tools: describe a task, watch the agent plan and act, approve the dangerous
steps, get an artefact, resume later. The workbench already has the conversation
surface; v2 makes the contract explicit and gives it a second client.

### 5.1 Headless core, one API

All clients speak the existing REST + SSE surface (`api/app.py`). Two additions:

- **`POST /api/sessions`** (**new**): a session is a conversation with a working
  set (attached documents, produced artefacts, approvals) and a permission mode.
  Today's `conversations` becomes the session store; tasks belong to sessions.
- **`GET /api/sessions/{id}/transcript`** (**new**): the full ordered trajectory —
  plan, tool calls, observations, refusals, approvals, evidence classes — as the
  single source the web client, the CLI and the audit export all render from.

### 5.2 Permission modes

Three modes, set per session and shown in the client at all times:

| mode | reads | calculator / search | code, docgen, file writes | approval gate |
|---|---|---|---|---|
| **review** (default) | auto | auto | asks before each | human |
| **trusted** | auto | auto | auto, logged | human only for `approval` tool |
| **locked** | auto | auto | refused | n/a |

This replaces the current global `policy/mode` with a per-session setting and
maps directly onto `tools/approval.py` and `policy/tool_policy.py`. It is the
"strict / standard / permissive" pattern the author will recognise.

### 5.3 The refusal contract

Every response, tool result and artefact carries one of four outcomes, rendered
identically in every client:

```
ESTABLISHED   Class A/B evidence, safe to put in a signed document
INTERPRETED   Class C, labelled as interpretation, engineer must confirm
CANNOT DETERMINE   the evidence does not establish it; what would
DEGRADED      the capability ran in a reduced mode (raster-only, no sandbox netns, OCR < threshold)
```

`DEGRADED` is the new one. The sandbox already reports it; OCR, drawings and the
vision path will too. A user must never learn from the *quality* of an answer
that the system was running in a reduced mode.

### 5.4 The terminal client (**new**, `clients/clawcal/`)

A ~600-line Python CLI over the same API:

```
clawcal                      # interactive session in the terminal
clawcal "summarise the attached IR" --attach IR-0731.pdf --mode review
clawcal resume <session>     # pick up a paused or preempted session
clawcal approve <id>         # act on a pending approval from anywhere
clawcal status               # queue, resident models, egress denials, audit head
```

Slash commands inside a session mirror the tool surface: `/attach`, `/evidence`,
`/approve`, `/mode`, `/artefacts`, `/trace`. The CLI exists because (a) the
author asked for the Codex / Claude Code shape, (b) an air-gapped server often has
no browser, and (c) it proves the core is headless. It is not a second product;
it renders the same transcript.

### 5.5 The web workbench

Keep the conversation-first SPA. Three changes:

- **Evidence inline.** The class badge sits next to every number in the answer
  (today it lives in a tab). Click → page image with the region highlighted.
- **Sovereignty panel always visible.** Egress policy state, denial count, audit
  head, backend health — a persistent strip, not a page. One button runs
  `/api/sovereignty/selftest` and animates the denials arriving.
- **Basis on every wait estimate** (§4.2).

## 6. Hardening for real-world dirty inputs

This is where "polished product" is decided. The existing real-world drawing
work (`docs/drawings-real-world.md`) shows the pattern: every mechanism that
scored 1.00 on the seeded corpus failed on the first real sheet, and was fixed by
a general mechanism (tag grammar as generator, table exclusion, register
snapping), not by tuning to that sheet. v2 applies the same rule to every input
path, with a **held-out dirty corpus** the code never trains on.

### 6.1 The dirty corpus (**new**, `corpus/dirty/`, not committed, fetched)

| class | examples | what it tests |
|---|---|---|
| scans | 200 dpi fax-quality reports, skewed, stamped, hand-annotated | OCR confidence gating, region refs surviving skew |
| photographs | phone photos of nameplates, gauges, printed pages | VLM path, glare, perspective |
| handwriting | field notes, filled forms | refusal thresholds, not invention |
| spreadsheets | merged headers, multi-sheet registers, units in headers | value extraction with units |
| CAD exports | vector P&IDs with title blocks, legends, revision tables | table exclusion, lexicon from vector text |
| rasterised drawings | the King County sheet at three resolutions | inventory-only refusal on connectivity |
| adversarial | documents containing instructions to the agent | injection framing rule |

Acceptance: every class has a recorded score and a recorded refusal rate. A
change that raises a score by lowering a refusal is rejected.

### 6.2 Perception rules

- **OCR confidence is calibrated per input class**, not a global threshold
  (build-report defect: confidence means different things on different inputs).
  Below the class threshold the value is `CANNOT DETERMINE`, with the region
  image attached so the engineer can read it themselves.
- **Every extracted value carries units, page and region**, and
  `extract_document_values` is the only path by which a critical engineering
  value reaches the model (D-09). Prose retrieval never supplies a number to the
  calculator.
- **The vision model is never routed a reporting task** (build-report defect); it
  describes regions, the text model reports.
- **Images are tiled to the VLM's real budget** from the registry, never sent
  whole.
- **Raster drawings emit inventory and zero CONFIRMED edges.** This stays a
  refusal, not a roadmap item.

### 6.3 Knowledge rules

- Lexicons (tag registers, equipment lists) are seeded **only from documents the
  organisation wrote or from vector CAD text**, never from prior OCR (the feedback
  loop found in the drawing work).
- Ingest is idempotent and a failed ingest never poisons its hash.
- Native `.docx/.xlsx/.pptx` ingest, with two-column tables read as label: value.

### 6.4 Agent rules

- **Attachment is authoritative.** "The attached document" resolves only to the
  session's working set; if empty, refuse and ask (the worst defect found).
- **Document inventory is injected**, never recalled.
- **Compaction keeps shape** (which tool ran, headline result), and the KV budget
  is the measured one per model.
- **Loop detection**: an identical tool call twice in a row ends the step with a
  `CANNOT DETERMINE` and the reason, instead of burning the step budget.
- **Spreadsheet work is a tool, not only a deliverable** (**new**,
  `tools/spreadsheet.py`). The PS names "spreadsheet work" beside file I/O and
  code execution. Today `.xlsx` is ingested and generated; the agent cannot read a
  named range, add a column or evaluate a formula on an attached workbook. The
  tool exposes `read_range`, `write_range`, `add_sheet`, `recalculate` over
  openpyxl, every written cell carries its evidence class, and formulas are
  written as formulas so the engineer can audit them in Excel.

### 6.5 Deliverable rules

- The generator refuses Class D values, twice, then marks gaps (D-08).
- Templates are the organisation's own, loaded from `SOVEREIGN_DATA_DIR`,
  with a template validator that reports which placeholders the template
  exposes, so a new template is a file drop, not a code change.
- Every artefact embeds its evidence appendix and its audit-head hash.

## 7. Sovereignty as a product feature

Five layers already exist: nftables default-deny, in-process guard on
`connect` and `getaddrinfo`, sandbox network namespace, no-CDN UI, hash-chained
and anchored audit. v2 changes only how they are *experienced*:

1. **The host policy is applied by the installer** (`ops/install.sh`, **new**)
   and its state is shown in the strip. Not applied is a red state, not a
   footnote.
2. **The self-test is a button and a CLI command**, and its output is a signed
   report (`artifacts/sovereignty-report-<date>.pdf`) the security officer can
   file. This is the artefact that gets the product through procurement.
3. **Audit export** is one command and verifies offline.

## 8. Build order

Ordered by dependency, not by calendar. Each step ends with a gate a judge or a
customer can run. No step adds a capability that cannot refuse.

| step | builds | gate |
|---|---|---|
| **B0 repair** | OpenCV 5 raster fix; doc figures reconciled; profile-row validity flag | `make test` green; every figure in the docs traces to an artifact |
| **B1 control plane made explicit** | `control/` package (§3.1): identity, per-session policy, uniform decision records, admin routes | every authority writes a `decision` row; no `"operator"` string literals remain |
| **B2 contract** | sessions + transcript API; permission modes per session; four-outcome refusal contract across all tools; spreadsheet tool | the transcript renders the full six-act demo; every tool result carries an outcome |
| **B3 clients** | terminal client; inline evidence badges; sovereignty strip and self-test button | the same task from CLI and web produces identical transcripts |
| **B4 dirty corpus** | `corpus/dirty` fetcher (open datasets only, as the PS permits); per-class OCR calibration; handwriting and photograph paths with refusal thresholds; loop detection; template validator | every dirty class has a score and a refusal rate recorded; no class regresses |
| **B5 basis** | perfmodel provenance on `ModelCard`; wait estimates with basis; filesystem probe | admission decisions show their basis; an uncalibrated model shows "unknown", not a number |
| **B6 install** | `ops/install.sh`, systemd unit, egress applied at install, signed sovereignty report | clean Fedora/Ubuntu VM to a working product with a red/green strip, one command |

## 9. Deviations from the locked architecture

| locked says | v2 does | why |
|---|---|---|
| reuse goose/OpenHands | keep the 500-line governed harness | admission, checkpointing and per-call policy cannot be retrofitted; D-02 |
| no parallel CLI products | one CLI **over the same API** | the author's interaction model is a terminal agent; the rule was against *parallel state*, and the CLI has none |
| P&ID connectivity is Phase 4 | vector connectivity shipped; raster refuses | it works on vector, and refusing on raster is honest |
| FreeToken optional backend | seat kept open, not used; same for the streaming engine | no host RAM for an expert cache; every model here fits without streaming |
| PostgreSQL + pgvector | SQLite behind a `VectorIndex` seam | zero services on an air-gapped box; swap when the corpus passes ~10⁵ chunks |

## 10. Non-goals, stated so nobody builds them

- Multi-GPU or multi-node scheduling.
- Expert streaming from disk on this hardware class.
- A general-purpose agent framework or plugin system.
- Raster P&ID connectivity.
- Fine-tuning any model. The evidence layer, not the weights, is where trust lives.
- A mobile client. The phone work stays a separate research line.
