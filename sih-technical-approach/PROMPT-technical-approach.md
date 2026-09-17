# Prompt — SIH "Technical Approach" diagram

> **Use this with a model that writes code (Claude, GPT, Gemini) and ask for HTML.**
> Do NOT paste it into an image generator — image models garble small text and
> cannot hold ~40 labels. Take the HTML it returns, open it in a browser at
> 1280×720, screenshot it, and drop the PNG onto the slide.

---

## THE PROMPT (copy everything below this line)

You are producing one presentation slide as a single self-contained HTML file.
It is slide 3 of a 6-slide Smart India Hackathon 2026 idea submission. The
audience is a panel of technical judges who will look at it for about forty
seconds.

### Output format

- ONE HTML file. No external files, no CDN links, no frameworks, no build step.
- Root element exactly 1280×720 px, `position: relative`, white background.
- All icons drawn as inline `<svg>`, stroke-based, 24×24 viewBox, `stroke-width:
  1.75`, `stroke-linecap: round`. No emoji, no icon fonts, no images.
- All CSS inline or in one `<style>` block. Fonts: `Arial` for body,
  `"Times New Roman"` for the slide title, `Consolas` for technical tags.
  These three only — they must render identically on a venue machine.
- Return the file and nothing else. No explanation, no commentary.

### The design system (non-negotiable — it comes from the rest of the deck)

Palette. Exactly three colours carry meaning; everything else is neutral.

| Hex | Meaning | Where |
|---|---|---|
| `#A8302B` | **we build this ourselves** | solid fill, white text |
| `#5C6A75` | off-the-shelf, we deploy it | solid fill, white text |
| `#C00000` | a warning or a refusal | thin accents, the ✗ mark |
| `#1A1A1A` | body text and rules | |
| `#6B7076` | secondary text | |
| `#C4C8CC` | box borders | |
| `#F1F2F3` | light fills | |
| `#1F6FB5` | footer bar only | |

Type scale: slide title 30pt Times New Roman bold, band headings 12pt bold,
block titles 10.5pt bold, body 9pt, small caps labels 7pt bold with 0.9pt
letter-spacing, technical tags 7.5pt Consolas.

Rules of the house style:
- Square corners. No border-radius, no gradients, no drop shadows.
- A band heading is followed by a 1.25pt horizontal rule spanning its width.
- Component names and licences are set in Consolas — mono means "machine fact".
- One boxed one-line claim, black 1.25pt border, centred bold text.
- Numbered items use `01 02 03` in Consolas, not circles or bullets.

### Layout — three horizontal bands

Do not use two side-by-side panels. The slide reads top to bottom in three
bands, and the architecture band reads left to right.

**Header (y 0–92).** Purple 1.25pt outlined oval at x 44, 118×52, containing
"Dilijens". Slide title "TECHNICAL APPROACH" centred, Times New Roman bold 30pt,
1.2pt letter-spacing. Empty 152×56 slot at the top right for a logo. Below the
title at x 44: `WHAT SITS ON THE BOX, AND WHY IT FITS ON ONE` in 8.5pt bold
`#C00000`, 1pt letter-spacing.

**Band 1 — the architecture (y 104–394).** Two headings with rules: "The plant
network" over the left column, "The workbench — one on-premise server" over the
rest. A left-to-right flow with visible arrows:

```
clients ─→ agent loop ─┬─→ approval gate ─→ tool surface ─→ ✗ │ fence
                       └─→ admission router ─→ model plane      │
scheduler & state                                               │
```

- **Clients** (w ≈ 168): four light-filled rows, each a 16px line icon plus a
  label — a monitor, a tablet, a clock, a stacked server. Below them, in
  Consolas: `browser · CLI · REST`.
- **Agent** (w ≈ 232): a white bordered box titled `AGENT LOOP`, containing four
  small outlined pills reading plan › act › observe › repeat, and a Consolas tag
  `goose · MCP tools`. Beneath it a second box, `SCHEDULER & STATE`, with the
  line "cron · long runs · replayable history" and the tag `Temporal · Postgres`.
- **The checks** (w ≈ 208): two solid `#A8302B` blocks with white text.
  `APPROVAL GATE` / "+ provenance" / "Nothing acts or cites unchecked." and
  `ADMISSION ROUTER` / "Residency-aware, over the LiteLLM gateway." Each carries
  a small Consolas tag reading `we write it`.
