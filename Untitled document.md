  
The Air-Gapped Workbench  
A survey of what already exists, what quietly does not, and a restatement of the problem that survives contact with the hardware.

Survey date · 24 Aug 2026  
71 repositories examined  
Status · problem definition  
§0 — Thesis  
Almost every layer this problem statement asks for is already commodity open source. The project is not a research problem; it is an integration problem with four load-bearing exceptions. Those four exceptions are the entire project, and three of them are invisible in the problem statement as written.

Inference serving, agent loops, OCR, RAG, document generation, guardrails and tracing are all solved to production quality under permissive licences. You can assemble a system that summarises a PDF and writes a Word file in a weekend. What you cannot assemble is the part the problem statement treats as an afterthought: fitting frontier-scale intelligence onto one mid-range GPU without thrashing it, a code sandbox that works with no internet, reading an engineering drawing, and proving the negative — that nothing left the building.

What follows is the evidence for that claim, the traps that cost time if you find them in week three instead of day one, and a one-day experiment that settles the architecture before any product code is written.

§1 — What is actually being asked  
The statement decomposes into eight capability layers and one proof obligation. The proof obligation is not a layer — it is a different kind of requirement, and it is the one the statement singles out as "the actual proof of the sovereign claim, not just a statement of it." Treat it as the acceptance test, not a feature.

Serve open-weight models locally on the organisation's own GPU.  
Host several models at once and add new ones later without redesign.  
Select the model automatically per task — coding handled differently from summarisation.  
Act as an agent: plan multi-step work, call tools, iterate rather than answer once.  
Execute code in a sandbox, verifiably.  
Read non-text input: scanned PDFs, handwriting, photographs, engineering drawings.  
Emit real deliverables: Word, Excel, PowerPoint, working code, worked calculations.  
Ground answers in the organisation's own manuals, SOPs and correspondence.  
Proof obligation. Demonstrate, through logs or a visible network monitor, that no external call is made at any point.

§2 — Stack survey  
What the ecosystem already gives you  
Star counts, licences and last-push dates were read from the GitHub API on 24 August 2026\. The verdict column is about fitness for this deployment — air-gapped, on one GPU, in a government or PSU procurement context — not about general quality.

Tag	Layer	Best available	Stars	Licence	Last push	Verdict  
L1	Inference serving	vLLM · llama.cpp · Ollama · SGLang	89.9k / 125k / 179k / 32k	Apache-2.0 / MIT	2026-08-24	Commodity  
L2	Big models, one GPU	FreeToken · vLLM sleep mode · llama-swap	3.9k / — / 5.5k	Apache-2.0 / MIT	2026-08-24	v0.1.2 — changes the sizing math  
L3	Task-aware routing	RouteLLM · semantic-router · Plano	5.4k / 3.8k / 7.0k	Apache-2.0 / MIT	2024-08-10 / 2026-08-10 / 2026-08-19	Weakest layer  
L4	Agent harness	goose · OpenHands · opencode · pydantic-ai	53k / 85k / 201k / 19k	Apache-2.0 / MIT	2026-08-24	Commodity — pick one  
L5	Code sandbox	E2B ✗ · gVisor · Firecracker · Kata · microsandbox	13.5k / 19k / 36k / 8.6k / 7.9k	Apache-2.0	2026-08-24	You build the packaging  
L6	Document & OCR	Docling · MinerU · PaddleOCR-VL 1.5 · Surya	65k / 78k / — / 21k	MIT / Apache+ / Apache-2.0	2026-08-24	Commodity  
L7	Engineering drawings	Azure P\&ID sample — then nothing	132	MIT	2026-01-27	Open research  
L8	Knowledge base / RAG	Onyx · RAGFlow · LightRAG · Haystack	32k / 89k / 39k / 26k	MIT+ee / Apache-2.0 / MIT	2026-08-24	Commodity  
L9	Deliverable generation	python-docx · python-pptx · openpyxl · LibreOffice	5.7k / 3.5k	MIT	2026-08-01 / 2024-08-07	Commodity — fidelity is the work  
L10	Guardrails & PII	NeMo Guardrails · Presidio · LLM Guard	7.0k / 10.6k / 3.2k	Apache-2.0 / MIT	2026-08-21	Usable  
L11	Tracing	OTel → SigNoz · Phoenix · Langfuse	32k / 11k / 34k	mixed \+ ee dirs	2026-08-24	Commodity via OTel  
L12	Egress proof	nftables · Falco · Tetragon	— / 9.3k / 4.9k	Apache-2.0	2026-08-24	Primitives only — nobody packages the proof  
L13	User interface	Open WebUI · LibreChat · AnythingLLM	150k / 42k / 65k	see T2 / MIT / MIT	2026-08-24	Licence decides this one  
assemble  
assemble with care  
you build it  
The seven repositories you supplied  
Each is real, active and worth reading. Their roles in this build differ sharply from their reputations.

