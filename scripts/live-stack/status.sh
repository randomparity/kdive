#!/usr/bin/env bash
#
# Read-only health report for the local kdive infrastructure. No side effects.
# Usage: scripts/live-stack/status.sh
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
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
# $1 is the numeric PID column (drops the awk self-line + header); $2 is the username.
ps -eo pid,user,args | awk -v re="$_daemon_match" '$0 ~ re && $1 ~ /^[0-9]+$/' || true
echo
report_build_stamps

echo
echo "=== app health ==="
server_health || true

echo
echo "=== database ==="
if "$py" - <<PY 2>/dev/null; then
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
  echo "  UNREACHABLE (see KDIVE_DATABASE_URL)"
fi

echo
echo "=== libvirt (${KDIVE_LIBVIRT_URI}) ==="
if libvirt_ok; then
  echo "  daemon: reachable"
else
  echo "  daemon: UNREACHABLE"
fi
if provision_prereqs_ok; then
  echo "  provision prereqs: qemu-img + ${KDIVE_ROOTFS_DIR} OK"
else
  echo "  provision prereqs: INCOMPLETE (see MISSING lines above)"
fi
