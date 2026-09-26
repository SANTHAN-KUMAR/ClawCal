#!/usr/bin/env bash
# Build the ClawCal client harness: opencode at a pinned tag, with the model
# catalogue baked in and its network call sites removed (patch.py), for one
# platform. Run on the connected build machine (spec §9.1 item 3); the output
# is registered on the node with POST /api/admin/bundles/harness and reaches
# devices only as a signed target.
#
#   bundle/opencode/build.sh [workdir]        # default: ./opencode-build
#
# Then prove it: scripts/offline_check.py --allow <node> --runs 100 -- <binary> run ...
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
TAG="${OPENCODE_TAG:-v1.16.2}"
REPO="${OPENCODE_REPO:-https://github.com/anomalyco/opencode.git}"
WORK="${1:-$PWD/opencode-build}"
BUN_VERSION="${BUN_VERSION:-1.3.14}"          # the tag's packageManager pin

command -v bun >/dev/null || { echo "bun $BUN_VERSION is required (https://bun.sh)"; exit 2; }
[ "$(bun --version)" = "$BUN_VERSION" ] || echo "warning: bun $(bun --version), tag pins $BUN_VERSION"

if [ ! -d "$WORK/.git" ]; then
  git clone --depth 1 --branch "$TAG" "$REPO" "$WORK"
fi
python3 "$HERE/patch.py" "$WORK"                       # fails loudly on drift
(cd "$WORK" && bun install --ignore-scripts)
(cd "$WORK/packages/opencode" && \
  MODELS_DEV_API_JSON="$HERE/../models-dev/api.json" OPENCODE_CHANNEL=clawcal \
  bun run script/build.ts --single --skip-install --skip-embed-web-ui)
OUT="$(ls -d "$WORK"/packages/opencode/dist/opencode-*/bin/opencode | head -1)"
sha256sum "$OUT"
echo "built: $OUT"
