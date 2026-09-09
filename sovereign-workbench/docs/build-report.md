# Build report

What was built, what was measured, and what testing found. Written during the
build rather than after it, so the defects below are the ones the system
actually had — several of them were invisible until something was tested against
a real input.

---

## What exists

| Layer | Module | Lines |
|---|---|---|
| Model Gateway (backend-neutral, 3 prompt adapters) | `gateway/` | ~1 270 |
| Resource-governed runtime | `runtime/` | ~1 060 |
| Tool gateway (17 governed tools) | `tools/` | ~1 950 |
| Engineering drawings | `drawings/` | ~1 800 |
| Knowledge plane | `knowledge/` | ~1 400 |
| Policy: tools, egress, injection | `policy/` | ~560 |
| Evidence and provenance | `evidence/` | ~340 |
| Deliverables (docx/xlsx/pptx) | `deliverables/` | ~730 |
| Agent harness and prompts | `agent/` | ~490 |
| Workflows | `workflows/` | ~360 |
| HTTP API (36 routes) | `api/` | ~470 |
| Web workbench (no framework, no CDN) | `frontend/` | ~1 100 |

Verification: **21 acceptance criteria** in `scripts/verify_e2e.py` (all passing),
**138 unit tests** in `tests/`, an API smoke test covering all 30 documented
routes, a scheduling benchmark, and a ground-truth-scored drawing corpus.

---

## Measurements on the target machine

RTX 4060 Laptop (8 GB VRAM), i7-14650HX, 15 GB RAM, PCIe 4 x8.

| Measurement | Value |
|---|---|
| gpt-oss-20b via the harmony adapter | 28–30 tok/s, part-offloaded (57% GPU) |
| qwen3-8b | ~45 tok/s, fully resident |
| OCR on the synthetic scanned report | 93% mean confidence, all values correct |
| Drawing pipeline, vector path | tags F1 **1.00**, connectivity F1 **1.00** |
| Drawing pipeline, raster path | tags F1 0.80, connectivity deliberately 0 |
| Line chaining, 5 000 primitives | 5.48 s → **0.052 s** after spatial indexing |
| Line chaining, 20 000 primitives | 0.13 s |
| Scheduling, 16-task mixed shift | 14 model loads → **4**; 141 s of loading avoided (61%) at slightly *better* capability fit (+0.004) |
| Sandbox | bubblewrap, 4/4 egress attempts blocked |

---

## Defects found by testing, and what they mean

These are listed because each was invisible from the code and only appeared when
something real was run through it.

**The harmony prompt bug (36× latency).** Ending a gpt-oss prompt at
`<|channel|>final<|message|>` — the obvious-looking optimisation — produced 2
tokens in 58 s and frequently nothing. Ending at `<|start|>assistant` produced a
correct answer in 1.6 s. The model needs its analysis channel; denying it stalls
the model. This is what made gpt-oss look unusable in the earlier prototype.

**BM25 scoring was inverted, silently.** SQLite's `bm25()` returns a *negative*
score where more negative is better. Clamping it at zero to make it positive —
which looks like defensive coding — collapsed every lexical result to the same
value and disabled half of hybrid retrieval. Nothing failed; the system just
retrieved worse.

**The egress guard broke dense retrieval.** Sovereignty enforcement correctly
refused sentence-transformers' hub metadata check, the model load raised, and
retrieval fell back to lexical-only. Also silent. Fixed by forcing offline mode
and by making the degradation an audited event and an asserted test.

**The calculator was a laundering machine.** The number rule says derived values
must come from the calculator. A model under context pressure satisfied that rule
with an invented input — computing a corrosion rate from a nominal thickness of
12.4 mm when the document says 12.0 — and the result came back stamped DERIVED.
Provenance now propagates: a calculation confers Class B only if its own inputs,
including numeric literals in the expression, are established.

**Prompting did not stop mental arithmetic.** Told explicitly to use the
calculator, the model did the sums in its head and produced an approval note
stating "remaining life > 5.0 years, no action required" when the figure was 4.25
years — which under that clause *requires* escalation to the Engineering Review
Committee. The document generator now refuses and names the offending values.

**A 7-8B model misreads values off a scan.** Asked to read "Nominal Thickness ©
12.0 mm" from an OCR'd table, it repeatedly produced 12.4. Retrieval plus a good
prompt could not fix this. `extract_document_values` now reads those values by
pattern, with page and region, and the model is told to use them verbatim.

**OCR confidence means different things on different inputs.** Tesseract reports
68% mean confidence on the handwritten field note while turning "N 9.4 E 9.2
S 9.6 W 9.3" into "No.4 E92 59 4 Wo,3". A single threshold would have accepted
confidently wrong thickness readings. Images now need 82%; rendered pages of
typed text still need 45%.

**`RLIMIT_NPROC` broke the sandbox.** It is enforced per *user* and checked
against the new namespace's ucounts during `clone()`, so any useful cap made
bubblewrap fail with `EAGAIN` on a desktop session owning several hundred
processes. The process cap moved inside the sandbox, where the count starts at
one.

**DNS failures escaped the denial log.** An external hostname that does not
resolve — the common case on an isolated host — raised a DNS error before
reaching the `connect()` hook, so the attempt was never recorded. The guard now
enforces at `getaddrinfo` too, which also means the log names
`vendor-portal-sync.example.com` rather than an IPv6 literal.

**The KV budget formula was wrong by 1000×.** It granted a 27 852-token context
where about 9 000 was affordable, pushing the model to 22% CPU. Replaced with a
calculation from each model's real attention geometry, and the context granted is
now sized to the task rather than to what the machine could afford.