- **Tool surface** (w ≈ 308): a small caps label, then four solid tiles in a 2×2
  grid, each a white line icon plus a label — code sandbox (`#A8302B`, because we
  build it), headless browser, plant knowledge, documents out (all `#5C6A75`).
- **Model plane**: a small caps label, then two white bordered boxes —
  **FreeToken** / "MoE generalist" and **vLLM** / "small specialists", each with a
  grey `Apache-2.0` chip in Consolas.
- **The fence** (far right): a 5px solid vertical bar in `#A8302B` running the
  full height of the band. An arrow leaves the tool surface, meets a red `✗`, and
  stops. Beside the bar: `THE FENCE` in bold, then in Consolas `default-DROP` and
  `nftables · Tetragon`, then the sentence "Everything left of this line stays
  there. Nothing dials out — the kernel refuses."
- Top right of the workbench heading, a 4-word legend: an 8px `#A8302B` square
  followed by "red is what we write". That is the entire legend.

**Band 2 — the inversion (y 404–538).** Heading "The inversion — nothing swaps,
on either machine" with a rule. Three cards side by side, each with a Consolas
number, a small caps tag, a row of Consolas mini-boxes showing the memory
topology, and one sentence.

- `01` — tag "THE OBVIOUS BUILD", greyed out (fill `#F5F6F7`, all text `#6B7076`)
  because it is the option being rejected. Topology: a box reading `GPU`, then
  `model A ⇄ model B`. Sentence: "One model fits. The swap costs 30–100 s, every
  agent step."
- `02` — tag "OURS · DISCRETE CARD" in `#C00000`, white card. Topology:
  `HOST RAM` → `PCIe` → `GPU · hot + KV`. Sentence: "Experts stream across the
  bus. The transfer is the cost."
- `03` — tag "OURS · UNIFIED MEMORY" in `#C00000`, white card. Topology: one
  full-width box reading `ONE POOL · CPU + GPU · KV`. Sentence: "No PCIe hop.
  Nothing to transfer — the pool is the ceiling."

**Band 3 — the claim (y 548–594).** A full-width box, black 1.25pt border, no
fill, containing one centred bold 12.5pt sentence: "The scarce resource stops
being VRAM. It becomes memory the GPU can reach, and how fast." Right-aligned
beneath it in 8pt grey: "First run measures which of the two this machine is."

**Footer (y 648–686).** Full-width `#1F6FB5` bar, white 9pt centred text
"@SIH Idea submission- Template", and "3" right-aligned in white bold.

### Copy rules

Every sentence above is final. Use it verbatim. Where you must write anything
new, match this voice:

- Short declarative sentences. Plain Anglo-Saxon words. Full stops, not commas.
- Concrete nouns and verbs. Numbers with units, never rounded up.
- Understatement over emphasis. "The kernel refuses" beats "robust enforcement".
- Never: leverage, seamless, robust, empower, cutting-edge, state-of-the-art,
  revolutionise, holistic, streamlined, comprehensive.
- Never a triplet of parallel clauses ("every X, every Y, every Z") — it is the
  single clearest tell of machine-written copy.

### Hard constraints — these are the failure modes, do not repeat them

1. **Never nest a box inside a box inside a box.** Maximum one level. In
   particular, do not draw a large rectangle around the whole system and label it
   "SEALED". The boundary is a *line* at the right edge, not a container.
2. **Never let one colour carry two meanings.** Red means "we build this". It does
   not also mean "heading", "arrow", or "emphasis".
3. **No sentence-long legend.** No "Red = we write it. Grey = we install it."
   No note explaining what the drawing does or does not cover.
4. **Every arrow must be drawn.** If two things are connected, show the arrow. A
   stack of boxes with no arrows is not a flow.
5. **No component appears twice.** Say each thing once, in one place.
6. **Invent nothing.** No metrics, no component names, no capabilities beyond
   what is written above.
7. **Nothing smaller than 7pt**, and no text overlapping any other element.
8. Fill the canvas but leave air. If a block looks empty, that is correct — do
   not pad it with filler text.

Return the HTML file.
