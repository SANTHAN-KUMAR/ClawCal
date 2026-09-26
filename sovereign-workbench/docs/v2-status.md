# ARCHITECTURE-v2: build status against its gates

Each row of §8 ends in a gate "a judge or a customer can run". This is where each
gate stands, and what shows it. *Partial* means the gate holds with a stated
exception.

| step | gate | status | how it is shown |
|---|---|---|---|
| **B0 repair** | `make test` green; every figure traces to an artefact; profile-row validity | **done** | 264 tests pass (also under Python 3.10); the OpenCV 5 raster fix; `runtime/perfmodel.validate` flags the 0 MB vision row; `docs/dirty-eval-results.md` is generated from result files |
| **B1 control plane** | every authority writes a decision row; no placeholder actor | **done** | `control/` façade; `decisions` table, hash-chained and cross-referenced into the audit log (acceptance A22); principals with roles on every row; `tests/test_control_plane.py::TestBoundary` greps both rules |
| **B2 contract** | the transcript renders the demo; every tool result carries an outcome | **done** | `GET /api/sessions/{id}/transcript` (json and text); per-session `review`/`trusted`/`locked` (A19, A23); `outcomes.py` applied in the tool gateway (`TestOutcomes`); `spreadsheet_read` / `spreadsheet_edit` (A27: 31/31 real cells) |
| **B3 clients** | the same task from CLI and web produces identical transcripts | **done** | `./clawcal` (standard library only) prints the server's own text rendering of the transcript that the web client reads. The web adds inline evidence badges (click → page with the region lit), a sovereignty strip on every view with the self-test button, inline approvals and a mode selector |
| **B4 dirty corpus** | every class has a score and a refusal rate; no class regresses | **done, with a finding** | `scripts/fetch_dirty_corpus.py` (106 real items, 7 classes); `scripts/eval_dirty.py` reports quality, refusal rate and confidently-wrong, split by ESTABLISHED / INTERPRETED; OCR thresholds calibrated from it; two-reader cross-check; loop and refusal detection; template validator. **Finding:** Qwen2.5-VL cannot be measured on this host (the backend refuses the load) |
| **B5 basis** | admission shows its basis; an uncalibrated model shows "unknown" | **done** | every `ModelCard` figure carries measured / calibrated / prior with a sample count; wait estimates say "unknown — first run of …" (`TestBasis`); the model-directory filesystem probe warns on FUSE; measured VLM quality replaces catalogue claims in routing (`--record`) |
| **B6 install** | clean Fedora/Ubuntu VM to a working product with a red/green strip, one command | **partial** | `ops/install.sh`: dry run, `shellcheck`, `systemd-analyze verify` and the egress ruleset exercised in a network namespace, all here. **Not run:** the root-only steps on a real clean VM; this workstation has no passwordless sudo |

## Beyond the build order

These came from the target deployment (a cloud GPU instance) and from the
out-of-memory crashes on the development machine:

* the server refuses a network bind without token auth, and never trusts a
  proxied request as the owner;
* host RAM is priced before every model load, a backend's own memory refusal
  is learned, and idle models are evicted to make room for a tool's VLM call;
* multi-GPU summing; several Ollama endpoints as one backend; capability rows
  for the 24–80 GB tier;
* upload streaming with a size limit; ingestion off the event loop; path
  confinement; an async live stream; an enforced queue depth; incremental audit
  anchoring;
* signed (Ed25519) sovereignty reports and audit exports, verifiable offline.

## Known limits, stated so nobody is surprised

* Injection *detection* catches 17% of an unseen public jailbreak set. Pattern
  rules cannot do better on free-form jailbreaks. The guarantee is that the
  tool policy and egress refuse the action, whatever the model is persuaded of.
* A VLM reading is INTERPRETED, never ESTABLISHED, unless an independent OCR
  reading agrees with it. On a clean form the best measured VLM recovers
  95–98% of words, and those words are still labelled for an engineer to
  confirm.
* One control plane per GPU host. The evaluation harness and the server must
  not share a GPU at the same time: in-flight tracking is per process.

## The trust domain (sovereign-workbench-v2.md, "Architecture v2, Re-based")

Built on top of B0–B6; see `docs/trust-domain.md`.

| spec | gate | status | how it is shown |
|---|---|---|---|
| §1, §11 grades and data classes | a grade-C device cannot see or read a confidential document | **done** | `TestClearance`; live T7 |
| §10 leases, revocation | revoking stops the lease now and renewal next | **done** | `TestLeases`; live T11 |
| §10.4 chained log, anchors | an edited entry quarantines the device until an operator clears it | **done** | `TestAnchoring`; live T9, T10 |
| §7 B5 classes and manifests | this laptop classifies with its reason; research tiers never ship | **done** | `TestProfiler`; live T4 (SPLIT-PCIE, gpt-oss-20b) |
| §8 B1 placement, `/admit` | extract always on the node; detached classes follow the manifest | **done** | `TestPlacement`; live T6 |
| §9 signed bundles | rollback, freeze, mix-and-match and forgery refused | **done** | `TestBundles`; live T3 |
| §11 client egress | reject, not drop; node reachable | **done on Linux** | live T1 in a network namespace; Windows/macOS rulesets generated, unverified here |
| §12 `execute_remote` | staged copy, no network, changed files returned | **done** | `TestRemoteTools` |
| §13 served set, B3 gate | invented values stripped; figures link to their page | **done** | `TestGate`; live T7, T8 |
| §6 attached harness | opencode completes the reference task, offline-clean | **measured** | live T8, scored by stopping rule 1 over 5 runs |
| §14 detached engine | engine starts under its manifest | **done** | node-shipped llama.cpp build, verified, pinned to the RTX 4060; opencode on it, loopback only (live T14–T15) |
| §10.2 TPM attestation | a verified quote lifts a managed device to A | **done** | native verifier, software TPM with an EK chain (`TestTpmAttestation`; live T13); hardware TPM needs `tss` group access |
| §14 instrument slices | the gate off-site for exported documents | **done** | `TestSlices`; live T16 (the node's re-gate matched the device's) |
| §6.2 own harness build | zero non-node connections (RQ9) | **done on Linux** | `bundle/opencode/`; 100/100 cold starts clean with egress open |
| §11 Windows / macOS egress | apply and verify with WFP / pf | **built** | `.github/workflows/client-platforms.yml` runs them on real runners once pushed |