**Stale `RUNNING` rows silently stopped the scheduler.** Tasks orphaned by a
control-plane restart kept holding quota slots forever, and the queue stopped
moving with a correct-sounding reason. Recovery now runs at start: a checkpointed
orphan becomes resumable, one without a checkpoint fails explicitly.

**Symbol glyph strokes were read as pipes.** A heat exchanger is a circle with a
diameter line; an instrument balloon is a circle with a divider. Those strokes
look exactly like short process lines, and they welded neighbouring equipment
together through the middle of a symbol — producing a phantom column-to-drum
connection that bypassed the exchanger between them. Connectivity F1 went from
0.60 to 1.00 once glyph-internal runs were excluded.

**The vision model was routed the drawing *reporting* task.** Drawing
understanding is done by the geometric pipeline; the agent only has to report it
and preserve the confidence labels. Because `drawing_analysis` declared a vision
capability floor, that reporting went to a 3B VLM, which answered "V-204 is
connected to vessel V-204". The floor moved to text and structure, where the work
actually is, and the same task now returns the full tag list with CONFIRMED,
PROBABLE and CANNOT DETERMINE each labelled correctly.

**A full-resolution image killed the vision model.** A 1320x1870 scan tiles into
thousands of image tokens and the runner terminated without a response, surfacing
as a connection reset rather than an error. Images are now downscaled to a 1280 px
long edge before inference — which costs nothing for document reading and is the
difference between the handwriting path working and silently falling back to the
OCR output it was meant to replace.

**Residency reuse traded away too much.** A flat tolerance let a HIGH-priority
approval note reuse a resident but weaker model, which then conflated a pressure
with a wall thickness. The tolerance is now priority-dependent and applied to the
*score* — which already prices decode speed — rather than to raw capability fit.

**Audit hash-linking did not detect truncation.** Removing the most recent
entries leaves a shorter chain that verifies perfectly. An anchor row recording
the head and entry count, plus a sequence-contiguity check, closes it.

---

**A chosen file was silently dropped, and the refusal explained nothing.** Found
in use, not in testing. The operator picked a PDF, typed "summarize the pdf file
in /home/user/Downloads/", and pressed Submit. Three things went wrong at once:
the frontend only attached a file if a *separate* "Upload & index" button had
been pressed first, so the file was ignored without a word; the agent then reached
for the host path, and `list_files` — which correctly confines agents to the task
workspace — quietly stripped the leading slash, found nothing, and returned
`ok=true, "workspace is empty"`; with no way to recover, the agent correctly
concluded CANNOT DETERMINE.

The confinement was right. Everything around it was wrong. Submit now indexes a
chosen file automatically, an absolute path is refused *as* an absolute path
rather than reinterpreted, every file-tool refusal names `list_documents` /
`search_knowledge` as the way documents are actually reached, an empty workspace
lists the indexed documents instead of saying nothing, and the system prompt
tells the model this before it reaches for a path. Seventeen regression tests in
`tests/test_files.py` hold it.

The general lesson is the one worth keeping: a security control that refuses
correctly but uninformatively reads, from the outside, exactly like a broken
system.

---

**The agent answered about the wrong document, confidently.** The worst defect
found so far, and it only appeared in use. With the attachment silently dropped
(above), a request to "summarize the attached doc" left the agent with no
attachment — so it picked the most plausible indexed document, an inspection
report the operator had never mentioned, and summarised that. Every value was
correctly cited; the provenance check reported 15 sourced facts and zero
unsupported; and the entire answer was about the wrong file. Provenance cannot
catch this, because the numbers really were in a real document.

The fix is structural. When a request refers to an attachment ("the attached",
"this doc", "the uploaded pdf") and nothing is attached, the agent is told so
explicitly and forbidden to substitute. Asked in the first version to *list* what
is indexed instead, it invented a plausible catalogue — "IR-2022-1234",
"SOP-001" — so the real inventory is now injected into the message rather than
recalled. Same principle as `extract_document_values`: never ask the model for a
fact the control plane already holds.

**A stale browser kept running last week's client.** After the attachment fix
shipped, the same failure recurred: the page was still serving a cached
`app.js`, so Submit still dropped files while the label on the button had already
changed. The symptom is silent and baffling. `/` and `/static` are now
`Cache-Control: no-store`.

**Spreadsheets were rejected by extension.** `.xlsx` returned "unsupported file
type", which made "spreadsheet work" — named in the problem statement — a file
dialog error. Workbooks, Word documents and decks are now read natively. Tables
are rendered so the labelled-value extractor works on them: a two-column table is
a parameter/value list and reads `Design pressure: 16 bar g`, while a wider one
keeps its headers. The same extractor that reads a scanned inspection report now
reads an equipment register kept in Excel.

**A failed ingest poisoned its own content hash.** Ingestion deduplicates on
sha256. A failed attempt left a row behind, so the file matched a document that
held nothing and could never be retried — the fix for the format gap would not
have helped anyone who had already tried the file once.

---

## Honest limits

* Raster drawings give equipment inventory, not connectivity. The system reports
  zero confirmed connections and says a vector PDF is needed.
* Instrument balloon text on a rasterised drawing is often genuinely illegible;
  those tags are missed, not invented.
* Provenance catches wrong *numbers*. It cannot catch every wrong *judgement* —
  approval notes are drafts for an engineer to sign, and the template says so.
* The audit anchor raises the bar against tampering but an attacker with database
  write access can edit it too. Off-box replication is the real answer.
* Dense retrieval is O(n) per query; fine to ~10^5 chunks.
