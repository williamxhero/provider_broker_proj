#!/usr/bin/env bash
set -euo pipefail

IPTABLES=/usr/sbin/iptables
allow_client=(-p tcp -d 192.168.50.2 --dport 8817 -s 192.168.50.1 -m comment --comment provider-broker-client -j ACCEPT)
allow_local=(-p tcp -d 192.168.50.2 --dport 8817 -s 192.168.50.2 -m comment --comment provider-broker-local -j ACCEPT)
deny_other=(-p tcp -d 192.168.50.2 --dport 8817 -m comment --comment provider-broker-deny -j REJECT)

remove_rule() {
  local -n rule=$1
  while "$IPTABLES" -C INPUT "${rule[@]}" 2>/dev/null; do
    "$IPTABLES" -D INPUT "${rule[@]}"
  done
}

case "${1:-}" in
  apply)
    "$IPTABLES" -C INPUT "${deny_other[@]}" 2>/dev/null || "$IPTABLES" -I INPUT 1 "${deny_other[@]}"
    "$IPTABLES" -C INPUT "${allow_local[@]}" 2>/dev/null || "$IPTABLES" -I INPUT 1 "${allow_local[@]}"
    "$IPTABLES" -C INPUT "${allow_client[@]}" 2>/dev/null || "$IPTABLES" -I INPUT 1 "${allow_client[@]}"
    ;;
  remove)
    remove_rule allow_client
    remove_rule allow_local
    remove_rule deny_other
    ;;
  *)
    echo "usage: $0 apply|remove" >&2
    exit 2
    ;;
esac
