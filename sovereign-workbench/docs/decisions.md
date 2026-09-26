# Architecture decisions

Each entry records what was decided, why, and what it costs. Where a decision
departs from the locked architecture, that is stated explicitly.

---

## D-01 · SQLite on ext4, not PostgreSQL + pgvector

**Locked architecture says:** "PostgreSQL + pgvector or equivalent".

**Decision:** SQLite in WAL mode, with FTS5 for lexical search and float32 blobs
plus NumPy for dense search, behind a retrieval interface that pgvector can
replace.

**Why:** an air-gapped appliance is deployed by people who cannot apt-get
anything. Every additional service is another thing to install, run, back up and
explain to a security review. The corpus for a departmental deployment is tens of
thousands of chunks, where a full NumPy dot product over the whole matrix takes
milliseconds — the index structure pgvector provides is not yet earning its
operational cost.

**Cost:** dense search is O(n) per query. At roughly a million chunks this stops
being acceptable and the `VectorIndex` seam has to be filled with pgvector or
sqlite-vec. That is a swap of one module.

**Consequence discovered during build:** the repository lives on an ntfs-3g
(FUSE) mount, which cannot support SQLite's WAL shared-memory mapping. Runtime
state therefore lives at `~/.sovereign` on a native filesystem, configured by
`SOVEREIGN_DATA_DIR`. Code and data are deliberately separable.

---

## D-02 · A purpose-built harness, not goose or OpenHands

**Locked architecture says:** "Do not build a general-purpose agent harness.
Reuse a mature local-capable harness."

**Decision:** build `agent/harness.py` — roughly 250 lines of reason/act loop.

**Why:** the instruction is right about *general-purpose* harnesses, and this is
not one. It has no plugin system, no multi-agent orchestration, no memory
subsystem, no provider abstraction of its own. What it does have are four
properties the control plane requires and that no off-the-shelf harness exposes:

1. it runs only inside a slot the scheduler granted;
2. it checkpoints at every workflow boundary and yields when preempted, which is
   what makes a batch job displaceable by a safety question without being killed;
3. every tool call passes through the policy engine, which can block for human
   approval *before* the call happens;
4. it trims history against the KV budget the runtime measured for the selected
   model.

Retrofitting those into goose would mean forking it. The governance is the
product; the loop is the cheap part.

**Cost:** no community ecosystem of harness plugins. Acceptable — the tool
surface here is deliberately small and fixed.

---

## D-03 · FreeToken is not a dependency

**Locked architecture says:** FreeToken is one optional backend behind the Model
Gateway.

**Decision:** the gateway keeps an OpenAI-compatible adapter seat that FreeToken
fits, and it is not used.

**Why:** two reasons, one general and one specific to this hardware. Generally,
it is at v0.1.2 with documented failure modes that are silent hangs rather than
errors, in exactly the configuration this project would run. Specifically, its
design streams MoE experts from host RAM — and this machine has 15 GB total, of
which about 6 GB is free. There is no host RAM here for an expert cache. The
architecture that FreeToken enables needs a machine with 128 GB or more.

**Cost:** the largest model this deployment can serve is a 21B MoE that runs
part-offloaded. Recorded honestly in the model registry rather than papered over.

---

## D-04 · Engineering drawings are in scope

**Locked architecture says:** full P&ID connectivity reasoning is Phase 4, out of
the MVP critical path.

**Decision:** build it, with an honest confidence model.

**Why:** the problem statement names P&IDs in its first sentence. The locked
document's caution is really about a specific failure — a vision model describing
a drawing fluently and getting the connectivity wrong — and that failure is
avoidable rather than inherent. CAD-exported P&IDs are *vector* PDFs: the
connectivity is present in the file as exact line geometry, so it can be read
rather than inferred.

The design that makes this safe is the per-edge status: `CONFIRMED` (exact vector
geometry, both endpoints attached within tolerance) may be stated as fact,
`PROBABLE` must be labelled as interpretation, `UNRESOLVED` is reported as a gap
requiring a human. Measured against ground truth on the corpus P&ID, the vector
path scores **1.00 F1 on tags and 1.00 F1 on connectivity**.

