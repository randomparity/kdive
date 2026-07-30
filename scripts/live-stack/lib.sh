#!/usr/bin/env bash
# Shared helpers for the local live-stack lifecycle scripts (up.sh, down.sh, status.sh).
# SOURCED, never executed: it defines variables and functions and must have no side effects
# beyond that. Consumers source env.sh themselves when they need the KDIVE_* runtime config.

# scripts/live-stack/ -> repo root is two levels up (matches the other scripts in this directory).
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
# KDIVE_PYTHON overrides the interpreter (the #1293 self-hosted CI job points it at /opt/kdive's
# libguestfs venv); unset, it stays the workspace .venv so operator use is unchanged.
py="${KDIVE_PYTHON:-${repo_root}/.venv/bin/python}"
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

# Fail (return 1) if KDIVE_HTTP_PORT is already held by a foreign listener, printing the holder so
# the operator can free it or relocate the port. Returns 0 when the port is free — or when `ss` is
# unavailable to inspect it (never block the stack on a missing diagnostic tool). MUST be called
# AFTER stop_daemons: a kdive server we just stopped has released its LISTEN socket, so anything
# still on the port is genuinely foreign (e.g. a podman vLLM container). Without this guard the
# host daemon loses the bind race and dies with a bare uvicorn sys.exit(1) buried in server.log.
require_free_http_port() {
  local port holder
  port="${KDIVE_HTTP_PORT:-8000}"
  command -v ss >/dev/null 2>&1 || return 0
  # `sport = :N` matches only that exact port (not :N-suffixed ports); -H drops the header, -p adds
  # the owning process when visible (a foreign owner's pid needs privilege, but the LISTEN line
  # still prints without it, so the bind is detected regardless). The match is port-wide (any
  # address), not scoped to KDIVE_HTTP_HOST: the common conflicts — a 0.0.0.0 or 127.0.0.1 listener —
  # genuinely collide with the server's bind, and erring toward a clear, actionable message beats
  # reverting to the silent bind-race death. A listener on a different specific IP over-reports; the
  # remediation (override KDIVE_HTTP_PORT) still applies.
  holder="$(ss -ltnpH "sport = :${port}" 2>/dev/null)"
  [[ -n "$holder" ]] || return 0
  {
    echo "ERROR: KDIVE_HTTP_PORT ${port} is already in use — the kdive server cannot bind it:"
    echo "  ${holder}"
    echo "Free that port, or relocate the stack, e.g.:"
    echo "    KDIVE_HTTP_PORT=8001 scripts/live-stack/up.sh"
  } >&2
  return 1
}

# How many `kdive worker` processes restart_host_processes() starts (KDIVE_WORKER_COUNT, default 1).
#
# A worker's claim loop runs jobs strictly one at a time, so a single worker gives the stack NO job
# concurrency: two MCP calls issued at the same instant queue behind each other. Worker
# *processes* are the concurrency unit — `queue.dequeue` claims under `FOR UPDATE SKIP LOCKED` so
# parallel workers take disjoint rows, job deadlines come from the database clock so no worker
# clocks need to agree, and `accepted_lanes` bounds what each one dispatches. Raising this is
# therefore the only way to make the local stack exercise a cross-worker path — the
# per-(investigation, checksum) rootfs fetch advisory lock is the one the live-testing runbook
# drives. Default 1 keeps the ordinary stack byte-identical to the single-worker shape.
configured_worker_count() {
  local count="${KDIVE_WORKER_COUNT:-1}"
  [[ "$count" =~ ^[1-9][0-9]*$ ]] || {
    echo "KDIVE_WORKER_COUNT must be a positive integer, got '${count}'" >&2
    return 1
  }
  printf '%s' "$count"
}

# Aux health/metrics listener bind (ADR-0090 §5) for worker <index>, empty for worker 1.
#
# uvicorn's bind is exclusive, so a second worker inheriting the first one's port dies at startup
# instead of claiming jobs — the failure this override exists to prevent. Worker 1 keeps the
# untouched environment so it lands on whatever ADR-0090 §5 resolves for it: the registered
# per-process default 9465, where the ADR-0482 skew preflight probes, or an explicit
# KDIVE_HEALTH_BIND_ADDR, which wins for every process.
#
# Extras step off WORKER 1's resolved port, not off a constant — an operator who sets
# KDIVE_HEALTH_BIND_ADDR would otherwise have their port honored for worker 1 and silently
# discarded for the rest, landing two workers on one port again if the value they chose happened
# to be the constant. The gap clears the whole registered block (server 9464, worker 9465,
# reconciler 9466) so the default stack's worker 2 does not land on the reconciler.
EXTRA_WORKER_HEALTH_PORT_GAP=5