litellm (57.1k, MIT core) — the right model gateway. One OpenAI-shaped endpoint over every local backend. Caveat in T3.  
qm (14.1k, MIT, created 29 Jul 2026\) — the best architectural reference in the list: headless core, Postgres for sessions and memory, a small fixed tool surface with one execute tool into a per-scope durable sandbox, and three org-wide security postures where Strict pauses every tool call for human approval. Read it for the shape; note it is four weeks old and targets Fly and AWS.  
ClawManager (1.9k, MIT, Go \+ React \+ MySQL) — a Kubernetes-native control plane with a governed AI gateway, per-instance quotas, approval workflow and a full audit console. This is what the administrative half of a PSU deployment looks like. Heavy, but it shows the surface area an operator actually demands.  
NeMo Guardrails (7.0k, Apache-2.0) — genuinely Apache-licensed, Colang policy runtime, runs locally. Use it for the "don't put a number in an approval note you can't cite" class of rule.  
e2b (13.5k, Apache-2.0) — disqualified. See T1.  
lms (5.2k, MIT) — the CLI is MIT; LM Studio itself is a closed-source desktop application. Fine on a developer laptop, wrong for a server-grade multi-GPU deployment and awkward in procurement.  
logfire (4.4k, MIT SDK) — the SDK is MIT and speaks OpenTelemetry; the platform that stores and displays the data is closed source, and self-hosting requires an enterprise licence. Use the SDK as an OTel emitter, point it at an open backend.  
FreeToken — the find that moves the design  
FlashML-org/FreeToken (3.9k stars in five weeks, Apache-2.0, created 20 Jul 2026\) is an edge-native MoE serving engine from Berkeley and MIT — Shuo Yang, Song Han, Kurt Keutzer, Matei Zaharia and Ion Stoica among the authors. It keeps MoE experts in host RAM and streams them over PCIe, with a bandwidth-adaptive policy (q\*) that splits each step between PCIe fetch and CPU compute.

The measured results reset the model-sizing conversation. On an RTX 5090 it sustains 77–83 tok/s on Qwen3.6-35B-A3B and 22–25 tok/s on DeepSeek-V4-Flash, 1.5–2.3× the strongest baseline. On an 8 GB RTX 4060 laptop it serves a 35B model at 39.3 tok/s. On a 32 GB gaming desktop it serves 284B interactively. On a single 96 GB RTX PRO 6000 it serves the 753B GLM-5.2 at twice llama.cpp's throughput. Worst-case time-to-first-token stays under 44 s across four agentic workloads, where every baseline blows past 150 s somewhere — llama.cpp at 232 s, Ollama at 179 s, KTransformers at 946 s.

Two details matter more than the headline. It is benchmarked on agentic traces, not single-turn prompts, and it holds its rate there while KTransformers loses 31%. And it exposes the memory scheduler as an API: POST /v1/cache/rebuild re-partitions VRAM between the expert cache and the KV pool live, with no restart, while GET /v1/stats reports throughput, latency, VRAM and pool occupancy. That is the control surface a VRAM-aware router needs, already built.

Air-gap verdict: clean, and better than most of this survey. Wheels install from local files, a companion kernel-cache wheel ships prebuilt .so files so nothing calls nvcc at runtime, and FREETOKEN\_DISABLE\_JIT=1 makes any cache miss fail immediately rather than compile. Models load from local paths. It serves both the OpenAI and Anthropic APIs on localhost, so LiteLLM, goose and opencode attach unchanged. The costs are in T8.

