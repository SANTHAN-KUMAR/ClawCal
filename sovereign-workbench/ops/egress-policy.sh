#!/usr/bin/env bash
# Sovereign egress policy — host-level layer of the defence-in-depth stack.
#
# Establishes a default-DROP outbound policy with logging, permitting only
# loopback traffic to the local inference backends. Every refused packet is
# logged with the prefix SOVEREIGN-EGRESS-DENY, which the workbench surfaces on
# its Sovereignty view alongside the application-level denial record.
#
# This is layer 1 of five. The others (application guard, tool policy, sandbox
# network namespace, audit) work without it — the workbench is fully functional
# and still refuses egress if this is never run — but the host firewall is what
# makes the refusal true for processes the control plane does not own.
#
#   sudo ./ops/egress-policy.sh apply
#   sudo ./ops/egress-policy.sh status
#   sudo ./ops/egress-policy.sh remove
set -euo pipefail

TABLE="sovereign"
# Retained for documentation and for deployments that additionally wish to pin
# loopback to the inference ports. The default policy accepts loopback in full.
ALLOWED_PORTS="{ 11434, 8000, 8080, 5000, 1234, 8794 }"

need_root() {
  if [[ $EUID -ne 0 ]]; then
    echo "This script must run as root (it edits the host firewall)." >&2
    exit 1
  fi
}

apply() {
  need_root
  command -v nft >/dev/null || { echo "nftables (nft) is not installed." >&2; exit 1; }

  nft list table inet "$TABLE" >/dev/null 2>&1 && nft delete table inet "$TABLE"

  nft -f - <<NFT
table inet ${TABLE} {
  chain output {
    type filter hook output priority filter; policy drop;

    # Established flows the policy already permitted.
    ct state established,related accept

    # Loopback in full. Traffic that never leaves the machine is not egress, and
    # restricting it to a port list breaks unrelated local IPC — desktop
    # services, the display server, language servers — for no sovereignty gain.
    # The local inference backends and the workbench itself ride on this.
    oifname "lo" accept

    # Anything else is a sovereignty violation. Log it, then drop it: the
    # denial record is the evidence, and an empty packet capture is not.
    limit rate 20/second burst 40 packets \\
      log prefix "SOVEREIGN-EGRESS-DENY " level warn flags all
    counter drop
  }

  chain input {
    type filter hook input priority filter; policy drop;
    ct state established,related accept
    iifname "lo" accept
  }
}
NFT
  echo "Sovereign egress policy applied. Outbound traffic is DROP by default."
  echo "Denied packets are logged with prefix SOVEREIGN-EGRESS-DENY:"
  echo "  journalctl -k -g SOVEREIGN-EGRESS-DENY -f"
}

status() {
  if nft list table inet "$TABLE" 2>/dev/null; then
    echo
    echo "--- recent denials ---"
    journalctl -k -g SOVEREIGN-EGRESS-DENY -n 20 --no-pager 2>/dev/null \
      || echo "(kernel log unavailable to this user)"
  else
    echo "Sovereign table is NOT loaded. Run: sudo $0 apply"
    exit 1
  fi
}

remove() {
  need_root
  nft delete table inet "$TABLE" 2>/dev/null && echo "Sovereign egress policy removed." \
    || echo "Sovereign table was not loaded."
}

case "${1:-status}" in
  apply)  apply ;;
  status) status ;;
  remove) remove ;;
  *) echo "usage: $0 {apply|status|remove}" >&2; exit 2 ;;
esac
