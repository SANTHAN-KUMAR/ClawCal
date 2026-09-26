#!/usr/bin/env python3
"""Patch an opencode source tree into the ClawCal client build (spec §6.2).

Each change removes a network call site from the binary itself, rather than
switching it off in configuration. Every anchor is asserted: if an upstream
version bump moves the code, the patch fails loudly instead of silently
leaving a call site in — and the offline check (scripts/offline_check.py) is
the second gate either way.

    models.dev   the catalogue is baked in at build (MODELS_DEV_API_JSON); the
                 runtime fetch and its hourly refresh loop are gone
    upgrade      auto-update is gone
    npm          no package installs from a registry at runtime (default
                 plugins included); the provider SDK is bundled
    share        the share service (opncd.ai) is gone
    well-known   remote config fetched from <url>/.well-known/opencode is gone
    providers    only `node` (the trust-domain node) and `local` (an engine on
                 loopback) can ever be used: no cloud fallback exists in the
                 binary, whatever the configuration says

    python3 patch.py <opencode-source-root>
"""
from __future__ import annotations

import sys
from pathlib import Path

MARK = "CLAWCAL-BUILD"

EDITS: list[tuple[str, str, str]] = [
    ("packages/core/src/models-dev.ts",
     """    const fetchApi = Effect.fn("ModelsDev.fetchApi")(function* () {
      return yield* HttpClientRequest.get""",
     """    const fetchApi = Effect.fn("ModelsDev.fetchApi")(function* () {
      // CLAWCAL-BUILD: no runtime catalogue fetch; the snapshot is baked in.
      if (true) return yield* Effect.fail(new Error("models.dev fetch removed in the ClawCal build"))
      return yield* HttpClientRequest.get"""),
    ("packages/core/src/models-dev.ts",
     """      if (Flag.OPENCODE_DISABLE_MODELS_FETCH) return {}""",
     """      // CLAWCAL-BUILD: never reach for the network when the snapshot is absent.
      if (true) return {}"""),
    ("packages/core/src/models-dev.ts",
     """    if (!Flag.OPENCODE_DISABLE_MODELS_FETCH && !process.argv.includes("--get-yargs-completions")) {""",
     """    // CLAWCAL-BUILD: no background refresh loop.
    if (false) {"""),
    ("packages/core/src/npm.ts",
     """      if (yield* afs.existsSafe(path.join(dir, "node_modules", name))) {
        return resolveEntryPoint(name, path.join(dir, "node_modules", name))
      }
""",
     """      if (yield* afs.existsSafe(path.join(dir, "node_modules", name))) {
        return resolveEntryPoint(name, path.join(dir, "node_modules", name))
      }
      // CLAWCAL-BUILD: no package installs from a registry at runtime; what
      // the harness needs is bundled, anything else must already be present.
      if (true) return yield* new InstallFailedError({ add: [pkg], dir })
"""),
    ("packages/core/src/npm.ts",
     """      if (!canWrite) return
""",
     """      if (!canWrite) return
      // CLAWCAL-BUILD: no registry installs.
      if (true) return
"""),
    ("packages/opencode/src/cli/upgrade.ts",
     """export async function upgrade() {""",
     """export async function upgrade() {
  // CLAWCAL-BUILD: auto-update removed; the organisation ships builds.
  return"""),
    ("packages/opencode/src/share/share-next.ts",
     """const disabled = process.env["OPENCODE_DISABLE_SHARE"] === "true" || process.env["OPENCODE_DISABLE_SHARE"] === "1\"""",
     """// CLAWCAL-BUILD: the share service is removed.
const disabled = true"""),
    ("packages/opencode/src/config/config.ts",
     """          if (value.type === "wellknown") {""",
     """          // CLAWCAL-BUILD: no remote configuration from .well-known.
          if (value.type === "wellknown" && false) {"""),
    ("packages/opencode/src/provider/provider.ts",
     """        function isProviderAllowed(providerID: ProviderV2.ID): boolean {
          if (enabled && !enabled.has(providerID)) return false""",
     """        function isProviderAllowed(providerID: ProviderV2.ID): boolean {
          // CLAWCAL-BUILD: the trust-domain node and a loopback engine are the
          // only providers this binary can use. No cloud fallback exists.
          if (providerID !== "node" && providerID !== "local") return false
          if (providerID === "local") {
            const base = String((cfg.provider as any)?.local?.options?.baseURL ?? "")
            try {
              const host = new URL(base).hostname
              if (!["127.0.0.1", "localhost", "[::1]", "::1"].includes(host)) return false
            } catch {
              return false
            }
          }
          if (enabled && !enabled.has(providerID)) return false"""),
]


def main(root: str) -> int:
    base = Path(root)
    done = 0
    for rel, old, new in EDITS:
        p = base / rel
        s = p.read_text()
        if new in s:
            done += 1
            continue
        if old not in s:
            print(f"FAIL {rel}: anchor not found — upstream moved this code; review "
                  f"the call site by hand before building", file=sys.stderr)
            return 1
        p.write_text(s.replace(old, new, 1))
        done += 1
    marks = sum((base / rel).read_text().count(MARK) for rel in {e[0] for e in EDITS})
    print(f"patched {done} call sites ({marks} {MARK} markers)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "."))