§3 — Traps  
Solved-looking things that are not solved here  
Each of these looks like a dependency you can add. Each fails specifically because of air-gap, on-premises scale, or government procurement — the three conditions this problem statement imposes.

T1  
E2B cannot be air-gapped  
The sandbox layer named in the brief is the one component that structurally cannot meet the brief. Self-hosting E2B is Terraform-deployed to AWS or Google Cloud; a general Linux machine is an explicitly unchecked box on their own list. Worse, the prerequisites are not merely cloud-shaped — they are network-shaped.

e2b-dev/infra · self-host.md · Prerequisites → Accounts:  
· Cloudflare account  
· Domain on Cloudflare  
· PostgreSQL database

Supported cloud providers: AWS ✓ · GCP ✓ · Azure ☐ · General Linux machine ☐  
An air-gapped refinery has no Cloudflare account and no domain. Budget for building the sandbox on primitives — gVisor, Kata Containers, Firecracker or microsandbox — and do not discover this in week three.

T2  
Open WebUI cannot be white-labelled above fifty users  
The default answer to "we need a chat UI" carries a branding clause that a PSU deployment will breach on day one, and it is written as a material condition of the licence rather than a request.

open-webui/open-webui · LICENSE · clause 4:  
"licensees are strictly prohibited from altering, removing, obscuring, or replacing any 'Open WebUI' branding … except … (i) deployments where the total number of end users … does not exceed fifty (50) within any rolling thirty (30) day period"  
A refinery rolling this out to its engineering department exceeds fifty users immediately. LibreChat (MIT) and AnythingLLM (MIT) carry no such clause. goose publishes an explicit custom distributions path for preconfigured, rebranded builds. If the deliverable is a sovereign product with the organisation's own name on it, that choice is made for you.

T3  
Enterprise directories gate exactly what a PSU requires  
LiteLLM, Onyx and Langfuse all ship an MIT or Apache core alongside an enterprise/ or ee/ directory under a commercial licence. The features behind that boundary are consistently SSO and SAML, audit logging, JWT auth and fine-grained RBAC — which is to say, precisely the list a government security review will treat as mandatory.

BerriAI/litellm · LICENSE:  
"All content that resides under the 'enterprise/' directory … is licensed under the license defined in 'enterprise/LICENSE'" — production use requires a valid seat-counted subscription.  
This is not a reason to avoid these projects. It is a reason to decide early whether identity and audit are things you buy, or things you build in your own control plane. Dify adds a second flavour of the same problem: its modified Apache licence forbids removing the console logo and forbids multi-tenant operation without written permission.

T4  
Some routers phone home by default  
Plano — the Envoy-based agent dataplane, formerly archgw, and the most credible routing product in the survey — states plainly that its routing models are hosted for you unless you take action.

katanemo/plano · README:  
"Plano and the Plano family of LLMs (like Plano-Orchestrator) are hosted free of charge in the US-central region to give you a great first-run developer experience."  
Running them locally is supported and documented. The trap is that the default configuration is a live external dependency in a system whose entire claim is that it has none. Every dependency needs the same audit: what does it do on first run, with no configuration?

T5  
RouteLLM has not been touched in two years  
The most-cited open-source model router — 5,391 stars, Apache-2.0, from LMSYS — last received a commit on 10 August 2024\. It is a research artefact, not a maintained component. Every current survey still lists it as a leading option; none mentions the date.

The maintained alternatives are semantic-router (3.8k, intent classification over embeddings) and Plano. Neither is what this problem actually needs, for the reason set out in H1.

T6  
Daytona no longer ships a licence  
A 71.9k-star sandbox project that appears in every "E2B alternative" list now has a repository containing a README and an assets folder, with no LICENSE file and no source. Absent a licence grant, the default is all rights reserved. Verify the licence file exists in the tree you actually depend on, not the badge in the README.

T7  
MinerU has revenue thresholds; Grafana Tempo is AGPL  
Neither is disqualifying — MinerU's commercial licence triggers at 100M monthly active users or USD 20M monthly revenue, which no PSU pilot approaches, and AGPL is acceptable for internal deployment that is not offered as a network service. Both belong in the compliance annexe that a procurement review will ask for, and that annexe is far cheaper to write as you go than to reconstruct at the end.

