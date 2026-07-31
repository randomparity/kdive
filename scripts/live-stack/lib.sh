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

# `-ww` is required, not cosmetic: ps truncates each line to COLUMNS when that is set, and a
# checkout path plus " -m kdive reconciler" runs past 80 characters for any ordinary worktree. In
# an 80-column shell the untruncated form finds NO daemons, so stop_daemons reports none running
# and wait_for_daemons_to_settle counts zero alive — a bring-up that fails on a display setting.
# shellcheck disable=SC2054 # the commas are ps's own -o field separator, not array separators
_PS_WIDE=(ps -ww -eo "pid=,comm=,args=")

# PIDs of the real python daemons only — comm must be python, so a `bash -c '... kdive worker'`
# launcher wrapper (whose argv also matches) is excluded.
daemon_pids() {
  "${_PS_WIDE[@]}" | awk -v re="$_daemon_match" '$2 ~ /^python/ && $0 ~ re {print $1}'
}

# The worker subset of daemon_pids(), one pid per line. There is no per-worker identity in the
# argv — every worker runs the same `-m kdive worker` — so this counts them rather than naming
# them, which is all the build-stamp header needs to expose a stale log.
#
# Scoped to THIS checkout's interpreter, unlike daemon_pids: the two want opposite things. A
# bring-up must stop every kdive daemon on the host whatever checkout started it, so daemon_pids
# stays deliberately broad. A build-stamp count compared against rows of THIS log dir must not be
# inflated by a worker from a sibling worktree — several run on this host — or a dead worker's
# stale log reads as a live, graded process. `index()` is a literal match, so the interpreter path
# needs no regex escaping.
worker_pids() {
  "${_PS_WIDE[@]}" | awk -v needle="${py} -m kdive worker" \
    '$2 ~ /^python/ && index($0, needle) {print $1}'
}

stop_daemons() {
  local pids pid owner
  local -a remaining
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
  # One scan per poll, reused by the WARN. A second `daemon_pids` down there was a separate `ps`,
  # so the set it printed was not the set that decided to warn — the same double-scan skew fixed
  # in require_workers_alive later in this file, and this list is likewise what an operator would
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
# Ceilinged because every worker is a root process with its own database pool and its own aux
# port, and the loop that starts them asks for no confirmation. A transposition typo — the aux
# port 9470 typed into the count — would fork thousands of root processes on the operator's host.
# Every documented use is 2 or 3.
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
    echo "root process with its own database pool and aux health port; the live-testing runbook's" >&2
    echo "contention arm needs 2. Raise MAX_WORKER_COUNT in lib.sh if you genuinely need more." >&2
    return 1
  }
  printf '%s' "$count"
}

# Aux health/metrics listener bind (ADR-0090 §5) for worker <index>, empty for worker 1.
#
# uvicorn's bind is exclusive, so a second worker inheriting the first one's port dies at startup
# instead of claiming jobs — the failure this override exists to prevent. Worker 1 keeps the
# untouched environment so it lands on the registered per-process default 9465, where the ADR-0482
# skew preflight probes for it. Extras are numbered from their own base, clear of the whole
# registered block (server 9464, worker 9465, reconciler 9466), so worker 2 does not land on the
# reconciler.
#
# This is a fixed base rather than an offset from worker 1's *resolved* port because an explicit
# KDIVE_HEALTH_BIND_ADDR is refused outright above one worker (see restart_host_processes) — so
# worker 1's resolved port is always this default, and deriving from it would only add a way for
# an operator-chosen base to walk the extras onto the server's and reconciler's ports.
EXTRA_WORKER_HEALTH_PORT_BASE=9470

