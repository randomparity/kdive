#!/usr/bin/env bash
# Onboard the remote-libvirt demo project so the first allocations.request is granted
# instead of dead-ending on quota_exceeded (#497).
#
# Runs the remote-libvirt preflight against the target host, obtains a project-admin token
# (KDIVE_TOKEN if set, else scripts/demo-token.sh in-cluster), then seeds the demo project's
# budget + quota via the role-gated accounting.set_quota / set_budget MCP tools.
#
# KDIVE_MCP_BASE must point at the server's MCP endpoint and end in /mcp. The in-cluster
# server is ClusterIP-only, so port-forward first, e.g.:
#   kubectl port-forward svc/<release>-server 8000:8000
#   export KDIVE_MCP_BASE=http://127.0.0.1:8000/mcp
#
# DEMO ONLY: the bundled mock issuer mints a valid token for any caller. Never run against a
# real deployment; production supplies its own token via KDIVE_TOKEN.
#
# Usage: setup-remote-libvirt.sh HOST [USER] [URI]
# Env: KDIVE_MCP_BASE (required), KDIVE_TOKEN (optional; else demo-token.sh),
#   KDIVE_PROJECT (demo), KDIVE_LIMIT_KCU (1000000), KDIVE_MAX_ALLOC (4), KDIVE_MAX_SYS (4).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "${SCRIPT_DIR}")"

# fastmcp lives in the project venv, not the system python3. Override with KDIVE_PYTHON
# (e.g. /opt/kdive/.venv/bin/python) if you are not running inside the venv.
readonly PY="${KDIVE_PYTHON:-python3}"
readonly PROJECT="${KDIVE_PROJECT:-demo}"
readonly LIMIT_KCU="${KDIVE_LIMIT_KCU:-1000000}"
readonly MAX_ALLOC="${KDIVE_MAX_ALLOC:-4}"
readonly MAX_SYS="${KDIVE_MAX_SYS:-4}"

usage() {
  echo "usage: setup-remote-libvirt.sh HOST [USER] [URI]" >&2
}

main() {
  if [[ $# -lt 1 || "$1" == "-h" || "$1" == "--help" ]]; then
    usage
    return 1
  fi
  : "${KDIVE_MCP_BASE:?set KDIVE_MCP_BASE (…/mcp); port-forward the ClusterIP server first}"

  "${SCRIPT_DIR}/check-remote-libvirt.sh" "$@"

  local token="${KDIVE_TOKEN:-}"
  if [[ -z "${token}" ]]; then
    token="$("${SCRIPT_DIR}/demo-token.sh")"
  fi

  (cd "${REPO_ROOT}" && KDIVE_TOKEN="${token}" "${PY}" -m scripts.kdive_set_accounting \
    --base "${KDIVE_MCP_BASE}" \
    --project "${PROJECT}" \
    --limit-kcu "${LIMIT_KCU}" \
    --max-concurrent-allocations "${MAX_ALLOC}" \
    --max-concurrent-systems "${MAX_SYS}")
  printf "onboarded project %s via audited admin tools\n" "${PROJECT}"
}

main "$@"
