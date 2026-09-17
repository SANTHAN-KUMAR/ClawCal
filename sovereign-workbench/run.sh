#!/usr/bin/env bash
# Start the Sovereign Workbench, checking its preconditions first.
set -euo pipefail
cd "$(dirname "$0")"

export PYTHONPATH="$PWD/backend:${PYTHONPATH:-}"

say() { printf '  %-34s %s\n' "$1" "$2"; }

echo "Sovereign On-Premise AI Workbench"
echo "---------------------------------"

# The inference backend has to be up; everything else degrades gracefully.
if ! curl -sf http://127.0.0.1:11434/api/version >/dev/null 2>&1; then
  echo "  Ollama is not responding on 127.0.0.1:11434."
  echo "  Start it with:  ollama serve &"
  echo "  (or point the gateway elsewhere with OLLAMA_URL / OPENAI_COMPAT_URL)"
  exit 1
fi
say "inference backend" "up"

command -v tesseract >/dev/null && say "OCR" "$(tesseract --version 2>&1 | head -1)" \
  || say "OCR" "tesseract MISSING — scanned documents will fail"
command -v bwrap >/dev/null && say "sandbox" "bubblewrap" \
  || say "sandbox" "bubblewrap missing — will fall back, possibly to degraded mode"
# Reading the ruleset needs CAP_NET_ADMIN. Unprivileged, a missing table and a
# refused read look identical — and announcing "not loaded" for the second told
# the operator a control was absent while it was in fact enforcing.
if ! command -v nft >/dev/null; then
  say "host egress policy" "nftables not installed"
elif nft_out=$(nft list table inet sovereign 2>&1); then
  say "host egress policy" "nftables default-deny loaded"
elif [ "$(id -u)" -ne 0 ] && printf '%s' "$nft_out" \
     | grep -qiE 'permission denied|not permitted|operation not supported'; then
  say "host egress policy" "cannot read unprivileged (sudo ./ops/egress-policy.sh status)"
else
  say "host egress policy" "not loaded (sudo ./ops/egress-policy.sh apply)"
fi

[ -f corpus/drawings/PID-204-01.pdf ] || {
  echo "  Seeding the corpus …"
  python3 scripts/seed_corpus.py >/dev/null
}
say "corpus" "present"

echo
exec python3 -m sovereign.server "$@"