extra_worker_health_bind() {
  local index="$1" addr
  ((index > 1)) || return 0
  addr="${KDIVE_HEALTH_BIND_ADDR:-127.0.0.1:9465}"
  printf '%s:%s' "${addr%:*}" "$((${addr##*:} + EXTRA_WORKER_HEALTH_PORT_GAP + index - 2))"
}

# Log file for worker <index>. Worker 1 keeps the historical unsuffixed name so the runbooks and
# recorded proof runs that name `worker-root.log` still point at a real file; each additional
# worker gets its own so an observation is attributable to one process rather than an interleave.
worker_log_path() {
  local index="$1" suffix="" stem="worker"
  ((index > 1)) && suffix="-${index}"
  [[ "${KDIVE_WORKER_AS_ROOT:-1}" == "1" && "$(id -un)" != "root" ]] && stem="worker-root"
  printf '%s/%s%s.log' "$log_dir" "$stem" "$suffix"
}

# Start ONE `kdive worker`, indexed from 1. Split out of restart_host_processes so the root and
# non-root launches stay a single branch while the caller loops over the configured worker count.
start_worker() {
  local index="$1" build_user="$2" kernel_src="$3"
  local log health_bind health_export=""
  log="$(worker_log_path "$index")"
  health_bind="$(extra_worker_health_bind "$index")"
  if [[ "${KDIVE_WORKER_AS_ROOT:-1}" == "1" && "$(id -un)" != "root" ]]; then
    # The worker needs root (install staging + libvirt/VM ops). sudo resets the environment, so any
    # override the invoking user set is stripped before env.sh re-runs under sudo and re-defaults it.
    # Forward the vars the root worker actually consumes so env.sh honors them verbatim (via its
    # `:-` defaults) instead of silently reverting: KDIVE_KERNEL_SRC (else HOME=/root points at a
    # nonexistent /root/src/linux) and the resolved backend endpoints KDIVE_DATABASE_URL +
    # KDIVE_S3_ENDPOINT_URL — without these a relocated KDIVE_POSTGRES_PORT/KDIVE_MINIO_PORT would
    # leave the worker connecting to the default host ports (nothing published there) while the
    # same-user server/reconciler use the overridden ones. The health-bind export lands AFTER the
    # env.sh source so it overrides, not precedes, that file's defaulting.
    [[ -n "$health_bind" ]] && health_export="export KDIVE_HEALTH_BIND_ADDR='${health_bind}' && "
    sudo bash -c "cd '${repo_root}' \
      && export KDIVE_KERNEL_SRC='${kernel_src}' KDIVE_BUILD_USER='${build_user}' \
      && export KDIVE_DATABASE_URL='${KDIVE_DATABASE_URL}' KDIVE_S3_ENDPOINT_URL='${KDIVE_S3_ENDPOINT_URL}' \
      && source scripts/live-stack/env.sh \
      && ${health_export}setsid nohup '${py}' -m kdive worker >>'${log}' 2>&1 </dev/null &"
  else
    local -a worker_env=(KDIVE_KERNEL_SRC="$kernel_src")
    [[ -n "$health_bind" ]] && worker_env+=(KDIVE_HEALTH_BIND_ADDR="$health_bind")
    env "${worker_env[@]}" setsid nohup "$py" -m kdive worker >"$log" 2>&1 </dev/null &
  fi
}

