#!/usr/bin/env bash

# Client-facing host:port for a published backend port (#3215). The port variables also take
# compose's ADDR:PORT publish form. A loopback or wildcard address keeps the localhost every default
# URL used before (the OIDC issuer string is an identity), any other address becomes the URL host,
# and anything else stops the sourcing caller instead of producing a malformed URL.
backend_endpoint() {
  local name="$1" value="${!1}" addr="" port
  port="${value##*:}"
  [[ $value == *:* ]] && addr="${value%:*}"
  if [[ ! $port =~ ^[0-9]{1,5}$ ]] || ((10#$port < 1 || 10#$port > 65535)) ||
    [[ -n $addr && (! $addr =~ ^[][A-Za-z0-9.:-]+$ || ! $addr =~ [A-Za-z0-9]) ]]; then
    echo "${name}=${value} must be PORT or ADDR:PORT with a port from 1 to 65535" >&2
    return 1
  fi
  case "$addr" in
  "" | 127.0.0.1 | 0.0.0.0 | ::1 | :: | "[::1]" | "[::]") addr=localhost ;;
  \[*\]) ;;
  *:*) addr="[${addr}]" ;;
  esac
  printf '%s:%s' "$addr" "$port"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  backend_endpoint "$1"
fi
