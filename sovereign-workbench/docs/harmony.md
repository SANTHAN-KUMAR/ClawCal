# Serving gpt-oss: the harmony format, and the bug that makes it look broken

## Symptom

gpt-oss-20b served through Ollama returns empty or near-empty completions, slowly,
and appears far less capable than its benchmarks suggest. Wired into an agent
harness it produces nothing usable.

## Measurement

Measured on this deployment (RTX 4060 Laptop 8 GB, gpt-oss-20b MXFP4 via Ollama
0.17.5, 2026-09-09). Identical prompt, identical sampling parameters; the only
difference is the last few tokens of the prompt.

| Prompt ends with | Wall clock | Tokens emitted | Output |
|---|---|---|---|
| `<\|start\|>assistant<\|channel\|>final<\|message\|>` | 58.1 s | 2 | `391` — frequently empty |
| `<\|start\|>assistant` | **1.6 s** | 38 | analysis channel, then `391` |

A 36× difference in latency, from four tokens of prompt.

## Cause

gpt-oss is trained on the OpenAI **harmony** response format, in which an
assistant turn opens by declaring a channel:

```
<|start|>assistant<|channel|>analysis<|message|>…reasoning…<|end|>
<|start|>assistant<|channel|>final<|message|>…answer…<|return|>
```

The model wants to open `analysis` first and reason before committing to an
answer. Pre-filling `<|channel|>final<|message|>` forecloses that: the model is
forced to begin the answer having done no reasoning, in a state its training
distribution barely covers. It stalls, emits a token or two, and stops.

This is easy to get wrong because the pre-filled form looks like the obvious
optimisation — it appears to skip the "wasted" reasoning tokens. It does the
opposite.

## Fix

End the prompt at `<|start|>assistant`, let the model choose its own channel, and
**parse** the channels out of the completion.

Implemented in `backend/sovereign/gateway/adapters.py::HarmonyAdapter`:

* `render()` builds the harmony prompt and stops at `<|start|>assistant`;
* `parse()` splits the completion on `<|channel|>` markers and routes
  `analysis` → the reasoning field (never shown as an answer, never written into
  a deliverable), `commentary to=functions.X` → a tool call, `final` → the answer;
* stop sequences are `<|return|>` and `<|call|>`, not `<|end|>` — `<|end|>`
  terminates the *analysis* message, so stopping there truncates the model
  before it produces its answer.

## Tool calls in harmony

Harmony tool calls arrive on the commentary channel, addressed to a namespace:

```
<|channel|>commentary to=functions.calculator <|constrain|>json<|message|>
{"expression":"16-12"}<|call|>
```

Tools are declared to the model as a TypeScript-shaped namespace in the
`developer` message, not as a JSON schema block. `_harmony_tool_decl()` renders
a `ToolSpec` into that form. Verified working end to end: the model emits a
correct `calculator` call for "design pressure 16 bar, operating 12 bar, what is
the margin?".

## The system / developer split

Harmony distinguishes two roles that most chat formats collapse into one:

* `system` — identity, current date, the reasoning-effort switch
  (`Reasoning: low|medium|high`) and the channel declaration;
* `developer` — the actual instructions and the tool namespace.

Putting instructions in the `system` message instead of `developer` degrades
instruction-following noticeably. The adapter routes both `system` and
`developer` messages from the rest of the codebase into the harmony `developer`
message, and synthesises the harmony `system` message itself.

## Why this lives behind the Model Gateway

Nothing above `gateway/` knows any of this. `ModelCard.prompt_adapter` selects
`harmony`, `qwen` or `chat`, and a model with a different prompt protocol is a
registry row rather than a code change. The same seam is what lets an empty
completion be treated as a *failure* — `GenResult.empty` marks it, the circuit
breaker counts it, and the fallback chain serves the request from another model
rather than returning a blank answer to the operator.
