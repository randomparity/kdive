#!/usr/bin/env bash
# Shared helpers for the local live-stack lifecycle scripts (up.sh, down.sh, status.sh).
# SOURCED, never executed: it defines variables and functions and must have no side effects
# beyond that. Consumers source env.sh themselves when they need the KDIVE_* runtime config.

# scripts/live-stack/ -> repo root is two levels up (matches the other scripts in this directory).
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
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

# Arches for which grafana publishes no upstream manifest (ADR-0356 accept-gap, #1261); it ships
# amd64 + arm64 only. On a listed arch, up.sh skips grafana and brings prometheus (which does
# publish ppc64le) up on its own, so a missing-manifest pull can't abort the metrics store.
GRAFANA_UNSUPPORTED_ARCHES=(ppc64le)

# Returns 0 if grafana publishes an image for the given `uname -m` arch (start it), 1 if not
# (skip it). An empty/unknown arch is treated as supported, so a host where `uname` is absent
# still attempts grafana best-effort rather than silently skipping it.
grafana_supports_arch() {
  local candidate
  for candidate in "${GRAFANA_UNSUPPORTED_ARCHES[@]}"; do
    [[ "$candidate" == "$1" ]] && return 1
  done
  return 0
}

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

# Restart the host-run kdive daemons with the code in THIS checkout: server + reconciler as the
# invoking user, worker as root (unless KDIVE_WORKER_AS_ROOT=0) for install-staging + VM ops.
# Stops live daemons found in the process table first. Assumes env.sh is already sourced and the
# compose backends are up. Env: KDIVE_WORKER_AS_ROOT (default 1), KDIVE_BUILD_USER (default
# invoking user; a root worker REFUSES the local build lane without it — ADR-0214), KDIVE_KERNEL_SRC.
restart_host_processes() {
  local worker_as_root="${KDIVE_WORKER_AS_ROOT:-1}"
  local build_user="${KDIVE_BUILD_USER:-$(id -un)}"
  local kernel_src="${KDIVE_KERNEL_SRC:-${HOME}/src/linux}"
  [[ -x "$py" ]] || {
    echo "no venv python at ${py}; run 'just setup' first" >&2
    return 1
  }
  mkdir -p "$log_dir"
  stop_daemons
  echo "starting kdive host processes @ $(git -C "$repo_root" rev-parse --short HEAD 2>/dev/null || echo '?') ..."
  setsid nohup "$py" -m kdive server >"${log_dir}/server.log" 2>&1 </dev/null &
  setsid nohup "$py" -m kdive reconciler >"${log_dir}/reconciler.log" 2>&1 </dev/null &
  if [[ "$worker_as_root" == "1" && "$(id -un)" != "root" ]]; then
    # The worker needs root (install staging + libvirt/VM ops). Export KDIVE_KERNEL_SRC *before*
    # sourcing env.sh so env.sh honors it verbatim instead of defaulting to ${HOME}/src/linux,
    # which under sudo (HOME=/root) would silently point at a nonexistent /root/src/linux.
    sudo bash -c "cd '${repo_root}' \
      && export KDIVE_KERNEL_SRC='${kernel_src}' KDIVE_BUILD_USER='${build_user}' \
      && source scripts/live-stack/env.sh \
      && setsid nohup '${py}' -m kdive worker >>'${log_dir}/worker-root.log' 2>&1 </dev/null &"
  else
    KDIVE_KERNEL_SRC="$kernel_src" setsid nohup "$py" -m kdive worker >"${log_dir}/worker.log" 2>&1 </dev/null &
  fi
  sleep 5
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
  # Ride out FastMCP startup latency (server.log shows ~30–40s from process spawn to accepting
  # requests). Fail-open after the deadline so status reports honestly instead of hanging.
  #
  # `|| true` (not `|| echo 000`): curl already prints "000" to stdout on connection failure via
  # `-w %{http_code}`, so a fallback `echo 000` would DOUBLE the code into "000000" — the visible
  # bug we are fixing. `|| true` keeps set -e happy without appending to the captured stdout.
  for _ in {1..30}; do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 "http://${host}:${port}/mcp" 2>/dev/null || true)"
    [[ "$code" == "401" ]] && break
    sleep 1
  done
  printf 'server http://%s:%s/mcp -> %s (401 = up, auth required)\n' "$host" "$port" "${code:-000}"
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