# Restart the host-run kdive daemons with the code in THIS checkout: server + reconciler as the
# invoking user, worker as root (unless KDIVE_WORKER_AS_ROOT=0) for install-staging + VM ops.
# Stops live daemons found in the process table first. Assumes env.sh is already sourced and the
# compose backends are up. Env: KDIVE_WORKER_AS_ROOT (default 1), KDIVE_BUILD_USER (default
# invoking user; a root worker REFUSES the local build lane without it — ADR-0214), KDIVE_KERNEL_SRC,
# KDIVE_WORKER_COUNT (default 1; see configured_worker_count).
restart_host_processes() {
  local build_user="${KDIVE_BUILD_USER:-$(id -un)}"
  local kernel_src="${KDIVE_KERNEL_SRC:-${HOME}/src/linux}"
  local worker_count index
  worker_count="$(configured_worker_count)" || return 1
  [[ -x "$py" ]] || {
    echo "no venv python at ${py}; run 'just setup' first" >&2
    return 1
  }
  mkdir -p "$log_dir"
  stop_daemons
  # Stopped our own daemons above; anything still on KDIVE_HTTP_PORT is foreign — fail loudly rather
  # than let the new server lose the bind race and die silently.
  require_free_http_port || return 1
  echo "starting kdive host processes (${worker_count} worker(s)) @ $(git -C "$repo_root" rev-parse --short HEAD 2>/dev/null || echo '?') ..."
  setsid nohup "$py" -m kdive server >"${log_dir}/server.log" 2>&1 </dev/null &
  setsid nohup "$py" -m kdive reconciler >"${log_dir}/reconciler.log" 2>&1 </dev/null &
  for ((index = 1; index <= worker_count; index++)); do
    start_worker "$index" "$build_user" "$kernel_src"
  done
  DAEMON_COUNT="$((2 + worker_count))"
  wait_for_daemons_to_settle
}

# Host processes have no supervisor — unlike the systemd units and the compose/Helm surfaces,
# nothing restarts them. So bring-up has to outlast their own startup budget before it can call
# the stack up: each daemon waits up to POOL_OPEN_TIMEOUT_SECONDS for its first database
# connection and exits if it cannot get one, plus a bounded pool teardown (ADR-0449, ~11s total).
# The former flat `sleep 5` returned while a doomed daemon was still in the process table, so
# status.sh reported three healthy processes and up.sh exited 0 for a stack that vanished seconds
# later. The most reachable trigger is the sudo-root-worker env footgun above: a KDIVE_DATABASE_URL
# the root worker cannot reach now kills it outright instead of showing up as a not-ready /readyz.
DAEMON_SETTLE_SECONDS=15
# server + reconciler + KDIVE_WORKER_COUNT workers. restart_host_processes() recomputes this from
# the count it resolved; the initializer is the one-worker stack so a consumer that only reads it
# still sees the ordinary shape.
DAEMON_COUNT=3

wait_for_daemons_to_settle() {
  local elapsed alive
  for ((elapsed = 0; elapsed < DAEMON_SETTLE_SECONDS; elapsed++)); do
    sleep 1
    alive="$(daemon_pids | grep -c . || true)"
    if ((alive < DAEMON_COUNT)); then
      echo "kdive host processes exited during startup (${alive}/${DAEMON_COUNT} alive)" >&2
      echo "check ${log_dir}/*.log — 'no database connection within' means the backend was" >&2
      echo "unreachable, or its credentials or database name are wrong" >&2
      return 1
    fi
  done
}

# worker-root.log is append-only, so report the LAST stamp of each service's own log. EVERY worker
# log present gets its own row: a skew check that reported one worker's stamp would pass on a
# multi-worker stack whose other workers are running different code (ADR-0482).
#
# The worker set comes from the log directory, not from KDIVE_WORKER_COUNT. status.sh is a bare
# read-only command an operator runs in a fresh shell long after bring-up, where that variable is
# gone — reading it there would report one worker for a stack started with several, which is the
# exact blind spot this per-worker reporting exists to close. A row whose stamp is stale relative
# to the `=== host daemons ===` process list above it is visible; a row that was never printed is
# not.
report_build_stamps() {
  local head_sha log found=0 name
  head_sha="$(git -C "$repo_root" rev-parse --short HEAD 2>/dev/null || echo '?')"
  echo "=== build stamps (expect g${head_sha}) ==="
  _report_build_stamp server "${log_dir}/server.log"
  _report_build_stamp reconciler "${log_dir}/reconciler.log"
  # Each row is labelled by its own log's name rather than by a running counter, so the label
  # survives any glob ordering and names the file to read when a stamp looks wrong.
  for log in "${log_dir}"/worker.log "${log_dir}"/worker-*.log; do
    [[ -f "$log" ]] || continue
    found=1
    name="${log##*/}"
    _report_build_stamp "${name%.log}" "$log"
  done
  ((found)) || _report_build_stamp worker "$(worker_log_path 1)"
}

_report_build_stamp() {
  local stamp
  stamp="$(grep -h 'starting kdive' "$2" 2>/dev/null | tail -1 |
    grep -oE 'g[0-9a-f]+ [(][a-z]+[)]' || true)"
  printf '  %-11s %s\n' "$1" "${stamp:-<no startup log line>}"
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
