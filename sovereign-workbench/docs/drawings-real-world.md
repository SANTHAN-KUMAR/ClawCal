# Engineering drawings: what the pipeline actually does on real ones

The synthetic P&ID in `corpus/` scores **F1 1.00 on tags and 1.00 on
connectivity**. That number is close to meaningless on its own: the same code
generated the drawing and its ground truth. This document records what happens on
drawings nobody on this project drew.

Reproduce with `python3 scripts/benchmark_drawings.py --fetch`.

---

## The test set

| Drawing | What it is |
|---|---|
| King County WTD **WW510-P-60003** | a real utility P&ID — effluent chlorine analyser and sampler, Carkeek Wet Weather Treatment Station |
| POL Oil **ALPHA-AA-T200-PL-0101-01** | a real offshore platform plot plan, Alpha Field Development |
| POL Oil **ALPHA-AA-0000-PR-PD-0001** | that project's own P&ID legend sheets |
| PDH Academy course 462, page 15 | a **table** of ISA instrument letters — a negative case |

Drawings are downloaded, not committed; they belong to their authors.

---

## Result

| Drawing | symbols | tagged | precision | recall | F1 |
|---|---|---|---|---|---|
| King County P&ID (2200×1424 native) | 96 | 57 | 0.54 | 0.60 | **0.57** |
| King County P&ID (1440×1080 render) | 58 | 26 | 0.38 | 0.21 | **0.27** |
| Synthetic corpus P&ID (vector) | 12 | 12 | 1.00 | 1.00 | **1.00** |

The middle row and the top row are **the same drawing**, read by the same code.
The only difference is how many pixels it was given.

---

## What was wrong, and what it cost

### 1. The tag pattern could not match a real plant tag

`\d{2,4}` — four digits. Real tags encode area, unit and sequence in one number:
King County uses `BV510544F`, `AIT520282`, `SV510503A`. **Zero tags matched on the
whole sheet.** Teaching examples use `V-204`; operating plants do not. Now 2–8
digits, with ISA 5.1 used as a *generator* for instrument prefixes rather than a
hand-written list that was missing AIT, AE, AY, AX, FXH and FS.

### 2. OCR cannot read plant tags without help

Literal output from the sheet: `BVS10344F`, `BYS10544N`, `5VS105448`, `AX3105C3`.
Tesseract confuses S↔5, O↔0, B↔8, I↔1 in small stencil lettering, systematically.

Two mechanisms now sit on top of it:

* **Position-aware repair.** The tag grammar is rigid, so inside the numeric
  block a letter-shaped glyph is a digit and inside the prefix a digit-shaped
  glyph is a letter. Splits are generated and scored rather than taken greedily —
  `BVS10344F` reads naturally as `BVS`+`10344`+`F`, but `BV` is a prefix the plant
  uses and `S` is a mangled 5. **13 of 15 readings recovered**, up from 1.
* **Snapping to the plant's tag register.** The organisation's equipment register
  is already in the knowledge base, so a reading two characters from exactly one
  registered tag *is* that tag. Ambiguous matches are left alone — returning the
  wrong equipment is worse than returning an unrecognised one. **15 of 15.**

The register is what makes this work. Without it the reading is a guess with a
grammar behind it, and is reported at lower confidence.

One trap found immediately: seeding that register from previously-read *scans*
creates a feedback loop where a misreading becomes a registered tag and then
snaps every later reading onto itself. `BVS-10344F` entered the register that way
inside a single run. The lexicon now comes only from documents the organisation
wrote and from vector drawings, where the text came from the CAD file.

### 3. A page of tables read as a plant full of equipment

Run over the ISA instrument-letter table — a page containing no equipment
whatsoever — the pipeline reported **437 symbols: 161 vessels and 223 valves**.
Every one was a table cell. The plot plan produced 1 449.

That is not a low score, it is a confident description of something that is not
there. Two geometric signals separate the cases: a table's rectangles share edges,
so their boundaries collapse onto a handful of coordinates, and a drawing's
symbols do not. Tabular regions are now located and excluded — which is better
than refusing the page, because every real drawing sheet carries a title block, a
revision table and often an equipment list.

**437 → 2.** The plot plan: 1 449 → 33.

### 4. Shape alone is not evidence

On a scan, lettering, hatching, dimension arrows and speckle all produce blobs
that classify as valves. A symbol is now kept only if something else on the sheet
agrees with it — it carries a tag, or a traced line runs into it. Vector geometry
needs none of this, because there the shape came from the CAD file.

### 5. The binding constraint is scan resolution, and the system now says so

An A1 sheet at 300 DPI is about 10 000 px wide. The King County drawing arrives
at 2 200 px — roughly 66 DPI — with tag text 9 px tall against the ~14 px
Tesseract needs. The 1440×1080 version is 44 DPI and 8 px.

No amount of processing recovers information the scan never had, so the pipeline
measures text height from the recogniser's own word boxes and says:

> INSUFFICIENT SCAN RESOLUTION: text on this sheet is about 9 px tall, below the
> ~14 px OCR needs. At 2200 px wide this is roughly 66 DPI for an A1 sheet.
> Rescan at 300 DPI or higher, or supply the vector PDF.

That sentence is worth more to an operator than another tuning pass.

---

## What this means for a refinery deployment

**Ask for the vector PDF.** CAD systems export one, and connectivity is then read
rather than inferred — the difference between F1 1.00 and F1 0.57. Every P&ID in
a modern plant exists as a vector file somewhere; the scan is a convenience copy.

**Load the tag register first.** It is the single highest-value input, and a
sovereign deployment already has it.

**Scan at 300 DPI or better** where only paper exists. 66 DPI is not a tuning
problem.

**Expect the raster path to give an inventory, not a topology.** It emits zero
CONFIRMED connections by design and says so.

## Honest limits

Of the four drawings tested, only one was a P&ID with process flow — the others
were a plot plan, two legend sheets and a table. A larger evaluation should use
**PID2Graph** (Zenodo 14803338, CC-BY-SA-4.0, graph-level ground truth) or
**Dataset-P&ID** from Digitize-PID; both were identified but not used here
because PID2Graph is a 9.3 GB download and this machine has 24 GB free.

Symbol *classification* is still unmeasured against ground truth. Tag reading and
connectivity are measured; "is that circle a pump or an instrument" is not.
