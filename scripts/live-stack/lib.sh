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

# Matches ordinary host daemon argv, e.g. ".venv/bin/python -m kdive server". `[.]` is a literal
# dot, so awk's dynamic-regex engine does not warn about an unescaped metacharacter.
_daemon_match='[.]venv/bin/python -m kdive (server|reconciler)'

# `-ww` is required, not cosmetic: ps truncates each line to COLUMNS when that is set, and a
# checkout path plus " -m kdive reconciler" runs past 80 characters for any ordinary worktree. In
# an 80-column shell the untruncated form finds NO daemons, so stop_daemons reports none running
# and wait_for_daemons_to_settle counts zero alive — a bring-up that fails on a display setting.
# shellcheck disable=SC2054 # the commas are ps's own -o field separator, not array separators
_PS_WIDE=(ps -ww -eo "pid=,comm=,args=")

# PIDs of the real python daemons only — comm must be python, so a `bash -c` launcher
# launcher wrapper (whose argv also matches) is excluded.
daemon_pids() {
  "${_PS_WIDE[@]}" | awk -v re="$_daemon_match" '$2 ~ /^python/ && $0 ~ re {print $1}'
}

# Does signalling every pid in <pid-csv> need sudo? Returns 0 (yes) or 1 (a bare kill suffices).
#
# The test is "is any pid NOT mine", never "is any pid root's". A daemon left by another account on
# a shared host is exactly as unsignalable as a root-owned one, and both pid scans above match on
# interpreter path rather than on owner, so either can list one; a root-only test sends the caller
# to a bare kill that dies with EPERM. Numeric uids, because `ps -o user=` truncates a name past
# eight characters and would then mis-compare a long-named caller against itself. root needs sudo
# for nothing, so it short-circuits before spawning ps.
#
# Unknown ownership answers YES. If ps reports no row for a pid the scan just listed, the usual
# cause is that the process exited in between — where both forms are equally harmless no-ops — and
# the only other cause is a ps that cannot report uids. There `sudo kill` still works on a
# self-owned process, while a bare `kill` does not work on a foreign one, so the unknown case is
# safe in exactly one direction. Only a row actually read licenses dropping sudo. ps is read into a
# variable rather than piped, because these scripts run under `set -o pipefail`, where a pipeline's
# status would come from ps's own exit-1-on-no-match and silently invert that decision.
pids_need_sudo() {
  local rows
  ((EUID != 0)) || return 1
  rows="$(ps -o uid= -p "$1" 2>/dev/null || true)"
  [[ -z "${rows//[[:space:]]/}" ]] && return 0
  awk -v me="$EUID" '$1 != me { foreign = 1 } END { exit !foreign }' <<<"$rows"
}

# Set by stop_daemons so its caller can distinguish a process which ignored SIGTERM from one the
# caller could not signal. Process-local by design: the value describes only the latest stop pass.
STOP_DAEMONS_UNSIGNALLED=()

