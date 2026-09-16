#!/usr/bin/env bash
#
# Read-only health report for the local kdive infrastructure. No side effects.
# Usage: scripts/live-stack/stack-status.sh
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# A health report is the tool an operator reaches for when the host is broken, so it declares
# itself libvirt-free and reports an unresolved endpoint instead of aborting on one (ADR-0660).
# EXPORTED, because the `worker-lifecycle.sh status` call below is a child process that sources
# lib.sh and env.sh itself; without this its whole section would report the resolver's error.
export LIBVIRT_OPTIONAL=1
# shellcheck source=scripts/live-stack/lib.sh
source "${here}/lib.sh"
# shellcheck disable=SC1091 # repo-relative env script
source "${here}/env.sh"
cd "$repo_root"

echo "=== compose backends + obs ==="
docker compose ps --format 'table {{.Service}}\t{{.Status}}' \
  "${KDIVE_BACKEND_SERVICES[@]}" prometheus grafana 2>/dev/null || echo "  (docker compose unavailable)"

echo
echo "=== host daemons ==="
# $1 is the numeric PID column (drops the awk self-line + header); $2 is the username. `-ww` for
# the same reason lib.sh needs it: ps truncates to COLUMNS, and a checkout path plus the module
# argument runs past 80 characters, so a narrow shell would report no daemons on a healthy stack.
ps -ww -eo pid,user,args | awk -v re="$_daemon_match" '$0 ~ re && $1 ~ /^[0-9]+$/' || true
echo
report_build_stamps

echo "=== worker lifecycle ==="
"${here}/worker-lifecycle.sh" status || true
echo
echo "=== app health ==="
server_health || true

echo
echo "=== database ==="
if KDIVE_DATABASE_URL="${KDIVE_SERVER_DATABASE_URL}" \
  env -u KDIVE_MIGRATION_DATABASE_URL -u KDIVE_WORKER_DATABASE_URL \
  -u KDIVE_RECONCILER_DATABASE_URL \
  "$py" - <<PY 2>/dev/null; then
import os, sys
import psycopg

try:
    psycopg.connect(os.environ["KDIVE_DATABASE_URL"], connect_timeout=5).close()
except Exception as exc:  # noqa: BLE001 - status probe reports, does not raise
    print(f"  UNREACHABLE: {exc}")
    sys.exit(1)
print("  reachable")
PY
  :
else
  echo "  UNREACHABLE (see KDIVE_SERVER_DATABASE_URL)"
fi

echo
# The banner and the probe both read the endpoint — `libvirt_ok` reads it unguarded (lib.sh), and
# `set -u` is not suppressed inside an `if` condition — so both sit inside the resolved branch.
# provision_prereqs_ok below reads no libvirt and stays outside it: the qemu-img and overlay-dir
# report is the part a host with a broken contract still needs.
if [[ -n "${LIBVIRT_UNRESOLVED}" ]]; then
  echo "=== libvirt (endpoint unresolved) ==="
  echo "  ${LIBVIRT_UNRESOLVED}"
  echo "  daemon: NOT PROBED — repair the contract, or export KDIVE_LIBVIRT_URI, and re-run"
else
  echo "=== libvirt (${KDIVE_LIBVIRT_URI}) ==="
  if libvirt_ok; then
    echo "  daemon: reachable"
  else
    echo "  daemon: UNREACHABLE"
  fi
fi
if provision_prereqs_ok; then
  echo "  provision prereqs: qemu-img + ${KDIVE_ROOTFS_DIR} OK"
else
  echo "  provision prereqs: INCOMPLETE (see MISSING lines above)"
fi
