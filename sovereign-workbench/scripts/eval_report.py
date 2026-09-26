#!/usr/bin/env python3
"""Tabulate dirty-corpus results into docs/dirty-eval-results.md.

Every figure in that file is read from a `results-*.json` written by
`scripts/eval_dirty.py`; nothing is typed by hand. For each reader and class the
most recent run is used, and the file it came from is named, so each number
traces to an artefact.
"""
from __future__ import annotations

import glob
import json
import os
import sys
import time
from pathlib import Path

DIRTY = Path(os.environ.get("CLAWCAL_DIRTY_DIR")
             or Path(os.environ.get("SOVEREIGN_DATA_DIR", Path.home() / ".sovereign"))
             / "dirty")
OUT = Path(__file__).resolve().parents[1] / "docs" / "dirty-eval-results.md"


def _answered(run: dict, cls: str) -> int:
    return sum(1 for r in run.get("results", [])
               if r.get("class") == cls and (r.get("preview") or {}).get("vlm", "").strip())


def latest() -> dict[tuple[str, str], tuple[dict, str, float]]:
    best: dict[tuple[str, str], tuple[dict, str, float]] = {}
    for f in sorted(glob.glob(str(DIRTY / "results-*.json"))):
        data = json.loads(Path(f).read_text())
        ts = data.get("generated", 0)
        for label, run in data.get("runs", {}).items():
            for cls, summ in run.get("summary", {}).items():
                usable = summ.get("crashes", 0) < summ.get("items", 1)
                # A VLM run in which the VLM answered nothing measured nothing.
                if label != "ocr-only" and cls in ("scans", "handwriting",
                                                   "photographs"):
                    if _answered(run, cls) == 0:
                        usable = False
                key = (label, cls)
                if usable and (key not in best or ts >= best[key][2]):
                    best[key] = (summ, Path(f).name, ts)
    return best


def fmt(v, pct=False):
    if v is None:
        return "—"
    return f"{v:.0%}" if pct else f"{v:.2f}"


def main() -> int:
    best = latest()
    classes = sorted({c for _, c in best})
    readers = sorted({r for r, _ in best}, key=lambda r: (r != "ocr-only", r))
    lines = ["# Dirty-corpus results (generated)", "",
             f"Generated {time.strftime('%Y-%m-%d %H:%M')} by `scripts/eval_report.py` "
             "from `results-*.json`. Do not edit by hand.", ""]
    for cls in classes:
        lines += [f"## {cls}", "",
                  "| reader | items | with truth | quality | refused | confidently wrong "
                  "| OCR alone | VLM alone | source |",
                  "|---|---|---|---|---|---|---|---|---|"]
        for r in readers:
            if (r, cls) not in best:
                continue
            s, src, _ = best[(r, cls)]
            ocr = s.get("tess_recall", s.get("tess_cer", s.get("tess_field_recall")))
            vlm = s.get("vlm_recall", s.get("vlm_cer", s.get("vlm_field_recall")))
            kind = ("CER" if "tess_cer" in s else "recall")
            lines.append(
                f"| {r} | {s['items']} | {s['with_truth']} | {fmt(s['quality'])} | "
                f"{fmt(s['refusal_rate'], True)} | {fmt(s['confidently_wrong'], True)} | "
                f"{fmt(ocr)} {kind if ocr is not None else ''} | "
                f"{fmt(vlm)} {kind if vlm is not None else ''} | `{src}` |")
        lines.append("")
    OUT.write_text("\n".join(lines) + "\n")
    print(f"wrote {OUT} ({len(best)} reader×class cells)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
