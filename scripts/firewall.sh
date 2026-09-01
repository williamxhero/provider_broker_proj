#!/usr/bin/env bash
set -euo pipefail

IPTABLES=/usr/sbin/iptables
ports=(8817 8818)

remove_rule() {
  local port=$1 suffix=$2 source=${3:-}
  local rule=(-p tcp -d 192.168.50.2 --dport "$port")
  [[ -n "$source" ]] && rule+=(-s "$source")
  rule+=(-m comment --comment "provider-broker-${suffix}-${port}" -j "${4:-REJECT}")
  while "$IPTABLES" -C INPUT "${rule[@]}" 2>/dev/null; do
    "$IPTABLES" -D INPUT "${rule[@]}"
  done
}

apply_rule() {
  local port=$1 suffix=$2 source=${3:-} action=${4:-REJECT}
  local rule=(-p tcp -d 192.168.50.2 --dport "$port")
  [[ -n "$source" ]] && rule+=(-s "$source")
  rule+=(-m comment --comment "provider-broker-${suffix}-${port}" -j "$action")
  "$IPTABLES" -C INPUT "${rule[@]}" 2>/dev/null || "$IPTABLES" -I INPUT 1 "${rule[@]}"
}

case "${1:-}" in
  apply)
    for port in "${ports[@]}"; do
      apply_rule "$port" deny "" REJECT
      apply_rule "$port" local 192.168.50.2 ACCEPT
      apply_rule "$port" client 192.168.50.1 ACCEPT
    done
    ;;
  remove)
    for port in "${ports[@]}"; do
      remove_rule "$port" client 192.168.50.1 ACCEPT
      remove_rule "$port" local 192.168.50.2 ACCEPT
      remove_rule "$port" deny "" REJECT
    done
    ;;
  *)
    echo "usage: $0 apply|remove" >&2
    exit 2
    ;;
esac