T8  
FreeToken's autoconfig starves the KV cache, and it fails by hanging  
The engine that makes this design possible is at v0.1.2, five days old at time of survey, with one release and 118 open issues. That is acceptable for research software. What is not acceptable without mitigation is where the known bugs sit: squarely in the configuration this project would run.

FlashML-org/FreeToken · open issues · 23–24 Aug 2026:  
· "Requests longer than the KV pool are queued forever with no error — and \--moe-cache-auto leaves only \~8k tokens of KV on a 96GB GPU"  
· "Request hang (silent, zero-log) with hybrid\_radix cache \+ moe-backend offload \+ nvfp4 triton backend — reproduced on 2 independent models"  
· "DSV4 ignores configured prefill limit and retains stale budget after SWA rebuild"  
An agent loop accumulating tool results routinely exceeds 8k tokens, and the failure mode is not an error — it is a request that never returns and logs nothing. On stage that is indistinguishable from a crash. Pin \--kv-reserve-tokens explicitly, never ship \--moe-cache-auto, and put a hard client-side timeout on every generation call. Verify the fix on your own hardware; do not assume the issue is closed because a version number moved.

Two further constraints from the same source. "Multimodal checkpoints are served text-only" — FreeToken does no vision, so the multimodal requirement needs a second engine whose VRAM competes with the expert cache. And it requires Linux x86\_64 with CUDA 13 and driver r580+; in a locked-down PSU environment a driver upgrade is a change request with a lead time, not a command.

§4 — The real problems  
What is actually hard  
H1  
Model auto-selection on one GPU is a memory-scheduling problem, not a classification problem  
The brief frames routing as picking the right model for the task. On a single GPU that framing is wrong, and following it literally produces a system slower than not routing at all.

The naive reading goes: a 24 GB card holds roughly one 30B-class model at 4-bit with usable context, not two, so a routing decision is really an eviction — historically 30 to 100 seconds per switch. An agent loop that re-routes per step would spend its run swapping. vLLM sleep mode softens this, keeping the process, allocator and CUDA graphs alive while weights are discarded, with wake times near 0.1–0.8 s.

But swapping is the wrong problem to solve. FreeToken's results say the better move on one GPU is not two models taking turns — it is one very large sparse model that never leaves, with its experts resident in host RAM and streamed on demand. A 32 GB desktop serves 284B that way; a 96 GB workstation serves 753B. Nothing swaps, so swap latency stops being a design constraint at all.

That reshapes the architecture into one big MoE generalist, always resident, plus small always-resident specialists — a vision-OCR model, an embedder, a reranker. The brief's "auto-selection across at least two task types" is then satisfied by routing between the generalist and those specialists, which costs nothing, demonstrates honestly, and cannot fail on stage. It also dissolves the brief's own hedge about using a smaller model if 120B-class hardware is unavailable: on this design, mid-range hardware is not a reason to downgrade the model.

The scarce resource moves — and so does the procurement conversation. The binding constraints become host RAM capacity and PCIe bandwidth, not VRAM. FreeToken's evaluation machines carry 180 to 512 GB of system memory, and their measured PCIe throughput spans 11.8 GB/s on an x8 laptop link to 52.7 GB/s on PCIe 5.0 x16 — a four-fold spread that sets decode speed directly. For a PSU this is unusually good news: DDR5 costs a fraction of HBM per gigabyte. The right spec question stops being "how many GPUs" and becomes "how much system RAM, and how many PCIe lanes at which generation." Almost nobody writes that into an AI server tender.

The objective function is different on-premises. Every commercial router optimises cost per token and reports 20–60% savings. On your own GPU the marginal cost of a token is electricity you are already paying. What is scarce is memory and wall-clock. The correct on-prem router minimises residency churn — expert-cache thrash and KV pressure — not rupees, and it decides at task admission rather than per step. No off-the-shelf router has that objective, and FreeToken hands you the two things needed to build one: live VRAM re-partitioning between expert cache and KV pool through /v1/cache/rebuild, and occupancy telemetry through /v1/stats. This is the one place the project has to think for itself, and it is defensible novelty.

