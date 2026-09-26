#!/usr/bin/env bash
# ClawCal installer: a clean Linux host to a running, verified appliance.
#
#   sudo ./ops/install.sh                 server install (systemd, egress policy)
#   ./ops/install.sh --user               workstation install, no root needed
#   ./ops/install.sh --dry-run            print every step, change nothing
#
# Options
#   --user              per-user service, no system packages, no firewall
#   --offline DIR       install Python wheels from DIR only (air-gapped box)
#   --models LIST|auto|none
#                       auto (default) sizes the model set to the GPU found;
#                       or a comma list of Ollama tags
#   --install-ollama    fetch and install Ollama if it is missing (network)
#   --no-egress         do not apply the host default-deny policy
#   --no-start          install, but do not start or self-test
#   --host ADDR         listen address (default 127.0.0.1; any other turns on
#                       token authentication, which the server insists on)
#   --port N            default 8794
#
# Order matters for sovereignty: everything that needs the network — packages,
# wheels, the embedding model, model weights — happens first, and the egress
# policy closes the door last. After that the appliance never needs it again.
#
# Tested on Fedora 42 and Ubuntu 22.04/24.04; Amazon Linux 2023 and RHEL 9 use
# the dnf path. Idempotent: re-running upgrades in place and keeps data.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
MODE=system; DRY=0; OFFLINE=""; MODELS=auto; INSTALL_OLLAMA=0; EGRESS=1; START=1
HOST=127.0.0.1; PORT=8794

while [[ $# -gt 0 ]]; do
  case "$1" in
    --user) MODE=user ;;
    --dry-run) DRY=1 ;;
    --offline) OFFLINE="$2"; shift ;;
    --models) MODELS="$2"; shift ;;
    --install-ollama) INSTALL_OLLAMA=1 ;;
    --no-egress) EGRESS=0 ;;
    --no-start) START=0 ;;
    --host) HOST="$2"; shift ;;
    --port) PORT="$2"; shift ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
  shift
done

if [[ $MODE == system ]]; then
  DATA="${SOVEREIGN_DATA_DIR:-/var/lib/clawcal}"; SVC_USER=clawcal
  UNIT_DIR=/etc/systemd/system; ENV_FILE=/etc/clawcal/clawcal.env
else
  DATA="${SOVEREIGN_DATA_DIR:-$HOME/.sovereign}"; SVC_USER="$(id -un)"
  UNIT_DIR="$HOME/.config/systemd/user"; ENV_FILE="$HOME/.config/clawcal/clawcal.env"
fi
VENV="$DATA/venv"
AUTH=local; [[ "$HOST" != "127.0.0.1" && "$HOST" != "localhost" && "$HOST" != "::1" ]] && AUTH=token

G=$'\e[32m'; R=$'\e[31m'; Y=$'\e[33m'; B=$'\e[1m'; N=$'\e[0m'
[[ -t 1 ]] || { G=""; R=""; Y=""; B=""; N=""; }
step() { printf '\n%s==> %s%s\n' "$B" "$*" "$N"; }
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*"; }
die()  { printf '  %s✗ %s%s\n' "$R" "$*" "$N" >&2; exit 1; }
run()  { if [[ $DRY == 1 ]]; then printf '    $ %s\n' "$*"; else "$@"; fi; }
sudo_run() {
  if [[ $DRY == 1 ]]; then printf '    $ sudo %s\n' "$*"; return; fi
  if [[ $EUID -eq 0 ]]; then "$@"; else sudo "$@"; fi
}

[[ $MODE == system && $EUID -ne 0 && $DRY == 0 ]] && die "a server install needs root (sudo), or use --user"

# ------------------------------------------------------------------ platform
step "Platform"
. /etc/os-release 2>/dev/null || die "cannot read /etc/os-release"
case "${ID_LIKE:-} $ID" in
  *fedora*|*rhel*|*centos*|*amzn*) PKG=dnf ;;
  *debian*|*ubuntu*) PKG=apt ;;
  *) PKG=unknown ;;
esac
ok "$PRETTY_NAME ($PKG), mode $MODE, data in $DATA"

PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$cand" >/dev/null && "$cand" -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
    PY="$(command -v "$cand")"; break
  fi
done

GPU_MB=0; GPU_N=0
if command -v nvidia-smi >/dev/null; then
  while read -r mb; do GPU_MB=$((GPU_MB + ${mb%.*})); GPU_N=$((GPU_N + 1)); done \
    < <(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null || true)
fi
RAM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
ok "GPU: ${GPU_N} device(s), ${GPU_MB} MB VRAM total; RAM ${RAM_MB} MB"

