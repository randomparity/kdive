#!/usr/bin/env bash
# Shared helpers for the local live-stack lifecycle scripts (up.sh, down.sh, status.sh,
# restart-stack.sh). SOURCED, never executed: it defines variables and functions and must
# have no side effects beyond that. Consumers source env.sh themselves when they need the
# KDIVE_* runtime config.

# scripts/live-stack/ -> repo root is two levels up (matches start.sh / restart-stack.sh).
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck disable=SC2034 # consumed by sourcing scripts
py="${repo_root}/.venv/bin/python"
log_dir="${KDIVE_STACK_LOG_DIR:-${repo_root}/.live-stack-logs}"

# Canonical backend compose services. NEVER the kdive:dev app tier (migrate/server/worker/
# reconciler) — the host processes own that tier.
# shellcheck disable=SC2034 # consumed by sourcing scripts
KDIVE_BACKEND_SERVICES=(postgres minio minio-init oidc)

# The local-libvirt provider connects here (KDIVE_LIBVIRT_URI, default qemu:///system) and
# stores per-System qcow2 overlays under KDIVE_ROOTFS_DIR. It uses user-mode SLIRP networking
# and qemu-img overlays — NO libvirt network or storage pool is involved.
KDIVE_LIBVIRT_URI="${KDIVE_LIBVIRT_URI:-qemu:///system}"
KDIVE_ROOTFS_DIR="${KDIVE_ROOTFS_DIR:-/var/lib/kdive/rootfs}"

# Matches the real daemon argv, e.g. ".venv/bin/python -m kdive server". `[.]` is a literal
# dot, so awk's dynamic-regex engine does not warn about an unescaped metacharacter.
_daemon_match='[.]venv/bin/python -m kdive (server|worker|reconciler)'

# PIDs of the real python daemons only — comm must be python, so a `bash -c '... kdive worker'`
# launcher wrapper (whose argv also matches) is excluded.
daemon_pids() {
  ps -eo pid=,comm=,args= | awk -v re="$_daemon_match" '$2 ~ /^python/ && $0 ~ re {print $1}'
}

stop_daemons() {
  local pids pid owner
  mapfile -t pids < <(daemon_pids)
  ((${#pids[@]})) || {
    echo "no kdive daemons running"
    return 0
  }
  echo "stopping kdive daemons: ${pids[*]}"
  for pid in "${pids[@]}"; do
    owner="$(ps -o user= -p "$pid" 2>/dev/null || true)"
    if [[ "$owner" == "root" && "$(id -un)" != "root" ]]; then
      sudo kill "$pid" 2>/dev/null || true
    else
      kill "$pid" 2>/dev/null || true
    fi
  done
  for _ in {1..20}; do
    [[ -z "$(daemon_pids)" ]] && return 0
    sleep 0.5
  done
  echo "WARN: daemons still running after stop: $(daemon_pids | tr '\n' ' ')" >&2
}

# worker-root.log is append-only, so report the LAST stamp of each service's own log.
report_build_stamps() {
  local head_sha entry stamp worker_log
  head_sha="$(git -C "$repo_root" rev-parse --short HEAD 2>/dev/null || echo '?')"
  worker_log="${log_dir}/worker.log"
  [[ -f "${log_dir}/worker-root.log" ]] && worker_log="${log_dir}/worker-root.log"
  echo "=== build stamps (expect g${head_sha}) ==="
  for entry in \
    "server:${log_dir}/server.log" \
    "reconciler:${log_dir}/reconciler.log" \
    "worker:${worker_log}"; do
    stamp="$(grep -h 'starting kdive' "${entry#*:}" 2>/dev/null | tail -1 |
      grep -oE 'g[0-9a-f]+ [(][a-z]+[)]' || true)"
    printf '  %-11s %s\n' "${entry%%:*}" "${stamp:-<no startup log line>}"
  done
}

# Returns 0 iff the host MCP server answers 401 (= up, auth required).
server_health() {
  local host port code
  host="${KDIVE_HTTP_HOST:-127.0.0.1}"
  port="${KDIVE_HTTP_PORT:-8000}"
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://${host}:${port}/mcp" || echo 000)"
  printf 'server http://%s:%s/mcp -> %s (401 = up, auth required)\n' "$host" "$port" "$code"
  [[ "$code" == "401" ]]
}

# libvirt daemon reachable. Sufficient as a libvirt signal: the provider needs no network/pool.
libvirt_ok() {
  virsh -c "$KDIVE_LIBVIRT_URI" list >/dev/null 2>&1
}

# The host prerequisites a local-libvirt provision actually needs. Returns 0 iff all are
# PRESENT (existence only — ownership/writability is the root worker's concern, not testable
# reliably as the invoking user). up.sh creates the dirs before calling this.
provision_prereqs_ok() {
  local rc=0 staging="${KDIVE_INSTALL_STAGING:-/var/lib/kdive/install}"
  command -v qemu-img >/dev/null 2>&1 || {
    echo "  MISSING: qemu-img on PATH (needed for rootfs overlays)" >&2
    rc=1
  }
  [[ -d "$KDIVE_ROOTFS_DIR" ]] || {
    echo "  MISSING: ${KDIVE_ROOTFS_DIR} (per-System qcow2 overlay dir)" >&2
    rc=1
  }
  [[ -d "$staging" ]] || {
    echo "  MISSING: ${staging} (KDIVE_INSTALL_STAGING)" >&2
    rc=1
  }
  return "$rc"
}

# Names of kdive-provisioned libvirt domains (kdive-<id>), one per line.
kdive_domains() {
  virsh -c "$KDIVE_LIBVIRT_URI" list --all --name 2>/dev/null | grep -E '^kdive-' || true
}
