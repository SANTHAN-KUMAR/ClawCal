# Running ClawCal (Sovereign Workbench) on your own laptop

This is the step-by-step version of the README quick start, written for
someone setting this up for the first time. Follow it top to bottom; each
section says what it's for and how to tell it worked.

Total first-time setup: ~20–30 min (mostly model downloads).

---

## 0. What you need

| Requirement | Minimum | Notes |
|---|---|---|
| OS | Linux (Fedora/Ubuntu/Debian) | macOS works for everything except the nftables egress policy (§6) and bubblewrap sandbox (falls back to `unshare`, or `none`) |
| Python | 3.11+ | 3.13 is what this was built/tested on |
| RAM | 8 GB+ | 16 GB recommended if running `gpt-oss-20b` |
| GPU | Optional | Any NVIDIA GPU helps; it also runs CPU-only, just slower |
| Disk | ~15 GB free | mostly model weights |

You do **not** need CUDA installed yourself — Ollama bundles what it needs.

---

## 1. Clone the repo

```bash
git clone https://github.com/SANTHAN-KUMAR/ClawCal.git
cd ClawCal/sovereign-workbench
```

## 2. System packages

```bash
# Fedora
sudo dnf install -y tesseract bubblewrap nftables python3-pip

# Ubuntu/Debian
sudo apt install -y tesseract-ocr bubblewrap nftables python3-pip python3-venv
```

These enable, respectively: OCR on scanned documents, the sandbox that runs
agent-generated code with no network/host access, and the host-level egress
firewall. The app still starts without any of them — `run.sh` tells you what's
missing and what degrades.

**Optional — only if you need to open `.dwg` (AutoCAD binary) files:**
DWG is proprietary; the project reads it by converting to DXF first via
[LibreDWG](https://www.gnu.org/software/libredwg/). If you don't have it, plain
DXF and vector PDF drawings still work fine — you'll just get a clear error
naming the missing tool if someone feeds it a `.dwg`. To add it:

```bash
mkdir -p ~/.sovereign/tools/bin
# build LibreDWG from source, or grab a release build of dwg2dxf,
# and place the `dwg2dxf` binary at ~/.sovereign/tools/bin/dwg2dxf
```

## 3. Python environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

This installs from wheels only — nothing here calls out to the network once
installed, which matters for the sovereignty story.

## 4. Local inference backend (Ollama)

```bash
# install Ollama if you don't have it: https://ollama.com/download
ollama serve &

ollama pull qwen3:8b          # general reasoning / chat
ollama pull qwen2.5:7b        # lighter/faster general tasks
ollama pull qwen2.5vl:3b      # vision (scanned docs, drawings, photos)
ollama pull gpt-oss-20b       # optional — deep reasoning, needs more RAM/VRAM
```

On an 8 GB GPU, `gpt-oss-20b` partially offloads to CPU — it still works, just
slower. Skip it if your machine has under 16 GB RAM total.

**Verify:** `curl http://127.0.0.1:11434/api/tags` should list the models above.

## 5. Seed the demo corpus and check the machine

```bash
python3 scripts/seed_corpus.py            # builds sample SOPs, scanned reports, a P&ID
python3 -m sovereign.server --probe       # from backend/, or with PYTHONPATH set
```

`--probe` prints what this machine can actually serve (models found, GPU,
sandbox engine, OCR) without starting the server — use it to catch a missing
dependency before the demo, not during it.

## 6. Start the workbench

```bash
./run.sh
```

This is a wrapper around `python3 -m sovereign.server` that checks
preconditions first (Ollama up? tesseract present? sandbox available?) and
tells you exactly what to fix if something's off, rather than failing deep
inside a request. Open **http://127.0.0.1:8794**.

To run it directly instead: `cd backend && python3 -m sovereign.server`.

## 7. (Optional, recommended for the actual sovereignty demo) Host firewall

```bash
sudo ./ops/egress-policy.sh apply      # loads now
sudo ./ops/egress-policy.sh persist    # + survives reboot
sudo ./ops/egress-policy.sh status     # check it's loaded
```

Default-deny outbound at the kernel level, logging every blocked attempt with
prefix `SOVEREIGN-EGRESS-DENY` — this is what the Sovereignty tab in the UI
reads from. The app works and still refuses egress at the application layer
without this, but this is the layer that makes the refusal true for *any*
process on the box, not just ones the control plane owns.

## 8. Prove it works

```bash
python3 scripts/verify_e2e.py            # all 21 acceptance criteria, nothing mocked
python3 scripts/benchmark_scheduling.py  # residency-aware vs naive scheduling
```

`verify_e2e.py` is slow (several minutes) because it runs real inference for
each check — that's the point, nothing here is a stub.

---

## Config knobs (env vars, all optional)

Set before `run.sh` / `python3 -m sovereign.server` if you need to override:

| Variable | Default | What it does |
|---|---|---|
| `SOVEREIGN_DATA_DIR` | `~/.sovereign` | DB, evidence, sandboxes, checkpoints. Must be on a real POSIX filesystem (ext4/btrfs) — **not** an NTFS/exFAT mount, which can't do SQLite WAL or POSIX permissions |
| `SOVEREIGN_PORT` | `8794` | web UI / API port |
| `OLLAMA_URL` | `http://127.0.0.1:11434` | point at a remote Ollama box if not running locally |
| `OPENAI_COMPAT_URL` | (unset) | use vLLM / llama.cpp / any OpenAI-compatible server instead of Ollama |
| `SOVEREIGN_SANDBOX` | `auto` | `bwrap` \| `unshare` \| `none` — force a sandbox engine |
| `SOVEREIGN_ORG` / `SOVEREIGN_UNIT` | demo org name | shown in generated deliverables |

## If your project checkout lives on an NTFS/exFAT drive

This repo itself can live anywhere — but `SOVEREIGN_DATA_DIR` (runtime state:
DB, sandboxes, evidence) must not. If your clone is on such a drive (e.g. a
shared/external drive mounted via ntfs-3g), just leave `SOVEREIGN_DATA_DIR` at
its default (`~/.sovereign`, on your normal home-directory filesystem) — don't
repoint it back onto the same drive as the code.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `run.sh` says "Ollama is not responding" | `ollama serve &`, then retry |
| OCR fails on scanned PDFs | `tesseract` not installed — see §2 |
| Sandbox falls back / code tool degraded | install `bubblewrap`, or accept the `unshare` fallback |
| `.dwg` file rejected with a converter message | see the LibreDWG note in §2 — DXF/PDF work without it |
| Slow first response | first inference call loads the model into VRAM/RAM — subsequent calls on the same model are fast (this is exactly what the residency scheduler is managing) |
| Port 8794 already in use | `SOVEREIGN_PORT=8000 ./run.sh` |

---

Once it's running, the top-level tabs in the UI map directly to the acceptance
criteria in [`GOAL.md`](GOAL.md) — chat/tasks for the agent loop, Drawings for
the engineering-drawing pipeline, Evidence for provenance, Sovereignty for the
egress proof, Audit for the tamper-evident log.