extra_worker_health_bind() {
  local index="$1"
  ((index > 1)) || return 0
  printf '127.0.0.1:%s' "$((EXTRA_WORKER_HEALTH_PORT_BASE + index - 2))"
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
    # env.sh source so the export is the last writer. That ordering is INERT today — env.sh never
    # mentions KDIVE_HEALTH_BIND_ADDR — and it is kept only because it costs nothing and would
    # become load-bearing the moment env.sh grew a `:-` default for it, like the vars above.
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
  # An explicit KDIVE_HEALTH_BIND_ADDR reaches the daemons unevenly here, so a multi-worker aux
  # layout cannot be placed deterministically under one. ADR-0090 §5 says an explicit value wins
  # for every process, but this bring-up does not deliver it to every process: the default root
  # worker is launched through `sudo bash -c`, which strips the environment and re-forwards only
  # the four variables named below — not this one — so a root worker 1 silently falls back to the
  # registered 9465, while a KDIVE_WORKER_AS_ROOT=0 worker 1 honours the operator's port. Worker 1
  # therefore lands somewhere that depends on a *different* knob, and the extras are numbered from
  # their own base regardless. Refuse rather than place ports on that footing.
  #
  # (An explicit bind is already unsound for the same-user server and reconciler at any count —
  # both do inherit it and both race one port. That is pre-existing and not gated here; this guard
  # covers only the multi-worker layout this file added.)
  if ((worker_count > 1)) && [[ -n "${KDIVE_HEALTH_BIND_ADDR:-}" ]]; then
    {
      echo "ERROR: KDIVE_WORKER_COUNT=${worker_count} and KDIVE_HEALTH_BIND_ADDR are incompatible."
      echo "  An explicit health bind does not reach every daemon here — sudo strips it from the"
      echo "  root worker, so worker 1's port depends on KDIVE_WORKER_AS_ROOT while the extra"
      echo "  workers are numbered from ${EXTRA_WORKER_HEALTH_PORT_BASE} regardless. Unset KDIVE_HEALTH_BIND_ADDR to run"
      echo "  more than one worker; every worker then takes a port this bring-up chose."
    } >&2
    return 1
  fi
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
  wait_for_daemons_to_settle || return 1
  require_workers_alive "$worker_count"
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
      # Above one worker there is a second cause with a very different remedy, and each worker
      # writes its own log so the failing one is identifiable. A worker whose aux port is held by
      # something foreign dies on an exclusive uvicorn bind, which leaves a bind traceback rather
      # than a database message — and the stack silently comes up with fewer workers than asked
      # for, which is the single-worker serialization a multi-worker run exists to escape.
      echo "on a multi-worker stack, an 'address already in use' traceback at the END of one" >&2
      echo "worker's own log (the root launch appends) means its aux health port" >&2
      echo "(${EXTRA_WORKER_HEALTH_PORT_BASE} and up) is taken, not that the backend is down" >&2
      return 1
    fi
  done
}

