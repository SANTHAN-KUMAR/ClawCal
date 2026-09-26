#!/usr/bin/env python3
"""Evaluate the perception pipeline on the held-out dirty corpus (v2 §6.1, B4).

Runs the product's own code paths — OCR, the VLM fallback, the drawing
pipeline, the spreadsheet tool, the injection scanner — on documents nobody on
this project made, and scores each against the ground truth its source
published. `scripts/fetch_dirty_corpus.py` builds the corpus.

Three numbers per class, because a score alone rewards guessing:

    quality           how much of the truth the system recovered
    refusal rate      how often it declined (DEGRADED / CANNOT DETERMINE)
    confidently wrong how often it ACCEPTED an output that is in fact wrong

The last is the one that matters. A change that raises quality by lowering the
refusal rate while raising "confidently wrong" is rejected (§6.1).

Each item runs in its own child process with a timeout, so one pathological
file costs that item, not the run, and not the machine's memory.

    python3 scripts/eval_dirty.py                      # everything, OCR + VLM
    python3 scripts/eval_dirty.py --classes handwriting --vlm qwen2.5vl-3b
    python3 scripts/eval_dirty.py --vlm none           # OCR only
    python3 scripts/eval_dirty.py --vlm all            # compare every VLM
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Resolved once and handed to child processes explicitly: _boot() repoints
# SOVEREIGN_DATA_DIR at the evaluation store, and children inherit it.
DIRTY = Path(os.environ.get("CLAWCAL_DIRTY_DIR") or
             Path(os.environ.get("SOVEREIGN_DATA_DIR", Path.home() / ".sovereign"))
             / "dirty")
os.environ["CLAWCAL_DIRTY_DIR"] = str(DIRTY)
EVAL_DATA = Path.home() / ".sovereign" / "eval"          # on disk, never tmpfs
ITEM_TIMEOUT_S = int(os.environ.get("EVAL_ITEM_TIMEOUT_S", "420"))

# Thresholds for "the output is wrong", per class.
SCAN_MIN_RECALL = 0.5          # a scan read that recovers under half the words
HAND_MAX_CER = 0.30            # a handwriting read with >30% character error


# --------------------------------------------------------------- metrics

def norm_words(text: str) -> list[str]:
    t = unicodedata.normalize("NFKC", text or "").lower()
    return [w for w in re.findall(r"[a-z0-9]+", t) if w]


def bag_scores(truth: list[str], pred: list[str]) -> tuple[float, float]:
    tc, pc = Counter(truth), Counter(pred)
    hit = sum((tc & pc).values())
    recall = hit / max(1, sum(tc.values()))
    precision = hit / max(1, sum(pc.values()))
    return round(recall, 3), round(precision, 3)


_PUNCT_SPACE = re.compile(r"\s*([,.;:!?\"'()\[\]])\s*")


def _cer_norm(s: str) -> str:
    # IAM writes ' Scotland the Brave , ' with spaced punctuation; a reader that
    # writes 'Scotland the Brave,' has read it correctly. Whitespace around
    # punctuation is not scored, for every reader alike.
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = s.replace("\u201c", '"').replace("\u201d", '"').replace("\u2019", "'")
    return _PUNCT_SPACE.sub(r"\1", " ".join(s.split()))


def cer(truth: str, pred: str) -> float:
    a = _cer_norm(truth)
    b = _cer_norm(pred)
    if not a:
        return 0.0 if not b else 1.0
    if len(a) * len(b) > 4_000_000:                  # bound the DP on huge pages
        b = b[: 4_000_000 // max(1, len(a))]
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
        prev = cur
    return round(min(1.0, prev[-1] / len(a)), 3)


def digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


# ----------------------------------------------------------- one item (child)

def _boot():
    os.environ["SOVEREIGN_DATA_DIR"] = str(EVAL_DATA)
    os.environ.setdefault("SOVEREIGN_POLICY_MODE", "trusted")
    sys.path.insert(0, str(ROOT / "backend"))
    from sovereign import db
    db.init_db()
    from sovereign.gateway import gateway
    gateway.sync_registry()
    return db


def read_image(path: Path, vlm: str | None) -> dict:
    from sovereign.knowledge import ocr
    t_text, _words, t_conf = ocr._tesseract_tsv(path)
    t0 = time.time()
    pages = ocr.extract_image(path, f"eval-{path.stem}", use_vlm=bool(vlm),
                              vlm_model=vlm)
    pt = pages[0]
    out = {"tess_text": t_text, "tess_conf": round(t_conf, 1),
           "product_text": pt.text, "extractor": pt.extractor,
           "product_conf": round(pt.confidence, 1), "product_ok": pt.ok,
           "product_note": pt.error, "seconds": round(time.time() - t0, 1)}
    if vlm:
        if pt.extractor == "vlm":
            out["vlm_text"], out["vlm_seconds"] = pt.text, out["seconds"]
        else:
            t1 = time.time()
            v_text, _ = ocr._vlm_read(path, model=vlm)
            out["vlm_text"] = v_text
            out["vlm_seconds"] = round(time.time() - t1, 1)
    return out


OUTCOME_OF = {"tesseract": "ESTABLISHED", "pdf-text-layer": "ESTABLISHED",
              "xlsx": "ESTABLISHED", "cad": "ESTABLISHED", "vlm": "INTERPRETED",
              "tesseract-low-confidence": "DEGRADED", "failed": "CANNOT_DETERMINE"}


def accepted(extractor: str, ok: bool) -> bool:
    """Whether the product presented this read as good enough to use."""
    return ok and extractor in ("tesseract", "pdf-text-layer", "vlm", "xlsx", "cad")


def eval_item(item: dict, vlm: str | None) -> dict:
    _boot()
    path = DIRTY / item["path"]
    cls, truth = item["class"], item.get("truth") or {}
    r: dict = {"id": item["id"], "class": cls}

    if cls in ("scans", "handwriting", "photographs") and path.suffix.lower() in (
            ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".bmp"):
        rd = read_image(path, vlm)
        r.update({k: rd[k] for k in ("tess_conf", "extractor", "product_conf",
                                     "product_ok", "product_note", "seconds")})
        r["accepted"] = accepted(rd["extractor"], rd["product_ok"])
        r["degraded"] = "low-confidence" in rd["extractor"]
        r["outcome"] = OUTCOME_OF.get(rd["extractor"], "CANNOT_DETERMINE")
        texts = {"tess": rd["tess_text"], "product": rd["product_text"]}
        if "vlm_text" in rd:
            texts["vlm"] = rd["vlm_text"]
            r["vlm_seconds"] = rd["vlm_seconds"]
        if cls == "scans" and truth.get("words"):
            tw = norm_words(" ".join(truth["words"]))
            for k, t in texts.items():
                r[f"{k}_recall"], r[f"{k}_precision"] = bag_scores(tw, norm_words(t))
            r["quality"] = r["product_recall"]
            r["wrong"] = r["accepted"] and r["product_recall"] < SCAN_MIN_RECALL
        elif cls == "handwriting" and truth.get("text"):
            for k, t in texts.items():
                r[f"{k}_cer"] = cer(truth["text"], t)
            r["quality"] = round(1 - r["product_cer"], 3)
            r["wrong"] = r["accepted"] and r["product_cer"] > HAND_MAX_CER
        elif cls == "photographs" and truth.get("fields"):
            fields = {k: v for k, v in truth["fields"].items()
                      if digits(str(v)) and len(digits(str(v))) >= 3}
            for k, t in texts.items():
                d = digits(t)
                hits = [f for f, v in fields.items() if digits(str(v)) in d]
                r[f"{k}_field_recall"] = round(len(hits) / max(1, len(fields)), 3)
            r["quality"] = r["product_field_recall"]
            r["wrong"] = r["accepted"] and r["product_field_recall"] < 0.5
        else:
            r["quality"] = None
            r["wrong"] = None                            # no truth: refusal only
        r["preview"] = {k: (t or "")[:160] for k, t in texts.items()}
        return r

    if cls == "spreadsheets":
        from sovereign.tools import spreadsheet
        hits, headers_ok, n = 0, 0, 0
        misses = []
        for key, want in truth.get("fields", {}).items():
            meta = truth.get("cells", {}).get(key, {})
            if not meta.get("cell"):
                continue
            n += 1
            try:
                got = spreadsheet.read_range(path, meta.get("sheet"), meta["cell"])
            except Exception as exc:
                misses.append({"key": key, "error": str(exc)[:120]})
                continue
            cells = got.get("cells") or []
            val = cells[0]["value"] if cells else ""
            ok = (val.strip() == str(want).strip() or
                  (_num(val) is not None and _num(val) == _num(want)))
            hits += ok
            col_hdr = key.split("!", 1)[1].split("/")[-1].split(">")[-1].strip().lower()
            hdr = (cells[0].get("header") or "").lower() if cells else ""
            headers_ok += bool(col_hdr and col_hdr in hdr)
            if not ok:
                misses.append({"key": key, "want": want, "got": val})
        r.update({"cells": n, "value_hits": hits, "header_hits": headers_ok,
                  "quality": round(hits / max(1, n), 3),
                  "header_quality": round(headers_ok / max(1, n), 3),
                  "accepted": True, "degraded": False, "wrong": hits < n,
                  "misses": misses[:6]})
        return r

    if cls in ("cad", "raster_drawings"):
        from sovereign.drawings import analyze
        t0 = time.time()
        suf = path.suffix.lower()
        try:
            if suf in (".dwg", ".dxf"):
                a = analyze.analyse_cad(path, title=path.stem)
            elif suf == ".pdf":
                a = analyze.analyse_pdf(path, title=path.stem, use_vlm=False)
            else:
                a = analyze.analyse_image(path, title=path.stem, use_vlm=False)
        except Exception as exc:
            r.update({"error": f"{type(exc).__name__}: {str(exc)[:200]}",
                      "accepted": False, "degraded": True, "wrong": False,
                      "quality": None})
            return r
        s = a.summary()
        tags = {t.text.upper() for t in a.tags} if hasattr(a, "tags") else set()
        r.update({"source_kind": a.source_kind, "symbols": len(a.symbols),
                  "tags": len(tags), "confirmed_edges": s.get("confirmed_connectivity"),
                  "seconds": round(time.time() - t0, 1), "accepted": True,
                  "degraded": a.source_kind == "raster"})
        want = {t.upper() for t in truth.get("tags") or []}
        if want:
            got = {re.sub(r"[^A-Z0-9]", "", t) for t in tags}
            w = {re.sub(r"[^A-Z0-9]", "", t) for t in want}
            tp = len(got & w)
            r["tag_recall"] = round(tp / max(1, len(w)), 3)
            r["tag_precision"] = round(tp / max(1, len(got)), 3)
            r["quality"] = r["tag_recall"]
        else:
            r["quality"] = None
        if truth.get("expect") == "inventory_only_no_confirmed_edges":
            r["wrong"] = bool(s.get("confirmed_connectivity"))
        elif truth.get("expect") == "not_a_drawing":
            r["wrong"] = len(a.symbols) > 25
            r["quality"] = 1.0 if not r["wrong"] else 0.0
        else:
            r["wrong"] = None
        return r

    if cls == "adversarial":
        from sovereign.knowledge import ocr
        from sovereign.policy import injection
        suf = path.suffix.lower()
        if suf == ".pdf":
            pages = ocr.extract_pdf(path, f"eval-{path.stem}", use_vlm=bool(vlm))
        elif suf in (".xlsx", ".xlsm"):
            pages = ocr.extract_xlsx(path)
        else:
            pages = ocr.extract_image(path, f"eval-{path.stem}", use_vlm=bool(vlm),
                                      vlm_model=vlm)
        text = "\n".join(p.text for p in pages)
        scan = injection.scan(text, source=path.name)
        injected = truth.get("injected") or []
        reached = any(_overlap(i, text) > 0.5 for i in injected)
        r.update({"extractor": ",".join(sorted({p.extractor for p in pages})),
                  "injection_reached_text": reached, "flagged": scan.detected,
                  "severity": scan.max_severity if scan.detected else None,
                  "accepted": True, "degraded": False,
                  # Wrong = injected instructions reached the text the agent reads
                  # without being flagged as untrusted.
                  "wrong": reached and not scan.detected,
                  "quality": 1.0 if (scan.detected or not reached) else 0.0})
        return r

    r.update({"skipped": f"no evaluator for {cls} {path.suffix}"})
    return r


def _num(v):
    try:
        return round(float(str(v).replace(",", "").strip()), 6)
    except (TypeError, ValueError):
        return None


def _overlap(needle: str, hay: str) -> float:
    n = set(norm_words(needle))
    return len(n & set(norm_words(hay))) / max(1, len(n))


# ------------------------------------------------------------- orchestration

def run_child(item: dict, vlm: str | None) -> dict:
    cmd = [sys.executable, __file__, "--one", json.dumps(item), "--vlm", vlm or "none"]
    t0 = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=ITEM_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return {"id": item["id"], "class": item["class"], "timeout": True,
                "accepted": False, "degraded": True, "wrong": False, "quality": None,
                "seconds": ITEM_TIMEOUT_S}
    line = next((l for l in reversed(p.stdout.splitlines()) if l.startswith("{")), "")
    try:
        r = json.loads(line)
    except ValueError:
        r = {"id": item["id"], "class": item["class"], "crash": p.stderr[-600:],
             "accepted": False, "degraded": True, "wrong": False, "quality": None}
    r.setdefault("wall_s", round(time.time() - t0, 1))
    return r


def summarise(results: list[dict]) -> dict:
    by: dict[str, list[dict]] = {}
    for r in results:
        by.setdefault(r["class"], []).append(r)
    out = {}
    for cls, rs in sorted(by.items()):
        q = [r["quality"] for r in rs if r.get("quality") is not None]
        judged = [r for r in rs if r.get("wrong") is not None]
        out[cls] = {
            "items": len(rs), "with_truth": len(q),
            "quality": round(sum(q) / len(q), 3) if q else None,
            "refusal_rate": round(sum(1 for r in rs if not r.get("accepted")
                                      or r.get("degraded")) / len(rs), 3),
            "confidently_wrong": round(sum(1 for r in judged if r["wrong"])
                                       / len(judged), 3) if judged else None,
            "timeouts": sum(1 for r in rs if r.get("timeout")),
            "crashes": sum(1 for r in rs if r.get("crash")),
            # How many items the VLM actually answered. A run where it answered
            # none did not measure the VLM at all (it never loaded, or a
            # breaker opened) and must not be reported as a score.
            "vlm_answered": sum(1 for r in rs
                                if (r.get("preview") or {}).get("vlm", "").strip()),
        }
        # Wrong answers split by the label the product put on them. A wrong
        # ESTABLISHED value is the failure that reaches a signed document; a
        # wrong INTERPRETED one was labelled for an engineer to confirm.
        for o in ("ESTABLISHED", "INTERPRETED"):
            got = [r for r in judged if r.get("outcome") == o]
            out[cls][f"{o.lower()}_share"] = round(len(got) / len(rs), 3)
            out[cls][f"{o.lower()}_wrong"] = (round(sum(1 for r in got if r["wrong"])
                                                    / len(got), 3) if got else None)
        for k in ("tess_recall", "vlm_recall", "tess_cer", "vlm_cer",
                  "tess_field_recall", "vlm_field_recall"):
            vals = [r[k] for r in rs if r.get(k) is not None]
            if vals:
                out[cls][k] = round(sum(vals) / len(vals), 3)
    return out


def vlm_quality(results: list[dict]) -> tuple[float | None, int]:
    """The VLM's own reading quality: its transcription alone, against truth.

    Deliberately not the product's end-to-end score, which depends on this
    harness's decision rules; a model is measured by what it read.
    """
    vals = []
    for r in results:
        if r.get("vlm_recall") is not None:
            vals.append(r["vlm_recall"])
        elif r.get("vlm_cer") is not None:
            vals.append(1.0 - r["vlm_cer"])
        elif r.get("vlm_field_recall") is not None:
            vals.append(r["vlm_field_recall"])
    answered = [r for r in results if (r.get("preview") or {}).get("vlm", "").strip()]
    if not answered:
        return None, 0                      # it never ran: nothing was measured
    return (round(sum(vals) / len(vals), 3) if vals else None), len(vals)


def record_capability(data_dir: Path, model: str, results: list[dict]) -> str:
    import sqlite3
    q, n = vlm_quality(results)
    if q is None or n < 5:
        return f"not recorded ({n} scored items; a measurement needs at least 5)"
    dbp = data_dir / "db" / "sovereign.db"
    if not dbp.exists():
        return f"not recorded: no appliance database at {dbp}"
    c = sqlite3.connect(str(dbp))
    cols = {r[1] for r in c.execute("PRAGMA table_info(model_profiles)")}
    if "measured_caps" not in cols:
        c.execute("ALTER TABLE model_profiles ADD COLUMN measured_caps TEXT")
    row = c.execute("SELECT measured_caps FROM model_profiles WHERE model=?",
                    (model,)).fetchone()
    caps = json.loads(row[0]) if row and row[0] else {}
    caps["vision"] = {"value": q, "samples": n, "basis": "measured",
                      "source": "dirty-corpus", "measured_at": time.time()}
    if row:
        c.execute("UPDATE model_profiles SET measured_caps=? WHERE model=?",
                  (json.dumps(caps), model))
    else:
        c.execute("INSERT INTO model_profiles (model, measured_caps, measured_at) "
                  "VALUES (?,?,?)", (model, json.dumps(caps), time.time()))
    c.commit()
    c.close()
    return f"{model}: vision {q:.3f} over {n} items -> {dbp}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--classes", default="")
    ap.add_argument("--vlm", default="auto",
                    help="auto (registry's best), none, all, or a model name")
    ap.add_argument("--limit", type=int, default=0, help="items per class")
    ap.add_argument("--truth-only", action="store_true",
                    help="only items whose source published ground truth")
    ap.add_argument("--record", nargs="?", const=str(Path.home() / ".sovereign"),
                    help="write each VLM's measured reading quality into the model "
                         "profile of the appliance at this data dir (default "
                         "~/.sovereign), so routing uses measurements, not claims")
    ap.add_argument("--one", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.one:
        item = json.loads(args.one)
        vlm = None if args.vlm == "none" else args.vlm
        print(json.dumps(eval_item(item, vlm), default=str))
        return 0

    man = json.loads((DIRTY / "manifest.json").read_text())
    items = man["items"]
    if args.classes:
        want = set(args.classes.split(","))
        items = [i for i in items if i["class"] in want]
    if args.truth_only:
        items = [i for i in items if i.get("truth")]
    if args.limit:
        per: Counter = Counter()
        kept = []
        for i in items:
            if per[i["class"]] < args.limit:
                kept.append(i)
                per[i["class"]] += 1
        items = kept

    if args.vlm == "all":
        _boot()
        from sovereign.gateway.registry import registry
        vlms = [c.name for c in registry.vision_models()]
    elif args.vlm == "auto":
        _boot()
        from sovereign.gateway.registry import registry
        v = registry.vision_models()
        vlms = [v[0].name] if v else [None]
    else:
        vlms = [None if args.vlm == "none" else args.vlm]

    runs = {}
    for vlm in vlms:
        label = vlm or "ocr-only"
        # Each model is measured alone on the GPU. With the previous one still
        # resident the next is pushed partly onto the CPU, and both its speed
        # and its quality would be measured unfairly.
        _boot()
        from sovereign import db as _db
        from sovereign.runtime.residency import residency
        _db.execute("DELETE FROM model_health")          # a fresh breaker per model
        evicted = residency.evict_all(reason="evaluation isolation")
        if evicted:
            print(f"  (evicted {', '.join(evicted)} before measuring {label})")
        print(f"\n=== {label}: {len(items)} items", flush=True)
        results = []
        for n, item in enumerate(items, 1):
            r = run_child(item, vlm)
            results.append(r)
            flag = ("TIMEOUT" if r.get("timeout") else "CRASH" if r.get("crash")
                    else "WRONG" if r.get("wrong") else
                    "refused" if (not r.get("accepted") or r.get("degraded")) else "ok")
            q = r.get("quality")
            print(f"  [{n:3}/{len(items)}] {item['id'][:52]:<52} {flag:<8} "
                  f"q={q if q is not None else '-'} {r.get('extractor', '')}",
                  flush=True)
        runs[label] = {"results": results, "summary": summarise(results)}
        print(json.dumps(runs[label]["summary"], indent=1))
        if args.record and vlm:
            print("  recorded:", record_capability(Path(args.record), vlm, results))

    out = DIRTY / f"results-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(json.dumps({"generated": time.time(), "runs": runs}, indent=1,
                              default=str))
    print(f"\nresults written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