H2  
You cannot prove a negative by watching  
The brief asks for logs or a network monitor showing no external calls. A packet capture showing zero egress during a five-minute demo proves only that nothing happened during those five minutes. It is the weakest possible form of the claim, and a sharp reviewer will say so.

The strong form inverts it: make egress structurally impossible, then show the record of what tried and was refused. A default-DROP nftables policy with logging, plus an eBPF process-level monitor on connect() — Falco or Tetragon — produces an artefact that is non-empty, and a non-empty denial log is far more persuasive than an empty capture.

Design the demo around this. Have the system deliberately attempt an outbound call during the run and show it blocked, attributed to a process, and written to an immutable audit record. That is the moment that sells the sovereignty claim.

H3  
Engineering drawings are the one genuinely unsolved capability  
The document pipeline is a solved problem: PaddleOCR-VL 1.5 reaches 94.5% on OmniDocBench v1.5 at 0.9B parameters, outperforming general vision models several hundred times its size. Scanned inspection reports, handwritten notes and photographs are all commodity.

P\&IDs are not. The most substantial open-source implementation is a 132-star Azure sample; below it are student projects at 11, 7, 3 and 0 stars. The published research — Digitize-PID from TCS Research, SynthPID, and a body of IEEE work — establishes that the task decomposes into symbol detection, text association and line tracing to recover connectivity, and that last part is a graph problem, not a captioning problem. A vision model will describe a P\&ID fluently and get the connectivity wrong, which in this domain is worse than refusing.

Decide deliberately whether P\&ID understanding is in scope. If it is, scope it to symbol and tag extraction against a fixed legend — tractable with a YOLO-class detector on a small annotated set — and state explicitly that connectivity recovery is out of scope. If it is not, use a scanned inspection report for the multimodal demo and say why.

H4  
A confidently wrong approval note is worse than no approval note  
This is the difference between a chat assistant and an instrument. In a refinery approval workflow, a summary that transposes a pressure rating or attributes a finding to the wrong equipment tag does not degrade gracefully — it propagates into a signed document. Generic RAG returns plausible text; the requirement here is that every extracted number carries a pointer back to the page and region it came from, and that the system declines rather than interpolates.

Treat provenance as a correctness requirement enforced at generation time, not as a citation feature bolted on afterward. This is the natural job for the guardrails layer, and it is also the most credible thing to show a domain reviewer.

H5  
Deliverable fidelity is the work, not the library  
python-docx and python-pptx write valid files in an afternoon. What an organisation means by "an approval note" is their approval note: their letterhead, their clause numbering, their signature block, their reference format. The gap between a valid .docx and an acceptable one is template engineering and is invisible in any library's README. Budget for it, and get one real template from a real organisation early — an authentic form is worth more in a demo than any amount of model quality.

§5 — Restatement  
The problem, stated so it survives the hardware  
Build a self-hosted agentic workbench for confidential industrial knowledge work whose scarce resources are memory and bandwidth rather than money — so that model selection is a residency decision made once per task, not a cost decision made per token; whose sandbox, knowledge base and observability stack have no cloud control plane anywhere in them; whose non-text understanding is honest about the difference between a scanned report it can read and an engineering drawing it cannot; whose deliverables are the organisation's own document templates rather than generic files; and whose central claim — nothing leaves the premises — is demonstrated by a denial log rather than an empty packet capture.

Everything in that sentence is buildable in a hackathon window. None of it is what you get by wiring together the obvious components.

§6 — Boundary  
Assemble versus build  
The single most valuable decision available is refusing to build what already exists. The survey supports a sharp line.

