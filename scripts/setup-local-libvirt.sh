#!/usr/bin/env bash
# Onboard the local-libvirt demo project so the first allocations.request is granted
# instead of dead-ending on quota_exceeded (#497).
#
# Runs the local-libvirt preflight, then seeds the demo project's budget + quota:
#   default       : python -m kdive seed-project  (token-less; the local host path)
#   audited (opt) : the role-gated accounting.set_quota / set_budget MCP tools, when
#                   KDIVE_SETUP_AUDITED=1 and KDIVE_MCP_BASE are set (needs an OIDC issuer
#                   configured to assert the project-admin claims and a KDIVE_TOKEN).
#
# DEMO ONLY: the bundled mock issuer mints a valid token for any caller. Never run the
# audited path against a real deployment; production onboards via the audited admin tools
# with a real token (see the project-onboarding guide).
#
# Env overrides: KDIVE_PROJECT (demo), KDIVE_LIMIT_KCU (1000000), KDIVE_MAX_ALLOC (4),
#   KDIVE_MAX_SYS (4); KDIVE_SETUP_AUDITED, KDIVE_MCP_BASE, KDIVE_TOKEN for the audited path.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "${SCRIPT_DIR}")"

# kdive/fastmcp live in the project venv, not the system python3. Prefer the repo's .venv when
# present (in-repo dev loop needs no env var), honor a KDIVE_PYTHON override (host-services
# deployment, e.g. /opt/kdive/.venv/bin/python), and fall back to system python3 (#1328).
_repo_venv_py="${REPO_ROOT}/.venv/bin/python"
if [[ -z "${KDIVE_PYTHON:-}" && -x "${_repo_venv_py}" ]]; then
  readonly PY="${_repo_venv_py}"
else
  readonly PY="${KDIVE_PYTHON:-python3}"
fi
unset _repo_venv_py
readonly PROJECT="${KDIVE_PROJECT:-demo}"
readonly LIMIT_KCU="${KDIVE_LIMIT_KCU:-1000000}"
readonly MAX_ALLOC="${KDIVE_MAX_ALLOC:-4}"
readonly MAX_SYS="${KDIVE_MAX_SYS:-4}"

main() {
  "${SCRIPT_DIR}/check-local-libvirt.sh"

  if [[ "${KDIVE_SETUP_AUDITED:-0}" == "1" ]]; then
    : "${KDIVE_MCP_BASE:?set KDIVE_MCP_BASE (…/mcp) for the audited path}"
    : "${KDIVE_TOKEN:?set KDIVE_TOKEN (project-admin) for the audited path}"
    (cd "${REPO_ROOT}" && "${PY}" -m scripts.kdive_set_accounting \
      --base "${KDIVE_MCP_BASE}" \
      --project "${PROJECT}" \
      --limit-kcu "${LIMIT_KCU}" \
      --max-concurrent-allocations "${MAX_ALLOC}" \
      --max-concurrent-systems "${MAX_SYS}")
    printf "onboarded project %s via audited admin tools\n" "${PROJECT}"
    return 0
  fi

  "${PY}" -m kdive seed-project \
    --project "${PROJECT}" \
    --limit-kcu "${LIMIT_KCU}" \
    --max-concurrent-allocations "${MAX_ALLOC}" \
    --max-concurrent-systems "${MAX_SYS}"
  printf "onboarded project %s via seed-project\n" "${PROJECT}"
}

main "$@"
