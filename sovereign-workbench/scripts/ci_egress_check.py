#!/usr/bin/env python3
"""Verify the client egress policy on this OS (Linux, Windows, macOS).

What `clawcal device egress apply|check` does, with expectations, for CI runners
where we have admin rights: before the policy, egress is open; with it, every
probe is refused fast (reject, never drop) while the allowed "node" stays
reachable and the platform mechanism reads as loaded; after removal, egress is
open again. The policy is removed in a `finally`, so a failed check never
leaves a runner cut off.

    python scripts/ci_egress_check.py https://<node-ip>:443
"""
from __future__ import annotations

import json
import platform
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "clients"), str(ROOT / "backend")]

from clawcal import egress  # noqa: E402


def main() -> int:
    node = sys.argv[1]
    before = egress.self_check(node)
    applied, during = {}, {}
    try:
        applied = egress.apply(node)
        during = egress.self_check(node)
    finally:
        removed = egress.remove()
    after = egress.self_check(node)
    ok = {
        "open before the policy": not before["active"] and any(
            p["result"] == "CONNECTED" for p in before["probes"][:3]),
        "policy applied": bool(applied.get("applied")),
        "every probe rejected, node reachable": bool(during.get("active")),
        "mechanism reads as loaded": bool(during.get("mechanism_verified")),
        "open again after removal": not after["active"],
    }
    print(json.dumps({"platform": platform.platform(), "node": node, "checks": ok,
                      "during": during, "applied": {k: v for k, v in applied.items()
                                                    if k != "ruleset"},
                      "removed": removed}, indent=1, default=str))
    return 0 if all(ok.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