# ------------------------------------------------------------ system packages
step "System packages"
if [[ $MODE == system ]]; then
  if [[ $PKG == dnf ]]; then
    sudo_run dnf install -y tesseract tesseract-langpack-eng bubblewrap nftables \
      python3 python3-pip curl util-linux findutils
  elif [[ $PKG == apt ]]; then
    sudo_run env DEBIAN_FRONTEND=noninteractive apt-get update -q
    sudo_run env DEBIAN_FRONTEND=noninteractive apt-get install -y -q tesseract-ocr \
      tesseract-ocr-eng bubblewrap nftables python3 python3-venv python3-pip curl
  else
    warn "unknown package manager: install tesseract, bubblewrap, nftables, python3.10+ by hand"
  fi
  if [[ -z "$PY" && $PKG == apt ]]; then
    # Ubuntu 20.04 ships 3.8. Prefer the distribution's newer package.
    sudo_run env DEBIAN_FRONTEND=noninteractive apt-get install -y -q python3.11 python3.11-venv || true
    PY="$(command -v python3.11 || true)"
  fi
else
  for b in tesseract bwrap nft; do
    command -v "$b" >/dev/null && ok "$b present" || warn "$b missing (--user cannot install it; features degrade and say so)"
  done
fi
[[ -n "$PY" || $DRY == 1 ]] || die "Python 3.10+ is required"
ok "python: ${PY:-python3 (dry run)}"

# ----------------------------------------------------------------- service user
if [[ $MODE == system ]]; then
  step "Service account"
  if ! id "$SVC_USER" >/dev/null 2>&1; then
    sudo_run useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin "$SVC_USER"
  fi
  sudo_run install -d -o "$SVC_USER" -g "$SVC_USER" -m 0750 "$DATA"
  ok "$SVC_USER owns $DATA"
fi

# ---------------------------------------------------------------- python env
step "Python environment ($VENV)"
as_svc() {
  if [[ $MODE == system ]]; then
    if [[ $DRY == 1 ]]; then printf '    $ sudo -u %s %s\n' "$SVC_USER" "$*"; else sudo -u "$SVC_USER" -H "$@"; fi
  else run "$@"; fi
}
[[ -x "$VENV/bin/python" ]] || as_svc "${PY:-python3}" -m venv "$VENV"
PIP=("$VENV/bin/pip" install --disable-pip-version-check -q)
if [[ -n "$OFFLINE" ]]; then
  as_svc "${PIP[@]}" --no-index --find-links "$OFFLINE" -r "$REPO/requirements.txt"
else
  # Embeddings run on CPU; the CPU build of torch is a tenth of the CUDA one.
  as_svc "${PIP[@]}" torch --index-url https://download.pytorch.org/whl/cpu
  as_svc "${PIP[@]}" -r "$REPO/requirements.txt"
fi
ok "dependencies installed"

step "Embedding model (fetched now, before egress closes)"
as_svc env SOVEREIGN_DATA_DIR="$DATA" HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0 \
  "$VENV/bin/python" -c "from sentence_transformers import SentenceTransformer as S; S('sentence-transformers/all-MiniLM-L6-v2', device='cpu'); print('  embedding model cached')"

# -------------------------------------------------------------------- ollama
step "Inference backend"
if ! command -v ollama >/dev/null; then
  if [[ $INSTALL_OLLAMA == 1 ]]; then
    run sh -c 'curl -fsSL https://ollama.com/install.sh | sh'
  else
    warn "ollama not found: install it (or re-run with --install-ollama), or point OLLAMA_URLS at a server"
  fi
fi
if [[ "$MODELS" == auto ]]; then
  # Sized to the GPU so every model in the set fits VRAM with room for its KV
  # cache. A model that spills into host RAM is how a busy workstation meets
  # the OOM killer; admission refuses those loads, so pulling them is waste.
  if (( GPU_MB >= 70000 )); then MODELS="gpt-oss:120b,qwen3:32b,qwen2.5-coder:32b,qwen2.5vl:7b,gpt-oss:20b"
  elif (( GPU_MB >= 22000 )); then MODELS="qwen3:32b,gpt-oss:20b,qwen2.5vl:7b,qwen3:8b"
  elif (( GPU_MB >= 11000 )); then MODELS="gpt-oss:20b,qwen3:8b,qwen2.5vl:7b,granite3.2-vision:2b"
  elif (( GPU_MB >= 6000 )); then MODELS="qwen3:4b,granite3.2-vision:2b,qwen2.5vl:3b"
  else MODELS="qwen3:4b,granite3.2-vision:2b"; fi
fi
if [[ "$MODELS" != none ]] && command -v ollama >/dev/null; then
  IFS=',' read -ra M <<<"$MODELS"
  for m in "${M[@]}"; do run ollama pull "$m" && ok "$m"; done
else
  warn "no models pulled (--models $MODELS)"
fi

# ------------------------------------------------------------------- config
step "Configuration ($ENV_FILE)"
if [[ $DRY == 1 ]]; then
  printf '    (write %s: SOVEREIGN_HOST=%s SOVEREIGN_PORT=%s SOVEREIGN_AUTH=%s)\n' "$ENV_FILE" "$HOST" "$PORT" "$AUTH"
elif [[ ! -f "$ENV_FILE" ]]; then
  install -d "$(dirname "$ENV_FILE")"
  cat > "$ENV_FILE" <<EOF
