#!/usr/bin/env python3
"""Score the drawing pipeline against real, publicly available drawings.

The synthetic P&ID in `corpus/` scores 1.00/1.00, and that number means very
little: the same code generated both the drawing and its ground truth. This
script fetches drawings nobody on this project drew, runs the pipeline over
them, and reports what it actually achieves.

The drawings are downloaded rather than committed, because they belong to their
authors. Sources and licences are listed below and printed at run time.

    python3 scripts/benchmark_drawings.py --fetch     # download first (~18 MB)
    python3 scripts/benchmark_drawings.py             # score what is cached
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
CACHE = Path("/tmp/pid-corpus")

SOURCES = [
    ("awwa-pid.pdf",
     "https://www.pnws-awwa.org/wp-content/uploads/2024/06/PIDs.pdf",
     "PNWS-AWWA, 'Process Flow / Instrumentation Drawings'. Contains King "
     "County WTD drawing WW510-P-60003, a real utility P&ID."),
    ("opito-process-flow-pid.pdf",
     "https://kh.aquaenergyexpo.com/wp-content/uploads/2025/02/"
     "Process-Flow-PlDs-Process-Engineering-Drawings.pdf",
     "OPITO / Petroleum Open Learning. Contains a real offshore platform plot "
     "plan and the project's two P&ID legend sheets."),
    ("pdh-462-pid.pdf",
     "https://pdhacademy.com/wp-content/uploads/2023/08/"
     "462-Piping-and-Instrumentation-Diagrams.pdf",
     "PDH Academy course 462. Used as a NEGATIVE case: its vector pages are "
     "tables of ISA letters, not drawings."),
]

# (label, source pdf, page, render scale, is_drawing)
CASES = [
    ("King County effluent P&ID (real)", "awwa-pid.pdf", 57, "native", True),
    ("POL platform plot plan (real)", "opito-process-flow-pid.pdf", 45, 2.0, True),
    ("POL P&ID legend sheet (real)", "opito-process-flow-pid.pdf", 55, 2.0, True),
    ("ISA letter table (NOT a drawing)", "pdh-462-pid.pdf", 15, 2.2, False),
]

# Transcribed from King County drawing WW510-P-60003 by reading the sheet.
KCWTD_TAGS = set("""
BV-510503A BV-510503B BV-510503C BV-510503D BV-510503E BV-510503F BV-510503G
BV-510503H BV-510503I BV-510503J BV-510503K BV-510503L BV-510503M BV-510544D
BV-510544E BV-510544F BV-510544G BV-510544H BV-510544N CV-510503 CV-510544A
CV-510544B NV-510544A SV-510503 SV-510503A SV-510544A SV-510544B SV-510544D
PRV-510544 AIT-520282 AIT-520431 AE-520282 AE-520431 AI-510503 AX-520431
AY-510282CL PI-510503A PI-510503B FS-510504 ME-510503A ME-510503B ME-510503C
SEP-510503 SAP-510544 T-510544 PNL-510501 PNL-510541 PNL-520811
""".split())


def fetch() -> None:
    CACHE.mkdir(parents=True, exist_ok=True)
    for name, url, _why in SOURCES:
        out = CACHE / name
        if out.exists():
            print(f"  cached  {name}")
            continue
        print(f"  fetching {name} …", flush=True)
        subprocess.run(["curl", "-sL", "-A", "Mozilla/5.0", url, "-o", str(out)],
                       timeout=300, check=False)
        print(f"           {out.stat().st_size / 1e6:.1f} MB"
              if out.exists() else "           FAILED")


def render(pdf: str, page: int, scale) -> Path | None:
    import fitz
    src = CACHE / pdf
    if not src.exists():
        return None
    out = CACHE / f"{pdf.split('-')[0]}-p{page}-{scale}.png"
    if out.exists():
        return out
    d = fitz.open(str(src))
    p = d[page - 1]
    if scale == "native":
        # Use the embedded image at its own resolution. Rendering the page at an
        # arbitrary zoom either throws away pixels or invents them, and on this
        # drawing that alone moved tag F1 from 0.27 to 0.57.
        imgs = p.get_images(full=True)
        if imgs:
            info = d.extract_image(imgs[0][0])
            out = out.with_suffix("." + info["ext"])
            out.write_bytes(info["image"])
            d.close()
            return out
        scale = 2.0
    p.get_pixmap(matrix=fitz.Matrix(scale, scale)).save(str(out))
    d.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true")
    args = ap.parse_args()

    print(__doc__.split("\n\n")[1].strip(), "\n")
    for name, url, why in SOURCES:
        print(f"  {name}\n    {url}\n    {why}")
    print()
    if args.fetch:
        fetch()
        print()

    from sovereign import db; db.init_db()
    from sovereign.drawings import analyze
    from sovereign.drawings.tags import plant_tag_lexicon

    lex = plant_tag_lexicon()
    print(f"plant tag register available to the reader: {len(lex)} tags\n")
    print(f"{'drawing':36s} {'symbols':>8s} {'tagged':>7s} {'P':>5s} {'R':>5s} "
          f"{'F1':>5s}  verdict")
    print("-" * 96)

    for label, pdf, page, scale, is_drawing in CASES:
        img = render(pdf, page, scale)
        if img is None:
            print(f"{label:36s} {'—':>8s}  source not downloaded (use --fetch)")
            continue
        t = time.time()
        a = analyze.analyse_image(img, title=label, use_vlm=False)
        s = a.summary()
        found = {x.text.upper() for x in a.tags}

        if "King County" in label:
            tp = found & KCWTD_TAGS
            p = len(tp) / len(found) if found else 0.0
            r = len(tp) / len(KCWTD_TAGS)
            f1 = 2 * p * r / (p + r) if p + r else 0.0
            score = f"{p:5.2f} {r:5.2f} {f1:5.2f}"
        else:
            score = f"{'—':>5s} {'—':>5s} {'—':>5s}"

        verdict = "drawing" if s["symbols"] else "refused"
        if not is_drawing:
            verdict = ("correctly refused / near-empty"
                       if s["symbols"] < 60 else "FALSE POSITIVE")
        print(f"{label:36s} {s['symbols']:8d} {s['symbols_tagged']:7d} {score}"
              f"  {verdict} ({time.time()-t:.0f}s)")
        for w in a.warnings[:1]:
            print(f"{'':36s} ! {w[:100]}")

    print()
    print("Read the numbers, not the demo. The synthetic P&ID in corpus/ scores "
          "1.00/1.00\nbecause the same code drew it and its ground truth. These "
          "are drawings nobody here drew.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