The raster path — a scan or photograph of a drawing — recovers the equipment
inventory (0.80 F1 on tags) but does **not** produce reliable connectivity, and
the system says so rather than guessing: it emits zero CONFIRMED edges and warns
that a vector PDF is needed. See D-09.

**Cost:** a legend-scoped symbol classifier. Drawings using an unfamiliar symbol
set need entries in `SYMBOL_LEGEND`, which is a table, not code.

---

## D-05 · Residency is the routing objective

**Decision:** model selection charges each candidate the measured cost of making
it resident, and prefers an already-loaded model when the capability gap is inside
a priority-dependent tolerance.

**Why:** on one mid-range GPU the scarce resources are VRAM and wall-clock, not
money. Every commercial router optimises cost per token; on your own GPU the
marginal cost of a token is electricity already being paid for. What is scarce is
residency. A router that re-selects per task without accounting for eviction will
thrash — measured here at 10 model loads across a 16-task shift where 3 suffice.

**Measured:** `scripts/benchmark_scheduling.py` runs an identical mixed workload
under naive per-task routing and under residency-aware scheduling. Naive: 10
loads. Residency-aware: 3 loads, ~28 s of loading avoided, **at identical mean
capability fit** — the batching does not trade quality for time.

**Why the tolerance is priority-dependent:** reusing a resident model saves real
seconds, but the resident model may be materially weaker at multi-step tool use.
Observed directly during the build: `qwen2.5-7b`, reused for a HIGH-priority
approval note because it was within a flat 0.12 tolerance, looped on identical
tool calls and conflated a pressure with a wall thickness. Urgent work now trades
almost no capability for residency (0.02 at CRITICAL); batch work trades freely
(0.25). See D-08.

---

## D-06 · Preemption at workflow boundaries, not token level

**Locked architecture says:** do not make token-level KV preemption an MVP
dependency.

**Decision:** followed. `TaskControl.checkpoint_barrier()` is called after every
tool observation; it persists resumable state and raises `Paused` if the
scheduler has asked the task to yield.

**Why:** workflow-boundary preemption needs nothing from the inference engine, so
changing backends cannot break it. The cost of a preemption is bounded by one
step — for the batch summariser, one document.

---

## D-07 · Provenance propagates through the calculator

**Decision:** a calculation's result counts as Class B (DERIVED) only if every one
of its inputs — including numeric literals written into the expression — is
itself established by a source passage or an earlier verified calculation.

**Why:** this hole was found by watching the system work. The number rule requires
derived values to come from the calculator. A model under context pressure will
satisfy that rule with an invented input: it computed a corrosion rate from a
nominal thickness of 12.4 mm when the document says 12.0 mm, and the result came
back stamped DERIVED. Without input verification the calculator is a laundering
machine that converts a hallucinated number into a trusted one.

Now `verify_inputs()` checks every input against the task's evidence, and
`EvidenceContext.calc_index()` excludes calculations with unverified inputs, so
their results classify as Class D with a rationale naming the offending value.
The calculator also warns the model at the point of use.

---

## D-08 · Enforcement at generation time, not in the prompt

**Decision:** `generate_approval_note` refuses to write a document containing
Class D values, and returns the specific offending numbers with instructions for
resolving each. After two refusals it proceeds, marking the gaps.

**Why:** the system prompt instructs the model to compute every derived value with
the calculator. Observed behaviour: it did the arithmetic in its head anyway, and
produced an approval note stating "remaining life > 5.0 years per clause 4.4 — no
action required" when the remaining life was 4.25 years, which under that clause
*requires* referral to the Engineering Review Committee. A prompt is advice; the
document generator is the last gate before an artefact leaves the workbench, and
that is where the rule has to be enforced.

The two-refusal cap exists so the loop is self-correcting rather than a livelock.

---

