"""The node's signing, sealing, gate and repository-verification code, as the
client uses it.

In an installed client these are vendored copies (`clawcal/_vendor/`), put
there by the node's bundle builder from the very files the node is tested
with. In a development checkout they are imported from the node's tree.
Either way there is one implementation, never a client re-write of it.
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    from ._vendor import gatecore, sealed, signing, tuf_verify   # installed client
except ImportError:                                              # development tree
    _backend = Path(__file__).resolve().parents[2] / "backend"
    if _backend.is_dir() and str(_backend) not in sys.path:
        sys.path.insert(0, str(_backend))
    from sovereign import sealed, signing                        # type: ignore[no-redef]
    from sovereign.bundles import verify as tuf_verify           # type: ignore[no-redef]
    from sovereign.evidence import gatecore                      # type: ignore[no-redef]

__all__ = ["gatecore", "sealed", "signing", "tuf_verify"]