# ClawCal appliance configuration. Edit, then: systemctl ${MODE/system/} restart clawcal
SOVEREIGN_DATA_DIR=$DATA
SOVEREIGN_HOST=$HOST
SOVEREIGN_PORT=$PORT
SOVEREIGN_AUTH=$AUTH
SOVEREIGN_POLICY_MODE=review
# Several Ollama servers are one backend, comma-separated.
OLLAMA_URLS=http://127.0.0.1:11434
# SOVEREIGN_ORG=Your organisation
# SOVEREIGN_UNIT=Your department
# SOVEREIGN_SEPARATION_OF_DUTIES=1
EOF
  chmod 0640 "$ENV_FILE"
  [[ $MODE == system ]] && chgrp "$SVC_USER" "$ENV_FILE"
  ok "written (existing files are never overwritten)"
else
  ok "kept existing $ENV_FILE"
fi

# ------------------------------------------------------------------ service
step "Service"
UNIT="$UNIT_DIR/clawcal.service"
render_unit() {
  cat <<EOF
[Unit]
Description=ClawCal sovereign workbench
After=network-online.target ollama.service
Wants=ollama.service

[Service]
Type=simple
EnvironmentFile=$ENV_FILE
Environment="PYTHONPATH=$REPO/backend"
WorkingDirectory=$REPO
ExecStart=$VENV/bin/python -m sovereign.server
Restart=on-failure
RestartSec=5
# The control plane is small; the models live in the backend's process. A cap
# here means a runaway ingest fails this service, not the whole host.
MemoryHigh=$(( RAM_MB / 4 ))M
MemoryMax=$(( RAM_MB / 3 ))M
NoNewPrivileges=true
PrivateTmp=true
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
EOF
  if [[ $MODE == system ]]; then
    # Only a system unit may lower its OOM score; a user unit that tries fails
    # to start at all.
    cat <<EOF
OOMScoreAdjust=-100
User=$SVC_USER
Group=$SVC_USER
ProtectSystem=strict
ReadWritePaths=$DATA
ProtectHome=read-only

[Install]
WantedBy=multi-user.target
EOF
  else
    printf '\n[Install]\nWantedBy=default.target\n'
  fi
}
if [[ $DRY == 1 ]]; then
  printf '    (write %s)\n' "$UNIT"; render_unit | sed 's/^/      /'
else
  install -d "$UNIT_DIR"
  render_unit > "$UNIT"
  if [[ $MODE == system ]]; then systemctl daemon-reload; systemctl enable clawcal >/dev/null
  else systemctl --user daemon-reload; systemctl --user enable clawcal >/dev/null
    loginctl enable-linger "$(id -un)" 2>/dev/null || true; fi
  ok "clawcal.service installed and enabled"
fi
[[ -e "$REPO/clawcal" ]] && ok "terminal client: $REPO/clawcal"

# ------------------------------------------------------------------- egress
step "Host egress policy"
if [[ $MODE == system && $EGRESS == 1 ]]; then
  # Rollback-guarded when run over SSH: if this session loses the box, the
  # policy removes itself; persist only once the service is confirmed up.
  if [[ -n "${SSH_CONNECTION:-}" ]]; then run "$REPO/ops/egress-policy.sh" apply --rollback 180
  else run "$REPO/ops/egress-policy.sh" apply; fi
else
  warn "not applied (--user or --no-egress); the strip will show the host layer as unverified"
fi

# -------------------------------------------------------- start + self-test
if [[ $START == 1 && $DRY == 0 ]]; then
  step "Start and verify"
  if [[ $MODE == system ]]; then systemctl restart clawcal; else systemctl --user restart clawcal; fi
  for _ in $(seq 1 60); do curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 && break; sleep 2; done
  curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null || die "the service did not come up: journalctl -u clawcal"
  ok "workbench answering on :$PORT"
  TOKEN=""
  [[ -f "$DATA/owner.token" ]] && TOKEN="$(cat "$DATA/owner.token" 2>/dev/null || sudo cat "$DATA/owner.token")"
  export CLAWCAL_URL="http://127.0.0.1:$PORT" CLAWCAL_TOKEN="$TOKEN"
  (cd "$DATA" && "$REPO/clawcal" selftest --report) && ST=green || ST=red
  if [[ $MODE == system && $EGRESS == 1 && $ST == green ]]; then
    "$REPO/ops/egress-policy.sh" confirm >/dev/null 2>&1 || true
    "$REPO/ops/egress-policy.sh" persist && ok "egress policy persisted across reboots"
  fi
  "$REPO/clawcal" status || true
  echo
  if [[ $ST == green ]]; then
    printf '%s%s  ClawCal is running. Sovereignty self-test passed.%s\n' "$G" "$B" "$N"
  else
    printf '%s%s  ClawCal is running, but the sovereignty self-test did NOT pass.%s\n' "$R" "$B" "$N"
  fi
  [[ "$AUTH" == token ]] && echo "  owner token: $DATA/owner.token (clawcal login <token>)"
  echo "  web: http://$HOST:$PORT   terminal: $REPO/clawcal"
fi
