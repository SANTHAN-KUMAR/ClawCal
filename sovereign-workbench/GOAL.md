# GOAL — Sovereign On-Premise Agentic AI Workbench

## Objective
Build a **working, beyond-POC software product** implementing the problem statement
*"Sovereign On-Premise Agentic AI Workbench using Open-Weight Multimodal LLMs for
Confidential Industrial Work"*, running entirely on this workstation with open-weight
local models, and verified end to end.

## Target hardware (measured 2026-09-09)
| Item | Value |
|---|---|
| GPU | NVIDIA RTX 4060 Laptop, **8 GB VRAM** |
| CPU | Intel i7-14650HX, 16C/24T |
| System RAM | **15 GB** (≈6 GB typically free) |
| PCIe | Gen4 x8 (link negotiates to Gen1 x8 at idle) |
| OS | Fedora 42, Linux 6.19, CUDA 13 / driver 580 |
| Code FS | ntfs-3g (FUSE) — **no SQLite WAL, no POSIX perms** |
| Data FS | ext4 at `~/.sovereign` — DB, sandboxes, evidence live here |

## Headline technical contribution
The team's chosen novelty is **the compute problem**. Everything else in the stack is
commodity; the scarce resources on one mid-range GPU are **VRAM residency and wall-clock**,
not money. So the load-bearing custom component is:

> **A residency-aware admission scheduler that treats model selection as a memory-eviction
> decision made once per task, not a classification decision made per step** — with
> empirically measured model residency costs, anti-thrash batching of queued work by
> required model, priority preemption at workflow checkpoints, and a context/KV budget
> that refuses work it cannot finish instead of hanging.

This is measurable: the benchmark harness must show naive per-task routing thrashing the
GPU versus residency-aware scheduling completing the same workload materially faster.

## Scope — what gets built
1. **Sovereign Control Plane** — task manager, model registry, capability router, tool
   policy engine, evidence/provenance engine, hash-chained audit, egress policy.
2. **Resource-Governed Agent Runtime** — admission control, priority queue with aging,
   concurrency + quotas, residency manager (evict/pin via backend keep-alive), workflow
   checkpoint/resume, health/heartbeat/recovery, live telemetry.
3. **Model Gateway** — backend-neutral (Ollama / llama.cpp / any OpenAI-compatible),
   per-model prompt adapters, health checks, fallback chain, hard timeouts.
   *Includes the gpt-oss **harmony** adapter — see `docs/harmony.md`.*
4. **Agent Harness** — governed ReAct loop, checkpointable, tool-gated, injection-aware.
5. **Local Knowledge Plane** — ingest → OCR → chunk → local embed → hybrid retrieve →
   rerank → evidence package. No external service.
6. **Multimodal** — scanned PDF / photo / handwriting via Tesseract + local VLM.
7. **Engineering-drawing understanding** — the capability the prototype lacked. Vector-
   native and raster paths, symbol detection, tag extraction, **line tracing to a real
   connectivity graph**, with an honest per-edge confidence model that refuses rather
   than guesses.
8. **Tool Gateway** — files, search, calculator (traced), **bubblewrap sandbox with
   `--unshare-net`**, document generation, approval, egress probe.
9. **Deliverables** — organisation-templated .docx / .xlsx / .pptx, code, calc reports.
10. **Sovereignty proof** — default-deny nftables egress + in-process guard + sandbox
    netns + **non-empty denial log**, attributed to a process and task.
11. **Web workbench** — one SPA: chat, files, tasks, evidence, deliverables, agent trace,
    resource view, drawings viewer, audit, network events.

## Acceptance — the system is done when `scripts/verify_e2e.py` passes all of:
- A1 Two+ local model capabilities served; gpt-oss returns non-empty text.
- A2 Task-aware model selection demonstrably differs across task types, with a recorded reason.
- A3 Agent plans, calls tools, observes, iterates.
- A4 Scanned inspection report processed locally into findings with page+region refs.
- A5 Local SOP retrieved and used.
- A6 Important facts resolve to evidence (Class A/B/C/D).
- A7 Derived numbers carry a reproducible calculation trace.
- A8 Unsupported claims are refused / flagged, not invented.
- A9 Multiple workloads queue and prioritise under real resource constraints.
- A10 A low-priority workflow pauses at a checkpoint and resumes.
- A11 Code executes in the sandbox with no network and no host access.
- A12 A real .docx deliverable is produced from an org template.
- A13 An outbound connection attempt is blocked, attributed and logged.
- A14 Audit chain verifies (tamper-evident).
- A15 Backend can be swapped without touching product code.
- A16 Engineering drawing → symbols, tags, connectivity graph, with confidence + refusals.
- A17 Residency-aware scheduling beats naive routing on the benchmark.

## Deliberate deviations from the locked architecture (justified in docs/decisions.md)
- **SQLite (WAL, on ext4) instead of PostgreSQL+pgvector** — zero external services is
  worth more to an air-gapped deployment than pgvector; the index layer is abstracted.
- **Purpose-built governed harness instead of goose/OpenHands** — the locked doc says
  reuse a harness, but no off-the-shelf harness can be admission-gated, checkpointed at
  workflow boundaries, or tool-policy-gated per step. Ours is small and not general-purpose.
- **FreeToken is not used** — it is v0.1.2 with documented silent-hang failure modes and
  15 GB of host RAM cannot hold a streamed 35B expert cache anyway. The Model Gateway
  keeps its adapter seat open.
- **Engineering drawings promoted into scope** (locked doc deferred them to Phase 4) —
  the problem statement names P&IDs explicitly, and the honest-confidence design makes
  connectivity tractable without pretending to be a research result.