## D-09 · Reading critical values by pattern, not by model

**Decision:** `extract_document_values` pulls labelled engineering values from a
document by regex with fuzzy label matching, returning each with its page and
region. The workflow calls it before anything else, and the model is told to use
the returned figures verbatim.

**Why:** retrieval hands the model a passage and hopes it reads the right figure.
For prose that is fine. For an OCR'd equipment particulars block it is not:
observed repeatedly, a 7-8B model asked to read "Nominal Thickness © 12.0 mm" off
a scan produces 12.4, and a wrong wall thickness propagates into a corrosion rate,
a remaining life, and an approval decision. Taking the model out of the reading
loop for exactly those values removes the failure rather than detecting it.

The extractor handles what OCR does to industrial documents: the label/value
separator is frequently mangled (`:` becomes `©`, `=`, or vanishes), and labels
are truncated by fixed-width templates ("Minimum Required Thick"). Matching is
therefore fuzzy on the label and strict on the value.

---

## D-10 · Python for the control plane; algorithms where performance matters

**Decision:** Python throughout, with hot paths fixed algorithmically rather than
rewritten in another language.

**Why:** essentially all wall-clock in this system is spent inside C and CUDA
already — llama.cpp for inference, Tesseract for OCR, ATen for embeddings, MuPDF
for parsing, OpenCV for image work. The control plane's own decisions take
microseconds. Rewriting the orchestration in Go or Rust would move a rounding
error and forfeit the library ecosystem that *is* the data plane.

Where a genuine hotspot existed, the fix was algorithmic and much larger than a
language change would have given. Line chaining in the drawing pipeline was
O(n²): 5.48 s at 5 000 primitives. Indexing endpoints into a uniform grid keyed
by the join tolerance made it effectively linear — **0.052 s at 5 000 (105×), and
0.13 s at 20 000**, which the quadratic form would have taken about 90 s to do.
Rewriting the quadratic algorithm in Go would have bought perhaps 50× and cost
the pipeline its access to OpenCV and PyMuPDF.

Go 1.26 is present on this host and was evaluated for exactly this. It was not
needed.

---

## D-11 · Bubblewrap, not a hosted sandbox

**Decision:** code executes under `bwrap --unshare-all`, falling back to
`unshare -Urn`, and finally to an rlimit-bounded subprocess reported honestly as
*degraded*.

**Why:** the sandbox layer named in the brief is the one component that
structurally cannot meet it — self-hosting E2B requires a Cloudflare account and
a domain, which an air-gapped refinery does not have. Bubblewrap is a host binary
with no control plane at all.

`--unshare-all` includes the network namespace, so the sandbox has no interfaces:
egress is not filtered, it is absent.

**Implementation note that cost real time:** `RLIMIT_NPROC` is enforced per *user*,
not per process tree, and is checked against the new namespace's ucounts during
`clone()`. Applying a useful cap in the parent makes namespace creation fail with
`EAGAIN` on any desktop session already owning a few hundred processes. The
process cap is therefore applied *inside* the sandbox by a small init shim, where
the count starts at one and the limit means what it says.

Verified: filesystem confined to a read-only `/usr` plus one writable workspace
(`/etc/shadow` and `/home` are absent, not merely unreadable); fork bomb contained
at the process cap; CPU bomb killed at the CPU limit; all four egress attempts
blocked.

---

## D-12 · Sovereignty is proved by a denial log, not a packet capture

**Decision:** five enforcement layers, and a self-test that deliberately attacks
the boundary and records every refusal into the hash-chained audit log.

**Why:** a packet capture showing zero egress during a five-minute demonstration
proves only that nothing happened during those five minutes. A non-empty,
attributed, tamper-evident record of refusals is a far stronger claim.

**Attribution detail:** the in-process guard hooks `getaddrinfo` as well as
`connect`. Without that, an external hostname that fails to resolve — the common
case on an isolated host — raises a DNS error and never reaches the connect hook,
so the attempt never appears in the denial log at all. Enforcing at resolution
also means the log names `vendor-portal-sync.example.com` rather than an opaque
IPv6 literal.