# Assert the stack really has the workers that were asked for, from THIS checkout.
#
# wait_for_daemons_to_settle counts a host-wide total against DAEMON_COUNT, and daemon_pids is
# deliberately checkout-agnostic, so a leftover daemon from another worktree — or a survivor of
# stop_daemons, which warns and returns 0 after ten seconds rather than failing — makes the total
# add up while one of this checkout's workers is missing. That is the silent degradation to fewer
# workers the whole knob exists to escape: bring-up would exit 0 and the contention arm would
# quietly measure serialization. Count workers specifically, and scoped, so the total cannot cover
# for the shortfall.
require_workers_alive() {
  local want="$1" have
  # ONE scan, reused. Counting with one `ps` and printing the pid list with a second let a worker
  # exit between them, so the message could report a count its own pid list contradicted — and
  # that list is what the remedy below tells the operator to act on.
  local -a pids
  mapfile -t pids < <(worker_pids)
  have="${#pids[@]}"
  ((have == want)) && return 0
  # Exact, not `>=`. A surplus is the failure this function was written about: stop_daemons warns
  # and returns 0 after ten seconds, and a worker does not act on SIGTERM until its job ends — so
  # a worker parked inside a multi-GiB fetch (which the contention arm creates deliberately)
  # routinely outlives it. That survivor is from THIS checkout and is counted here, so a `>=`
  # comparison lets it stand in for a new worker that died, which is exactly the substitution the
  # count exists to prevent. The two directions need different remedies, so they say different
  # things.
  if ((have > want)); then
    local pid_csv kill_cmd
    printf -v pid_csv '%s,' "${pids[@]}"
    pid_csv="${pid_csv%,}"
    # Mirror stop_daemons' ownership test (above) rather than prescribing `sudo` unconditionally:
    # under KDIVE_WORKER_AS_ROOT=0 the workers are the operator's own processes, and an operator
    # already root on a host without sudo installed cannot run the command at all. One prefix
    # covers the whole list either way — root may signal any of these pids, and if none is
    # root-owned the caller owns them all.
    kill_cmd="kill -9"
    if [[ "$(id -un)" != "root" ]] &&
      ps -o user= -p "$pid_csv" 2>/dev/null | awk '$1 == "root" { found = 1 } END { exit !found }'; then
      kill_cmd="sudo kill -9"
    fi
    # The remedy is deliberately NOT down.sh. It calls this same stop_daemons — one SIGTERM, a
    # ten-second poll, a WARN, `return 0`, no escalation — so the survivor this message is about
    # outlives it exactly as it outlived bring-up, and the compose backends come down for nothing.
    # (`--yes` gates only the --wipe prompt, so `down.sh --yes` is `down.sh` here.)
    #
    # Escalating to SIGKILL inside stop_daemons was the alternative, and is rejected because that
    # helper runs on EVERY bring-up, not just teardown: escalation would hard-kill a worker that is
    # legitimately mid-job every time anyone runs up.sh, discarding a multi-GiB fetch or a build
    # that was about to finish. Recovery is not free either — reclaiming the abandoned job spends
    # one of its bounded attempts. So the judgement call is the operator's; state both options and
    # what killing actually costs, rather than making the choice for them silently.
    #
    # That leaves teardown with no supported way to end a SIGTERM-ignoring worker, which is a real
    # gap and is tracked in #1733 — escalation scoped to down.sh, where it does not run on every
    # bring-up. Until that lands, the pids below are the operator's only handle.
    {
      echo "ERROR: asked for ${want} worker(s) but ${have} from this checkout are running."
      echo "  A worker from a previous stack outlived stop_daemons — it does not act on SIGTERM"
      echo "  until its current job ends, and the ten-second wait only warns. It may be running"
      echo "  older code, and it masks a new worker that failed to start."
      echo "  Live worker pids: ${pids[*]}"
      echo "  That list is every worker running under ${py}, INCLUDING the ones this run started."
      echo "  The survivors are whichever have the older start times. This step is diagnostic"
      echo "  only — the kill below ends every pid in the list, survivor or not:"
      echo "    ps -ww -o pid,lstart,etime,args -p ${pid_csv}"
      echo "  Tearing the stack down will NOT clear it: that path sends the same SIGTERM and"
      echo "  gives up the same way. So either wait for the in-flight job to finish and re-run"
      echo "  (that only helps if the SIGTERM landed — if it did not, the worker keeps claiming"
      echo "  new jobs and waiting never ends it; and this run's own workers above are claiming"
      echo "  jobs meanwhile, so a re-run can land on this same surplus), or end these in one"
      echo "  step and re-run:"
      echo "    ${kill_cmd} ${pids[*]}"
      echo "  Killing abandons those jobs mid-flight: another worker reclaims each one once its"
      echo "  lease lapses, spending one of its bounded attempts."
    } >&2
    return 1
  fi
  {
    echo "ERROR: asked for ${want} worker(s) but only ${have} from this checkout are running."
    echo "  Each worker writes its own log under ${log_dir}. The root launch APPENDS, so look at"
    echo "  each log's tail: the failing one ends in a traceback rather than in this run's startup"
    echo "  line. An 'address already in use' there means that worker's aux health port"
    echo "  (${EXTRA_WORKER_HEALTH_PORT_BASE} and up) is held by something else — a local port conflict, not a backend"
    echo "  problem."
  } >&2
  return 1
}

# worker-root.log is append-only, so report the LAST stamp of each service's own log. EVERY worker
# log present gets its own row: a skew check that reported one worker's stamp would pass on a
# multi-worker stack whose other workers are running different code (ADR-0482).
#
# The worker set comes from the log directory, not from KDIVE_WORKER_COUNT. status.sh is a bare
# read-only command an operator runs in a fresh shell long after bring-up, where that variable is
# gone — reading it there would report one worker for a stack started with several, which is the
# exact blind spot this per-worker reporting exists to close.
#
# Logs outlive processes, though: nothing prunes them, and the root launch appends, so a stack
# brought up with two workers and then with one leaves a `worker-root-2.log` whose last stamp still
# reads. Enumerating files alone would report that dead worker as live — the same blindness in the
# other direction. So the header states how many worker processes are ACTUALLY running: a row count
# above it means at least one row is a stale log, not a worker.
report_build_stamps() {
  local head_sha log found=0 name live
  local -A seen=()
  head_sha="$(git -C "$repo_root" rev-parse --short HEAD 2>/dev/null || echo '?')"
  live="$(worker_pids | grep -c . || true)"
  echo "=== build stamps (expect g${head_sha}; ${live} worker process(es) live) ==="
  _report_build_stamp server "${log_dir}/server.log"
  _report_build_stamp reconciler "${log_dir}/reconciler.log"
  # Each row is labelled by its own log's name rather than by a running counter, so it names the
  # file to read when a stamp looks wrong. Worker 1's two possible names lead, because the glob
  # alone sorts `worker-root-2.log` ahead of `worker-root.log`; `seen` dedupes the overlap.
  for log in "${log_dir}"/worker.log "${log_dir}"/worker-root.log "${log_dir}"/worker-*.log; do
    [[ -f "$log" ]] || continue
    [[ -n "${seen[$log]:-}" ]] && continue
    seen["$log"]=1
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
