#!/usr/bin/env bash
# Start the Sovereign Workbench, checking its preconditions first.
set -euo pipefail
cd "$(dirname "$0")"

export PYTHONPATH="$PWD/backend:${PYTHONPATH:-}"

say() { printf '  %-34s %s\n' "$1" "$2"; }

echo "Sovereign On-Premise AI Workbench"
echo "---------------------------------"

# The inference backend has to be up; everything else degrades gracefully.
# OLLAMA_URLS may name several servers (models on different disks, or hosts).
URLS="${OLLAMA_URLS:-${OLLAMA_URL:-http://127.0.0.1:11434}}"
up=0
IFS=',' read -ra EPS <<<"$URLS"
for u in "${EPS[@]}"; do
  if curl -sf "${u%/}/api/version" >/dev/null 2>&1; then
    n=$(curl -sf "${u%/}/api/tags" | python3 -c 'import json,sys; print(len(json.load(sys.stdin).get("models", [])))' 2>/dev/null || echo "?")
    say "inference backend" "up: $u ($n models)"; up=$((up + 1))
  else
    say "inference backend" "DOWN: $u"
  fi
done
if [ "$up" -eq 0 ]; then
  echo "  No Ollama endpoint is responding. Start one with:  ollama serve &"
  echo "  (or set OLLAMA_URLS / OPENAI_COMPAT_URL)"
  exit 1
fi
export OLLAMA_URLS="$URLS"

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
PY="${SOVEREIGN_PYTHON:-}"
[ -z "$PY" ] && [ -x "$HOME/.sovereign/venv/bin/python" ] && PY="$HOME/.sovereign/venv/bin/python"
exec "${PY:-python3}" -m sovereign.server "$@"
