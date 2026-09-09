# ClawCal — Sovereign On-Premise Agentic AI Workbench

A self-hosted, air-gapped agentic AI workbench for confidential industrial
knowledge work. It runs open-weight models on the organisation's own GPU,
coordinates them as governed workloads, grounds answers in the organisation's own
documents, and produces real deliverables — while proving, through a
tamper-evident denial log, that nothing left the building.

Built for the problem statement *"Sovereign On-Premise Agentic AI Workbench using
Open-Weight Multimodal LLMs for Confidential Industrial Work"*.

---

## Where things are

| Path | What it is |
|---|---|
| **`sovereign-workbench/`** | **The product.** Start here. |
| `sovereign-workbench/README.md` | architecture, how to run, how to extend |
| `sovereign-workbench/docs/decisions.md` | 13 decisions: what, why, what each costs |
| `sovereign-workbench/docs/build-report.md` | the defects testing found — most useful read |
| `sovereign-workbench/docs/harmony.md` | the gpt-oss prompt bug, measured and fixed |
| `Sovereign_On_Premise_Agentic_AI_Workbench_FINAL_LOCKED.md` | the locked solution architecture |
| `Untitled document.md` | the stack survey the architecture was derived from |
| `gpt-oss-20b/` | the original single-file prototype, kept for reference; superseded |

Model weights are **not** in this repository and never should be — the 20B MXFP4
GGUF alone is 12 GB. Models are pulled by the inference backend, which is exactly
what makes them replaceable.

---

## Getting started

```bash
git clone https://github.com/SANTHAN-KUMAR/ClawCal.git
cd ClawCal/sovereign-workbench

# system packages
sudo dnf install -y tesseract bubblewrap nftables        # or apt equivalents
pip install -r requirements.txt

# a local inference backend, and the models
ollama serve &
ollama pull qwen3:8b && ollama pull qwen2.5:7b && ollama pull qwen2.5vl:3b
ollama pull gpt-oss-20b        # optional deep reasoner, needs ~12 GB

python3 scripts/seed_corpus.py     # synthetic SOPs, scanned reports, a P&ID
./run.sh                           # → http://127.0.0.1:8794
```

Check what your machine can actually serve before anything else:

```bash
make probe
```

## Verify it works

Nothing in the acceptance suite is mocked — models load, documents are OCR'd,
code executes in the sandbox, and network connections are genuinely attempted and
genuinely refused.

```bash
make test          # 165 unit tests, no GPU needed, ~45 s
make verify        # 21 acceptance criteria, loads models, ~12 min
make bench         # residency scheduling vs naive routing
python3 scripts/demo.py    # the six-act industrial demonstration
```

---

## Working on this in parallel

The layers are deliberately separable; two people rarely need the same file.

| Area | Owns | Touch |
|---|---|---|
| Runtime / scheduling | admission, residency, preemption, quotas | `backend/sovereign/runtime/` |
| Model gateway | backends, prompt adapters, registry, fallback | `backend/sovereign/gateway/` |
| Knowledge | OCR, ingest, embeddings, retrieval, extraction | `backend/sovereign/knowledge/` |
| Drawings | vector + raster P&ID, connectivity graph | `backend/sovereign/drawings/` |
| Evidence | the four classes, the number rule | `backend/sovereign/evidence/` |
| Tools & policy | tool gateway, sandbox, egress, injection | `backend/sovereign/tools/`, `policy/` |
| Deliverables | docx / xlsx / pptx templates | `backend/sovereign/deliverables/` |
| API & UI | routes, SSE, the workbench SPA | `backend/sovereign/api/`, `frontend/` |

House rules that keep the thing honest:

1. **Run `make test` before you push.** It is fast and needs no GPU.
2. **A capability the system does not have must say so.** Refusing is a feature
   here; a confident wrong answer is the failure mode the whole design exists to
   prevent. If you add a path that can guess, add the refusal with it.
3. **Never ask the model for a fact the control plane already holds.** Read it
   and inject it. Half the defects in `docs/build-report.md` are violations of
   this rule.
4. **Adding a model is a registry row, not a code change** — see the workbench
   README. If you find yourself special-casing a model name, something is wrong.
5. **Runtime state lives outside the tree**, under `SOVEREIGN_DATA_DIR`
   (default `~/.sovereign`). Nothing generated belongs in git.
6. **No CDN, ever.** The UI ships every asset it needs. A workbench that phones
   out for a font contradicts the claim it exists to make.

---

## Status

21/21 acceptance criteria, 165 unit tests and an API smoke test over all 30
routes currently pass on an RTX 4060 Laptop (8 GB VRAM) / 15 GB RAM machine.
Measured there: drawing connectivity F1 1.00 on the vector path, 14 → 4 model
loads under residency-aware scheduling, and gpt-oss-20b at ~29 tok/s through the
harmony adapter.

Known limits are listed at the end of `sovereign-workbench/README.md`. The one
worth repeating: the evidence layer catches wrong *numbers*, not every wrong
*judgement*. Approval notes are drafts for an engineer to sign.