stop_daemons() {
  local pids pid
  local -a remaining
  STOP_DAEMONS_UNSIGNALLED=()
  mapfile -t pids < <(daemon_pids)
  ((${#pids[@]})) || {
    echo "no kdive daemons running"
    return 0
  }
  echo "stopping kdive daemons: ${pids[*]}"
  for pid in "${pids[@]}"; do
    if pids_need_sudo "$pid"; then
      sudo kill "$pid" 2>/dev/null || STOP_DAEMONS_UNSIGNALLED+=("$pid")
    else
      kill "$pid" 2>/dev/null || STOP_DAEMONS_UNSIGNALLED+=("$pid")
    fi
  done
  if ((${#STOP_DAEMONS_UNSIGNALLED[@]})); then
    echo "WARN: SIGTERM was not delivered to: ${STOP_DAEMONS_UNSIGNALLED[*]}" >&2
  fi
  # One scan per poll, reused by the WARN. A second `daemon_pids` down there was a separate `ps`,
  # so the set it printed was not the set that decided to warn — the same double-scan skew fixed
  # in the startup status check later in this file, and this list is likewise what an operator would
  # act on. Sleep FIRST so the surviving scan is the last thing observed rather than one already
  # half a second stale. The price is one extra 0.5s poll when every daemon exits immediately; the
  # gain is that the pids the WARN hands the operator were still there when it decided to warn.
  for _ in {1..20}; do
    sleep 0.5
    mapfile -t remaining < <(daemon_pids)
    ((${#remaining[@]})) || return 0
  done
  echo "WARN: daemons still running after stop: ${remaining[*]}" >&2
}

# End daemons which remain after stop_daemons' grace period. This is deliberately a separate
# helper: stop_daemons also runs during bring-up, where escalation would abandon legitimate work.
force_stop_daemons() {
  local current pid still_daemon
  local -a pids remaining unsignalled=()
  mapfile -t pids < <(daemon_pids)
  ((${#pids[@]})) || return 0
  echo "force-stopping kdive daemons: ${pids[*]}"
  for pid in "${pids[@]}"; do
    # Discovery and signalling cannot be atomic in portable shell. Narrow the PID-reuse window by
    # requiring the pid to match the daemon argv again immediately before the irreversible signal.
    still_daemon=0
    while read -r current; do
      [[ "$current" == "$pid" ]] && still_daemon=1
    done < <(daemon_pids)
    ((still_daemon)) || continue
    if pids_need_sudo "$pid"; then
      sudo kill -9 "$pid" 2>/dev/null || unsignalled+=("$pid")
    else
      kill -9 "$pid" 2>/dev/null || unsignalled+=("$pid")
    fi
  done
  if ((${#unsignalled[@]})); then
    echo "ERROR: SIGKILL was not delivered to: ${unsignalled[*]}" >&2
    return 1
  fi
  for _ in {1..20}; do
    sleep 0.5
    mapfile -t remaining < <(daemon_pids)
    ((${#remaining[@]})) || return 0
  done
  echo "ERROR: daemons still running after SIGKILL: ${remaining[*]}" >&2
  return 1
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
#
# Ceilinged because every worker has its own database pool and auxiliary health port. A
# transposition typo must not ask the lifecycle witness to activate thousands of slots.
MAX_WORKER_COUNT=8

configured_worker_count() {
  local count="${KDIVE_WORKER_COUNT:-1}"
  [[ "$count" =~ ^[1-9][0-9]*$ ]] || {
    echo "KDIVE_WORKER_COUNT must be a positive integer, got '${count}'" >&2
    return 1
  }
  # Bound the MAGNITUDE before doing any arithmetic on it. The regex above bounds sign and format
  # but not size, and bash arithmetic is 64-bit signed, so any value past int64 silently wraps and
  # the comparison then judges the wrapped number rather than the one the operator typed: 2^63
  # lands on INT64_MIN, 2^64 on 0, and 2^64+1 .. 2^64+8 land inside the accepted range. No numeric
  # comparison can screen those, because by the time `((...))` sees the value it has already
  # wrapped — hence the digit-count test, which is exact here and needs no arithmetic at all: the
  # regex forbids a leading zero, so for two decimal strings more digits means strictly greater.
  #
  # Left unbounded this was not a cosmetic slip. The unwrapped string flowed on to the launch loop,
  # whose `index <= count` ran zero times, and to `DAEMON_COUNT="$((2 + count))"`, which went
  # negative and made the settle gate's `alive < DAEMON_COUNT` unreachable — so a stack with no
  # workers at all reached the SURPLUS branch below and reported one for a condition that was not
  # occurring.
  ((${#count} <= ${#MAX_WORKER_COUNT} && count <= MAX_WORKER_COUNT)) || {
    echo "KDIVE_WORKER_COUNT=${count} is outside 1..${MAX_WORKER_COUNT}. A value past the ceiling —" >&2
    echo "or one so large it wraps bash's signed 64-bit arithmetic — is refused: each worker is a" >&2
    echo "worker process with its own database pool and aux health port; the live-testing runbook's" >&2
    echo "contention arm needs 2. Raise MAX_WORKER_COUNT in lib.sh if you genuinely need more." >&2
    return 1
  }
  printf '%s' "$count"
}

# Restart ordinary host daemons and route every worker through the fixed lifecycle witness.
# Assumes env.sh is sourced and compose backends are up. KDIVE_WORKER_COUNT stays within 1..8.
restart_host_processes() {
  local worker_count
  worker_count="$(configured_worker_count)" || return 1
  [[ -x "$py" ]] || {
    echo "no venv python at ${py}; run 'just setup' first" >&2
    return 1
  }
  mkdir -p "$log_dir"
  "${repo_root}/scripts/live-stack/worker-lifecycle.sh" diagnostics || return 1
  "${repo_root}/scripts/live-stack/worker-lifecycle.sh" stop || return 1
  stop_daemons
  # Stopped our own daemons above; anything still on KDIVE_HTTP_PORT is foreign — fail loudly rather
  # than let the new server lose the bind race and die silently.
  require_free_http_port || return 1
  local revision
  revision="$(git -C "$repo_root" rev-parse --short HEAD 2>/dev/null || echo '?')"
  echo "starting kdive host processes (${worker_count} lifecycle worker(s)) @ ${revision} ..."
  KDIVE_DATABASE_URL="${KDIVE_SERVER_DATABASE_URL}" \
    env -u KDIVE_MIGRATION_DATABASE_URL -u KDIVE_WORKER_DATABASE_URL \
    -u KDIVE_RECONCILER_DATABASE_URL \
    setsid nohup "$py" -m kdive server >"${log_dir}/server.log" 2>&1 </dev/null &
  KDIVE_DATABASE_URL="${KDIVE_RECONCILER_DATABASE_URL}" \
    env -u KDIVE_MIGRATION_DATABASE_URL -u KDIVE_SERVER_DATABASE_URL \
    -u KDIVE_WORKER_DATABASE_URL \
    setsid nohup "$py" -m kdive reconciler >"${log_dir}/reconciler.log" 2>&1 </dev/null &
  "${repo_root}/scripts/live-stack/worker-lifecycle.sh" start "$worker_count"
  DAEMON_COUNT=2
  wait_for_daemons_to_settle || return 1
  KDIVE_LIFECYCLE_EXPECTED_SLOTS="$worker_count" \
    "${repo_root}/scripts/live-stack/worker-lifecycle.sh" status
}

# Host processes have no supervisor — unlike the systemd units and the compose/Helm surfaces,
# nothing restarts them. So bring-up has to outlast their own startup budget before it can call
# the stack up: each daemon waits up to POOL_OPEN_TIMEOUT_SECONDS for its first database
# connection and exits if it cannot get one, plus a bounded pool teardown (ADR-0449, ~11s total).
# The former flat `sleep 5` returned while a doomed daemon was still in the process table, so
# status.sh reported three healthy processes and up.sh exited 0 for a stack that vanished seconds
# later. The most reachable trigger is the sudo-root-worker env footgun above: a KDIVE_DATABASE_URL
# an unavailable role-specific database now kills it outright instead of showing up as a not-ready
# /readyz.
DAEMON_SETTLE_SECONDS=15
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

report_build_stamps() {
  local head_sha
  head_sha="$(git -C "$repo_root" rev-parse --short HEAD 2>/dev/null || echo '?')"
  echo "=== build stamps (expect g${head_sha}) ==="
  _report_build_stamp server "${log_dir}/server.log"
  _report_build_stamp reconciler "${log_dir}/reconciler.log"
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
# PRESENT (existence only — ownership/writability is the lifecycle witness's concern, not testable
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
