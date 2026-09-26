# The dirty-corpus evaluation

The team's own tests pass, and the rule this project holds itself to is that
those tests say little about the real world. The synthetic P&ID scored F1 1.00
and the first real sheet scored 0.27 (`drawings-real-world.md`). This is the
same exercise for every input path.

The per-reader tables are generated from the result files, not typed:
**`dirty-eval-results.md`** (`python3 scripts/eval_report.py`).

---

## The corpus

`scripts/fetch_dirty_corpus.py` builds it under `$SOVEREIGN_DATA_DIR/dirty`.
Nothing is committed: the documents belong to their authors, and several are
licensed for research only. That applies to FUNSD, IAM and RVL-CDIP; each
manifest item records its terms.

| class | what it is | ground truth |
|---|---|---|
| scans | 12 FUNSD forms (real scanned business forms), plus fax / skew / low-resolution degradations of three of them, plus RVL-CDIP pages | every word, from the dataset |
| photographs | 10 CORD receipts (phone photos); 15 Wikimedia Commons rating plates and gauges | receipt totals and line items from the dataset; none for Commons |
| handwriting | 10 IAM lines, 8 IAM paragraphs, 3 RVL-CDIP handwritten pages | transcriptions from the dataset |
| spreadsheets | 5 EIA and 3 UK DESNZ public workbooks: merged two-row headers, stacked tables, units in title rows, suppressed cells | 31 cell facts, each verified by opening the file |
| cad | the local PID-Symbols DWG and PDF, real P&ID PDFs (AWWA, OPITO, PDH), an ISA letter *table* as a negative | a tag list for one sheet; "not a drawing" for the table |
| raster_drawings | the King County WW510 P&ID at four resolutions | its tags; and no edge may be CONFIRMED |
| adversarial | real pages carrying injected instructions: pixels, an invisible text layer, 5 pt text, white-on-white, rows under a spreadsheet's footnotes | the injected text |

Injection detection is also measured on a separate public set, the
`deepset/prompt-injections` test split, and on 47 benign pages for false
positives.

## What is scored

`scripts/eval_dirty.py` runs **the product's own code paths**: ingestion, the
OCR/VLM decision, the drawing pipeline, the spreadsheet tool and the injection
scanner. It does not run a special evaluation path. Each item runs in its own
process with a timeout, so a pathological file costs that item and not the
run. Each model is measured alone on the GPU, with every other model evicted
first.

For every class it reports three numbers, because a score alone rewards
guessing:

* **quality** — word recall on scans; 1 − character error rate on handwriting
  (whitespace around punctuation is not scored, for every reader alike); share
  of receipt values recovered on photographs; cells read correctly on
  spreadsheets; tag recall on drawings;
* **refused** — how often the product declined (DEGRADED / CANNOT DETERMINE);
* **confidently wrong** — how often it presented a wrong reading as usable.
  This is split by the label it carried. A wrong **ESTABLISHED** value is the
  failure that can reach a signed document. A wrong **INTERPRETED** one was
  marked for an engineer to confirm.

The rule for accepting a change (§6.1): a change that raises quality by
lowering refusals, while raising confidently-wrong, is rejected.

---

## What the evaluation changed

Each of these was found by the evaluation and fixed by a general mechanism,
not by tuning to an item.

**OCR thresholds, calibrated.** Tesseract's mean confidence tracked scan
quality well:

- every FUNSD page read at ≥ 70% recovered 62–96% of its words;
- 11 of the 16 below 70% recovered under half;
- the v1 page threshold of 45% accepted a fax-quality page, read at 54%, that
  recovered 7%.

The threshold is now 70, from that measurement.

**Two readers for images.** A CORD receipt read at 89% confidence had two
thirds of its values wrong, so no confidence threshold makes camera-image OCR
trustworthy. On an image, OCR is ESTABLISHED only if an independent VLM
reading agrees with it: 75% of words, and 90% of numbers, so a single wrong
digit fails. Otherwise the VLM's reading is used and labelled INTERPRETED. A
VLM reading that is mostly `[illegible]` is a refusal. Rendered pages of a
scanned PDF are trusted on OCR alone at ≥ 90%, so a 200-page scan does not
cost 200 VLM calls.

**The VLM is chosen by measurement.** Catalogue scores are claims.
`granite3.2-vision-2b`, the smallest document-specialised model, was routed
vision work on its claimed score and size. Measured, it beat OCR on
photographs (0.77 vs 0.48 of values), but was worse than OCR on scans (0.22 vs
0.37 word recall) and on handwriting (CER 0.67 vs 0.37). 72–76% of its
readings of scans and handwriting were wrong. They were labelled INTERPRETED,
so no value was falsely established, but that is low value. The registry's
vision scores now carry the measured figures (see the results file for every
model).

**Injection detection, as classes of attack.** The v1 scanner caught 0% of the
public held-out set. The rewrite catches 17% of the unseen half, with 0 false
positives on held-out benign text and 0 on 47 benign corpus pages. It catches
12 of 12 document injections in this corpus, but that set was used during
development, so it is no longer held out. Free-form chatbot jailbreaks remain
out of reach of pattern rules. The guarantee against them is structural: the
tool policy and the egress layers refuse the *action*, whatever the model was
persuaded of.

**Drawings.** A vector PDF of the ISA instrument-letter table was read as 147
symbols. It is now refused as "not a drawing": its vector content is 588
rectangles and no curves. No real drawing's result changed. Raster drawings
never produce a CONFIRMED edge, at any resolution.

**Spreadsheets.** 31 of 31 truth cells were read correctly through merged
two-row headers, stacked tables and suppressed cells, with the header and unit
attached to each value.

## What could not be measured here

* **Qwen2.5-VL (3B and 7B) did not load under Ollama 0.17.5 on this host.**
  Its memory estimator asks for about 10 GiB at any context, always just above
  what is available. The gateway now records that figure as a measured
  footprint and refuses the load up front. It should load on a GPU with 12 GB
  or more.
* **Server-tier models** (`gpt-oss-120b`, `qwen3-32b`) need a larger GPU. They
  are in the registry, disabled until a backend serves them.