---

## D-13 · The audit log is anchored, not only hash-linked

**Decision:** alongside the hash chain, record an anchor row holding the chain
head, the entry count and the highest sequence number issued.

**Why:** found by writing the test for it. Hash-linking detects an *edited* row,
because the next row's `prev_hash` stops matching, and it detects a removed
*interior* row for the same reason. It does not detect **truncation**: lopping
the most recent entries off the end leaves a shorter chain that verifies
perfectly, which is exactly the tampering an attacker who has just done something
would attempt.

The anchor closes that: the count and head no longer match what is present.
Sequence contiguity closes a second gap — because SQLite's AUTOINCREMENT never
reissues a number, a deleted row leaves a permanent hole that no amount of
subsequent honest logging papers over.

**Cost and honest limit:** an attacker with write access to the database can edit
the anchor as well. This raises the bar and makes casual tampering visible; it is
not a substitute for shipping the log off-box, which is what a deployment that
must survive a hostile administrator requires. Stated here rather than implied.


---

## D-14 · The appliance becomes the node of a trust domain; nothing is replaced

**Architecture v2 re-based (`sovereign-workbench-v2.md`) says:** a trust domain
of one node, member devices and one policy; the agent loop moves to a native
client; vLLM, Postgres, LiteLLM, gVisor and mTLS on the node.

