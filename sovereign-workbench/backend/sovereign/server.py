"""Launcher.

    python3 -m sovereign.server            # start the workbench
    python3 -m sovereign.server --probe    # hardware + backend report, then exit
"""
from __future__ import annotations

import argparse
import json
import sys

from . import audit, control, db, hardware
from .config import DATA_DIR, settings
from .gateway import gateway
from .gateway.registry import registry


def probe_report() -> dict:
    db.init_db()
    prof = hardware.probe()
    sync = gateway.sync_registry()
    residency = control.residency
    return {
        "hardware": {
            "gpu": prof.gpu.get("name"), "vram_mb": prof.gpu.get("total_mb"),
            "usable_vram_mb": prof.usable_vram_mb,
            "ram_gb": round(prof.memory.get("total_mb", 0) / 1024, 1),
            "cpu": prof.cpu.get("model"), "pcie_gbs": prof.pcie_est_gbs,
            "data_fs": prof.data_fs, "disk_free_gb": prof.disk_free_gb,
            "sandbox_engines": prof.sandbox_engines, "ocr": prof.ocr,
        },
        "backends": prof.backends,
        "models": [
            {"name": c.name, "enabled": c.enabled, "adapter": c.prompt_adapter,
             "weights_mb": c.residency_mb, "kv_mb_per_1k": c.kv_cost_per_1k,
             "cold_load_s": round(c.cold_load_s, 1),
             "context_budget": residency.context_budget_tokens(c)}
            for c in registry.all(include_disabled=True)
        ],
        "registry_sync": sync,
        "sovereignty": {"enforce": settings.sovereignty.enforce,
                        "nftables": control.sovereignty.nftables_status()},
        "model_fs": {"dir": prof.model_dir, "fs": prof.model_fs,
                     "warning": prof.model_fs_warning},
        "gpu_count": prof.gpu.get("count"),
        "policy_mode": control.tool_policy.policy.mode,
        "auth_mode": control.identity.auth_mode(),
        "data_dir": str(DATA_DIR),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Sovereign On-Premise AI Workbench")
    ap.add_argument("--host", default=settings.host)
    ap.add_argument("--port", type=int, default=settings.port)
    ap.add_argument("--probe", action="store_true",
                    help="print the hardware and backend report, then exit")
    ap.add_argument("--workers", type=int, default=1,
                    help="must be 1: the scheduler lives in this process")
    args = ap.parse_args()
    if args.workers != 1:
        print("refusing --workers != 1: the scheduler, residency manager and event "
              "bus are in-process, and a second worker would be a second scheduler "
              "admitting work against the same GPU. Scale out with one process per "
              "GPU host behind a reverse proxy.", file=sys.stderr)
        return 2

    if args.probe:
        print(json.dumps(probe_report(), indent=2, default=str))
        return 0

    control.identity.check_bind_safety(args.host)

    import uvicorn
    from .api.app import app

    print("=" * 74)
    print(" Sovereign On-Premise AI Workbench")
    print(f" {settings.org_name} — {settings.org_unit}")
    print(f" Web workbench : http://{args.host}:{args.port}")
    print(f" Data directory: {DATA_DIR}")
    print(f" Default mode  : {control.tool_policy.policy.mode}")
    print(f" Auth          : {control.identity.auth_mode()}")
    print(f" Egress        : DEFAULT-DENY (nftables "
          f"{'loaded' if control.sovereignty.nftables_status().get('loaded') else 'not loaded'})")
    print("=" * 74)

    # proxy_headers stays off: the identity layer reads the forwarded headers
    # itself to refuse proxied requests without a token, and must see them.
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning",
                access_log=False, proxy_headers=False, workers=1,
                timeout_keep_alive=30)
    return 0


if __name__ == "__main__":
    sys.exit(main())