Assemble — do not write this  
FreeToken for the resident MoE generalist; vLLM or llama.cpp for the small specialists  
LiteLLM as the gateway (MIT core only)  
An existing agent harness — goose is Apache-2.0, Linux Foundation governed, MCP-native, has an Ollama provider and supports branded custom distributions  
Docling or MinerU plus PaddleOCR-VL for documents  
Postgres with pgvector; a reranker  
python-docx / python-pptx / openpyxl  
NeMo Guardrails and Presidio  
OpenTelemetry into any open backend  
Build — this is the project  
The residency-aware admission router, over /v1/stats and /v1/cache/rebuild (H1)  
An air-gapped sandbox on gVisor or Firecracker (T1)  
The egress-denial proof harness (H2)  
Provenance-enforced generation (H4)  
Organisation document templates (H5)  
Optionally, legend-scoped P\&ID symbol extraction (H3)  
§7 — Settling experiment  
The one measurement that decides the architecture  
The whole design pivots on three numbers nobody has yet, and all three are properties of your machine rather than of any model. Measure them before writing product code. It takes a day, it can only be done on the target hardware, and the tooling already exists — FreeToken ships the benchmarks.

Probe  
Bandwidth. Run ft bench bw. It writes the profile the engine reads and tells you whether this machine is PCIe-bound or CPU-bound — the difference between the offload and hybrid backends. Record host RAM bandwidth and PCIe generation and width; these set the ceiling on everything downstream.  
Decode rate under the real backend. Run bench\_decode\_moe.py \--backend offload,cpu,hybrid against your candidate generalist. Compare against 33 tok/s, the median decode speed of a production coding agent — below that, the workbench feels broken regardless of answer quality.  
KV headroom, which is the one that will bite. With the generalist and the vision model both loaded, query /v1/cache/status and record how many KV tokens actually remain. Then run a real agent trajectory — ten tool calls with document text in the results — and confirm it completes rather than queueing forever (T8). Pin \--kv-reserve-tokens until it does.  
Co-residency. Vision runs on a second engine whose VRAM competes with the expert cache. Load both, then re-run step 2\. The delta is the true cost of the multimodal requirement, and it is currently unmeasured by anyone.  
Floor. Run the end-to-end demo task once, single model, no routing. That wall-clock is the number every later optimisation is judged against.  
Decision gate  
\> 40 tok/s, KV holds a full trajectory  
Ship the big-generalist design. Route between generalist and specialists; nothing swaps.  
20–40 tok/s, KV holds  
Viable, but the demo must be scripted around latency — batch the document work, stream visibly, never leave dead air.  
KV starves or trajectories hang  
Drop to a smaller generalist until a ten-call trajectory completes three times running. Context that survives the loop beats parameters that do not.  
\< 20 tok/s, or unstable  
Abandon the streamed-MoE path. Fall back to a resident 30B-class dense model on vLLM plus small specialists. Same architecture, smaller generalist, no v0.1.2 risk on stage.  
That last branch is worth stating plainly in advance, because it is the branch teams refuse to take. A demo where a small vision model and a general reasoning model are selected automatically and correctly is a passing demo. A demo where a 284B model hangs silently for ninety seconds is a failing one, whatever the architecture diagram claims — and per T8, silent hangs are a documented failure mode of the exact configuration that makes 284B possible.

Stopping rule  
Fix a date. If by that date the end-to-end path — scanned inspection report in, findings extracted, approval note out as a .docx — does not complete unattended in at least three of five consecutive attempts on the target hardware, cut engineering-drawing understanding entirely and ship the document path alone. One capability that works without supervision beats three that need a rehearsed operator.

§8 — Open  
What would change these conclusions  
The machine — and it is no longer just the GPU. Three numbers now drive H1: VRAM, host RAM capacity, and PCIe generation and width. A 24 GB card behind PCIe 5.0 x16 with 256 GB of DDR5 is a fundamentally more capable workbench than the same card behind an x8 link with 32 GB, and the second configuration is the one people accidentally buy. Get these three numbers for both the dev box and the venue before committing to a model.  
Whether the venue tolerates a driver upgrade. FreeToken needs CUDA 13 and driver r580+ on Linux x86\_64. If the demo machine is not yours to reconfigure, this is a gating question, not a detail.  
Whether the UI must carry the organisation's name. If yes, T2 removes Open WebUI. If a fifty-user pilot is genuinely the scope, it returns as the fastest option available.  
Whether a real document template and a real drawing can be obtained. Authentic artefacts change what the demo is worth more than any model choice does. Synthetic ones are workable and should be labelled as such.  
Survey conducted 24 August 2026 · Repository metadata read from the GitHub API on that date · Licence texts read from repository trees, not from badges