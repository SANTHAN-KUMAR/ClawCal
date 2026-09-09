# Overnight build — summary

Built 2026-09-09, ~00:15–03:00 IST, on this machine.

## What you have

A complete, working **Sovereign On-Premise Agentic AI Workbench** at
`sovereign-workbench/` — 93 files, ~14 850 lines of Python plus a
dependency-free web UI. Not a prototype: 21 acceptance criteria, 138 unit tests
and an API smoke test all pass against real models, real OCR, a real sandbox and
real refused network connections.

## Start it

```bash
cd sovereign-workbench
ollama serve &            # if it is not already running
./run.sh                  # → http://127.0.0.1:8794
```

Then, to see the whole story in one run:

```bash
python3 scripts/demo.py           # the six-act industrial demonstration
python3 scripts/verify_e2e.py     # the 21 acceptance criteria
python3 -m pytest tests/ -q       # 138 unit tests
python3 scripts/benchmark_scheduling.py   # the scheduling result
```

## Read these first

| File | What it is |
|---|---|
| `README.md` | the product, the architecture, how to run and extend it |
| `docs/decisions.md` | 13 decisions: what, why, and what each costs |
| `docs/build-report.md` | **the defects testing found** — the most useful read |
| `docs/harmony.md` | the gpt-oss bug you flagged, measured and fixed |
| `GOAL.md` | the goal this session was held to |

## Your three questions, answered

**gpt-oss returning blank.** Measured and fixed. Ending the prompt at
`<|channel|>final<|message|>` gave 2 tokens in 58 s; ending at
`<|start|>assistant` gave a correct answer in 1.6 s. The model needs its analysis
channel — denying it stalls it. It now runs at ~29 tok/s through the harmony
adapter and calls tools correctly. `docs/harmony.md`.

**Engineering drawings.** Built properly, and it works: on the corpus P&ID the
vector path scores **F1 1.00 on tags and 1.00 on connectivity** against ground
truth. Every connection carries CONFIRMED / PROBABLE / UNRESOLVED, instrument
leads are separated from process flow, and a connection the drawing does not
establish comes back CANNOT DETERMINE. Raster scans give the equipment inventory
but explicitly refuse to assert connectivity.

**Stack and performance.** Python throughout, and the reasoning is in
`docs/decisions.md` D-10: essentially all wall-clock is already inside C/CUDA
(llama.cpp, Tesseract, ATen, MuPDF, OpenCV), so a Go or Rust control plane would
move a rounding error and forfeit the library ecosystem that *is* the data plane.
Where there was a genuine hotspot the fix was algorithmic and much larger than a
language change: line chaining went from **5.48 s to 0.052 s at 5 000 primitives
(105×)** via spatial indexing. Go 1.26 is installed here and was evaluated; it
was not needed.

## The compute contribution

Your chosen novelty. Model selection is treated as a **memory-residency
decision**, not a capability lookup:

```
                                   loads    load s    wall s  mean fit
naive per-task routing                14     231.5     238.5     0.821
residency-aware scheduling             4      89.9      96.3     0.825
```

10 fewer model loads and 141 seconds saved on a 16-task shift, at *marginally
better* answer quality. Plus: concurrency derived from measured VRAM, a KV budget
computed from each model's real attention geometry, context granted to the task
rather than to the machine, priority-dependent residency tolerance, minimum dwell
to stop oscillation, priority aging to stop starvation, and preemption at
workflow checkpoints — a batch job pauses mid-run for a safety question and
resumes without redoing work.

## Two things to know

**The host firewall is not left applied.** `ops/egress-policy.sh` was tested —
it blocks external traffic, permits loopback, and logs every dropped packet with
prefix `SOVEREIGN-EGRESS-DENY` — and then removed, so your machine has normal
network. Turn it on with `sudo ./ops/egress-policy.sh apply`. The other four
sovereignty layers are always on and do not need it.

**Runtime state lives at `~/.sovereign/`,** not beside the code. The repo sits on
an ntfs-3g mount, which cannot support SQLite's WAL. Set `SOVEREIGN_DATA_DIR` to
move it.

## Fixed after you reported it (9 Sep, morning)

The "summarize the pdf in /home/santhankumar/Downloads/" failure. Three bugs, all
mine: Submit silently ignored the file you had chosen unless you pressed "Upload
& index" first; `list_files` turned an absolute host path into an empty workspace
listing instead of a refusal; and nothing told the agent that documents are
reached by tool rather than by path. Attaching a file on Submit now works, the
refusals are actionable, and `tests/test_files.py` (17 tests) holds it. Verified
end to end on one of your own PDFs: 13 pages, 74 chunks, correct summary.

## Second round of fixes (9 Sep, 08:30)

The `.xlsx` attempt exposed four more, one of them serious:

1. **The agent answered about the wrong document.** With the attachment dropped,
   "summarize the attached doc" made it summarise an inspection report you never
   mentioned — correctly cited, entirely wrong file. It now refuses and asks for
   the attachment, and the real document inventory is injected rather than
   recalled (its first attempt at listing was fabricated).
2. **Your browser was running cached JavaScript**, which is why the first fix
   appeared not to work. `/` and `/static` are now `no-store`.
3. **`.xlsx` / `.docx` / `.pptx` are now ingested natively.** Verified on your
   timetable file: 2 sheets, 2333 chunks, correct summary. Two-column tables read
   as `Design pressure: 16 bar g`, so `extract_document_values` works on an
   Excel equipment register exactly as it does on a scanned report.
4. **A failed ingest no longer blocks retrying the same file.**

162 unit tests, and A3/A4/A5/A11/A16/A18/A19/A20 re-verified.

## Honest limits

Listed at the end of `README.md`. The important one: provenance catches wrong
*numbers*, not every wrong *judgement*. Approval notes are drafts for an engineer
to sign, and the template says so.