**Decision:** the existing control plane *is* the node. The new work enters it
as an eighth authority (`trust`) behind the same façade, and every new surface
is an adapter over something that already exists: the MCP tool server over the
`ToolGateway`, `/v1` over the model gateway, `execute_remote` over the
bubblewrap sandbox, the deliverable gate over the provenance classifier and the
calculator. D-01 (SQLite), D-02 (the node's own harness) and D-11 (bubblewrap)
stand. See `docs/trust-domain.md` §5 for every deviation.

**Why:** the spec's invariant — compute and data never cross a boundary the
organisation cannot revoke — is about *what the node knows and enforces*, not
about which database or engine it uses. A parallel stack would have doubled the
policy and audit surfaces the invariant depends on.

**Cost:** one appliance per domain, as before; clustering the node remains a
non-goal.

---

## D-15 · Device identity is proved at the application layer, not by mTLS

**Decision:** a device is an Ed25519 key. Enrolment is signed by the key being
enrolled; every standing-changing request is signed over
`{purpose, device_id, ts, nonce, body}` (skew ±300 s, strictly increasing,
purpose-bound); a lease token rides on ordinary requests and only the key
renews it. TLS on :8443 is the terminator's job.

**Why:** identity then survives any proxy in front of the node, and every task,
tool call and audit row names the user *and* the device whatever the transport
was. mTLS client-certificate state is not reliably visible to an ASGI app.

**Cost:** a stolen lease token works until it expires (24 h attached) or the
device is revoked; the key, not the token, is what the device must protect.

---

## D-16 · A grade is computed from facts the device cannot set

**Decision:** `managed` is set by an admin; an attestation counts only when a
verifier on the node marked it verified; the egress self-check is self-reported
and so can never lift a device above C, only hold it at C or drop it to D.
Clearance is enforced at retrieval, per tool call, from the grade read live.

**Why:** the review found attestation is a ladder whose rungs mostly need a
vendor's server (T11). Enforcement therefore stays on the node; attestation only
moves the grade. Stopping rule 3 is the default: grade C attaches, retrieves
`internal` at most, never runs detached.

---

## D-17 · The gate checks against what the node served, and lets models be wrong
## in the shape of their input but not in their numbers

**Decision:** every span the node hands out is recorded per session
(`served_spans`). The deliverable gate resolves every number, equipment tag and
date to a served span (correcting a wrong citation), or to a verified
calculation, or strips it. The `deliver` tool accepts the shapes models actually
send — `{title, content}` prose with inline `(span_id=…)` citations — and
refuses loudly when it can find no claims at all.

**Why:** found live. A qwen3-8b run sent `{title, content}` sections; the first
version of the tool silently wrote a near-empty note. Silence is the one outcome
a deliverable tool must never produce. The same run showed the gate stripping a
vessel tag and an inspection date the model had invented.

---

## D-18 · The harness's startup deadline measures contact with the node, not output

**Decision:** `clawcal attach` kills the harness if, within 60 s, it has neither
emitted an event nor been seen by the node (a `/v1` or MCP call on the plan).

**Why:** found live. `opencode run --format json` emits nothing until a model
turn completes; a cold 8B model's first turn took ~120 s, and a deadline on
first *output* killed a healthy harness. What the upstream hang (#38723) looks
like is a harness that never reaches the node — which is what the deadline now
detects.

---

## D-19 · TPM attestation verified natively, offline, with standard tools

**Decision:** the node verifies TPM 2.0 attestation itself with `tpm2-tools` and
`openssl`: EK certificate chain to vendor roots the organisation installs,
MakeCredential/ActivateCredential to prove the attestation key lives in that
TPM, and a PCR quote over a nonce bound to the device's Ed25519 key, compared
with a baseline. Keylime remains a supported verifier, not a dependency.

**Why:** the spec's reason for Keylime is that TPM 2.0 is the one rung that
verifies offline. That property comes from the TPM protocol, not from Keylime's
services, and the tools are already on every Linux node. The node computes the
AK's name from its public area rather than accepting a PEM from the device;
otherwise a device could activate the credential with a real TPM and sign
quotes with a software key.

**Cost:** vendor EK roots must be curated per fleet. Firmware TPMs that publish
their EK certificate only online (some Intel PTT) cannot be enrolled on an
air-gapped node without the organisation supplying the certificate.

---

## D-20 · The client harness is our own build, and the proof is the binary

**Decision:** `bundle/opencode/patch.py` removes from the source, not the
configuration, the models.dev fetch and its refresh loop, runtime npm installs,
auto-update, share and `.well-known` remote config, and restricts providers to
`node` and a loopback `local`. Every anchor is asserted, so an upstream bump
that moves code fails the build rather than leaving a call site in.

**Why:** measured, with egress open and no disable flags. Upstream 1.16.2
reached Cloudflare-hosted services and hung for 275 s (the spec's T10). The
patched build made 100 of 100 cold starts touching nothing but loopback. Given
a config naming OpenAI, a disguised OpenAI-compatible cloud endpoint and a
remote "local", upstream offered all of them; ours offered only the node.

**Cost:** a rebuild and re-verification on every upstream bump. The web UI is
not embedded (`--skip-embed-web-ui`); the headless `run` path does not use it.

---

## D-21 · Instrument slices: export is serving, and one gate runs in two places

**Decision:** a slice is the node's own served set, exported: it is recorded as
served to the slice's session. It is encrypted to the device key (X25519 +
ChaCha20-Poly1305, pure Python, checked against RFC 7748/8439 vectors) and
bound to the lease's grace deadline. It is gated off-site by
`evidence/gatecore.py`, the node's gate rules factored into a standard-library
module and vendored into the client. On rejoin the node re-gates the same
claims and writes the .docx.

**Why:** the spec requires B3 off-site "for those documents alone" without the
client carrying Docling or OCR. Making export itself count as serving means the
rule needs no off-site exception. Sharing the gate's code, rather than
re-writing it in the client, is what makes "the node agrees on rejoin" true.
In the live run the node's counts matched the device's exactly.

---

## D-22 · Detached admission happens on the device

**Decision:** when the lease is detached, `clawcal plan|attach` admits locally,
from the signed manifest (the allowed task classes, the pinned model), with a
small deterministic rule set. Each decision is written to the chained log.

**Why:** found while building the detached path. Admission was a node call, and
a detached device is by definition away from the node. The decision still
follows the node's rules: the node signed the manifest that bounds it, and
the node sees every decision on rejoin.
