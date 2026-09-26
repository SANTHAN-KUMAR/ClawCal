#!/usr/bin/env bash
# Sovereign egress policy — host-level layer of the defence-in-depth stack.
#
# Default-DROP outbound, with every refused packet logged under the prefix
# SOVEREIGN-EGRESS-DENY, which the workbench surfaces beside its own
# application-level denial record. Loopback is open in full: the inference
# backends and the workbench itself live there.
#
# What else is allowed, and why — each is something the host itself needs to
# stay a working machine, not something an agent can use:
#
#   inbound  SSH and the ports in SOVEREIGN_INGRESS_PORTS (default "22 443").
#            A drop-everything input chain locks the administrator out of a
#            cloud VM the moment it is applied.
#   outbound DHCP (v4 and v6) and IPv6 neighbour discovery. Without them the
#            instance loses its address at the next lease renewal.
#   outbound NTP to SOVEREIGN_NTP_SERVERS only (default: none; on AWS set
#            169.254.169.123, the Time Sync Service).
#
# The cloud instance metadata service (169.254.169.254, fd00:ec2::254) is
# dropped *and logged by name*: it hands out credentials, and "an agent read
# the instance role's keys" is the exfiltration path a default-deny host exists
# to close.
#
# An nftables ruleset lives in kernel memory and does not survive a reboot, so
# `persist` installs a systemd unit that reloads it at boot.
#
#   sudo ./ops/egress-policy.sh apply [--rollback SECONDS]
#   sudo ./ops/egress-policy.sh confirm     keep a rollback-guarded apply
#   sudo ./ops/egress-policy.sh persist     apply now and at every boot
#   sudo ./ops/egress-policy.sh unpersist
#   sudo ./ops/egress-policy.sh status
#   sudo ./ops/egress-policy.sh remove
#   ./ops/egress-policy.sh render           print the ruleset, change nothing
#
# Over SSH, apply with --rollback: the policy removes itself after SECONDS
# unless `confirm` is run from a session that still works.
set -euo pipefail

TABLE="sovereign"
INGRESS_PORTS="${SOVEREIGN_INGRESS_PORTS:-22 443}"
NTP_SERVERS="${SOVEREIGN_NTP_SERVERS:-}"
STATE_DIR="/run/sovereign-egress"
PERSIST_FILE="/etc/sovereign/egress.nft"
UNIT="/etc/systemd/system/sovereign-egress.service"

need_root() {
  if [[ $EUID -ne 0 ]]; then
    echo "This must run as root (it edits the host firewall)." >&2
    exit 1
  fi
}

set_of() {   # "22 443" -> "{ 22, 443 }"
  local out="" x
  for x in $1; do
    [[ "$x" =~ ^[0-9]+$ ]] || { echo "invalid port: $x" >&2; exit 2; }
    out+="${out:+, }$x"
  done
  echo "{ ${out:-22} }"
}

render() {
  local ports ntp4="" ntp_rule=""
  ports=$(set_of "$INGRESS_PORTS")
  for s in $NTP_SERVERS; do ntp4+="${ntp4:+, }$s"; done
  [[ -n "$ntp4" ]] && ntp_rule="    ip daddr { $ntp4 } udp dport 123 accept"
  cat <<NFT
table inet ${TABLE} {
  chain output {
    type filter hook output priority filter; policy drop;

    ct state established,related accept
    oifname "lo" accept

    # Instance metadata: credentials live there. Named in the log.
    ip daddr 169.254.169.254 limit rate 10/second \\
      log prefix "SOVEREIGN-EGRESS-DENY IMDS " level warn drop
    ip6 daddr fd00:ec2::254 limit rate 10/second \\
      log prefix "SOVEREIGN-EGRESS-DENY IMDS " level warn drop

    # What the host needs to keep its address.
    udp sport 68 udp dport 67 accept
    udp sport 546 udp dport 547 accept
    icmpv6 type { nd-neighbor-solicit, nd-neighbor-advert, nd-router-solicit,
                  mld2-listener-report } accept
${ntp_rule}

    limit rate 20/second burst 40 packets \\
      log prefix "SOVEREIGN-EGRESS-DENY " level warn flags all
    counter drop
  }

  chain input {
    type filter hook input priority filter; policy drop;
    ct state established,related accept
    ct state invalid drop
    iifname "lo" accept
    tcp dport ${ports} ct state new accept
    udp sport 67 udp dport 68 accept
    udp sport 547 udp dport 546 accept
    icmp type { echo-request, destination-unreachable, time-exceeded } accept
    icmpv6 type { echo-request, destination-unreachable, packet-too-big,
                  time-exceeded, parameter-problem, nd-neighbor-solicit,
                  nd-neighbor-advert, nd-router-advert, mld-listener-query } accept
    counter drop
  }
}
NFT
}

