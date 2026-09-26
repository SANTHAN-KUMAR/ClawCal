#!/usr/bin/env python3
"""Every model the node serves, exercised through the node's own gateway.

The acceptance run (verify_e2e.py) proves the system works with *some* model
for each capability; this proves each served model works at all, one at a
time, on the path the product uses: text models answer through their prompt
adapter with fallback disabled, vision models transcribe the corpus nameplate
photograph through the same reader ingestion uses. Nothing is mocked.

Models are loaded strictly one at a time and evicted afterwards. A model the
host cannot take is refused by the gateway's own memory pricing, and that
refusal is reported as SKIP with the reason, never forced.

    python3 scripts/verify_models.py                # every served model
    python3 scripts/verify_models.py --only qwen3-4b,qwen2.5vl-3b

Results are printed and written to docs/model-sweep-results.json.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from verify_e2e import bootstrap                                  # noqa: E402
from sovereign import hardware                                    # noqa: E402
from sovereign.gateway import gateway                             # noqa: E402
from sovereign.gateway.base import ChatMessage, GenRequest        # noqa: E402
from sovereign.gateway.registry import registry                   # noqa: E402
from sovereign.knowledge import ocr                               # noqa: E402
from sovereign.runtime.residency import residency         # noqa: E402

PHOTO = ROOT / "corpus" / "photos" / "nameplate-V-204-photo.jpg"
QUESTION = "What is 17 multiplied by 23? Reply with the number only."


def text_check(name: str) -> tuple[str, str]:
    res = gateway.generate(GenRequest(
        messages=[ChatMessage("user", QUESTION)], model=name, max_tokens=400,
        temperature=0.0, reasoning="low", timeout_s=600), allow_fallback=False)
    if not res.ok:
        return ("SKIP" if ocr._unavailable(res.error) else "FAIL"), str(res.error)
    ok = "391" in res.text
    return ("PASS" if ok else "FAIL"), (
        f"answered {res.text.strip()[:40]!r} at {res.decode_tps:.1f} tok/s "
        f"via the {registry.get(name).prompt_adapter} adapter")


def vision_check(name: str) -> tuple[str, str]:
    t0 = time.time()
    text, _conf = ocr._vlm_read(PHOTO, model=name)
    if not text:
        return "SKIP", "the reader did not run (refused or unavailable on this host)"
    ok = "V-204" in text.upper().replace(" ", "")
    return ("PASS" if ok else "FAIL"), (
        f"transcribed {len(text)} chars in {time.time() - t0:.0f}s; "
        f"{'found' if ok else 'missed'} the tag V-204")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="comma-separated model names")
    args = ap.parse_args()
    bootstrap(False)
    wanted = {x.strip() for x in args.only.split(",") if x.strip()}
    cards = [c for c in registry.all() if not wanted or c.name in wanted]
    results = []
    print(f"{len(cards)} served models on {hardware.gpu_state().name}")
    for card in cards:
        residency.evict_all(reason="model sweep: one model at a time")
        free = hardware.mem_state().available_mb
        t0 = time.time()
        try:
            fn = vision_check if card.modality == "vision" else text_check
            status, detail = fn(card.name)
        except Exception as exc:                                  # noqa: BLE001
            status, detail = "FAIL", f"unhandled: {exc!r}"
        row = {"model": card.name, "backend_ref": card.backend_ref,
               "modality": card.modality, "role": card.role, "status": status,
               "detail": detail, "seconds": round(time.time() - t0, 1),
               "host_free_mb_before": round(free)}
        results.append(row)
        print(f"  {status:4}  {card.name:22} {detail}")
    residency.evict_all(reason="model sweep finished")
    out = ROOT / "docs" / "model-sweep-results.json"
    out.write_text(json.dumps({"ran_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                               "results": results}, indent=2) + "\n")
    return 1 if any(r["status"] == "FAIL" for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