load() {
  command -v nft >/dev/null || { echo "nftables (nft) is not installed." >&2; exit 1; }
  nft list table inet "$TABLE" >/dev/null 2>&1 && nft delete table inet "$TABLE"
  render | nft -f -
}

apply() {
  need_root
  local rollback=""
  if [[ "${1:-}" == "--rollback" ]]; then rollback="${2:-120}"; fi
  load
  echo "Sovereign egress policy applied: outbound DROP by default."
  echo "  inbound ports open: ${INGRESS_PORTS}"
  echo "  denied packets are logged: journalctl -k -g SOVEREIGN-EGRESS-DENY -f"
  if [[ -n "$rollback" ]]; then
    mkdir -p "$STATE_DIR"
    rm -f "$STATE_DIR/confirmed"
    ( sleep "$rollback"
      if [[ ! -f "$STATE_DIR/confirmed" ]]; then
        nft delete table inet "$TABLE" 2>/dev/null || true
        logger -t sovereign-egress "rollback: policy removed, not confirmed within ${rollback}s"
      fi ) >/dev/null 2>&1 &
    disown || true
    echo "  ROLLBACK ARMED: removed in ${rollback}s unless you run: sudo $0 confirm"
  fi
}

confirm() {
  need_root
  mkdir -p "$STATE_DIR" && touch "$STATE_DIR/confirmed"
  echo "Policy kept."
}

persist() {
  need_root
  load
  mkdir -p "$(dirname "$PERSIST_FILE")"
  render > "$PERSIST_FILE"
  cat > "$UNIT" <<UNITEOF
[Unit]
Description=Sovereign default-deny egress policy
DefaultDependencies=no
Before=network-pre.target
Wants=network-pre.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/sbin/nft -f ${PERSIST_FILE}
ExecStop=/usr/sbin/nft delete table inet ${TABLE}

[Install]
WantedBy=multi-user.target
UNITEOF
  systemctl daemon-reload
  systemctl enable sovereign-egress.service >/dev/null
  echo "Persisted: ${PERSIST_FILE}, reloaded at boot by sovereign-egress.service."
}

unpersist() {
  need_root
  systemctl disable --now sovereign-egress.service 2>/dev/null || true
  rm -f "$UNIT" "$PERSIST_FILE"
  systemctl daemon-reload
  echo "No longer applied at boot (the live table is unchanged; use remove)."
}

status() {
  if nft list table inet "$TABLE" 2>/dev/null; then
    echo
    echo "--- recent denials ---"
    journalctl -k -g SOVEREIGN-EGRESS-DENY -n 20 --no-pager 2>/dev/null \
      || echo "(kernel log unavailable to this user)"
  else
    echo "Sovereign table is NOT loaded (or not readable without root)."
    exit 1
  fi
}

remove() {
  need_root
  nft delete table inet "$TABLE" 2>/dev/null && echo "Sovereign egress policy removed." \
    || echo "Sovereign table was not loaded."
}

case "${1:-status}" in
  apply)     shift; apply "$@" ;;
  confirm)   confirm ;;
  persist)   persist ;;
  unpersist) unpersist ;;
  status)    status ;;
  remove)    remove ;;
  render)    render ;;
  *) echo "usage: $0 {apply [--rollback S]|confirm|persist|unpersist|status|remove|render}" >&2
     exit 2 ;;
esac
