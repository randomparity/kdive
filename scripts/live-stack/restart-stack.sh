#!/usr/bin/env bash
#
# Restart the host-run kdive stack (server + reconciler as the current user, worker as root)
# with the code in THIS checkout, against the already-running compose backends.
#
# Why this exists: the live-stack scripts track PIDs in .live-stack.pid, which drifts out of date
# for a hand-rolled bring-up (a stale PID once left the old worker running through a "restart").
# This script instead finds the live `python -m kdive` daemons by inspecting the process table,
# stops them (sudo for a root-owned worker), and starts fresh detached processes. It prints each
# service's build stamp and the server's health at the end so you can confirm the new code is live.
#
# Usage:
#   scripts/live-stack/restart-stack.sh   (run from the repo root)
#
# Env:
#   KDIVE_WORKER_AS_ROOT=0   run the worker as the current user instead of via sudo
#   KDIVE_KERNEL_SRC=<path>  warm-tree kernel source for the worker (default ~/src/linux)
#   KDIVE_BUILD_USER=<name>  unprivileged account the root worker drops to for local kernel
#                            builds (git clone + make); default: the invoking user. A root worker
#                            with this unset REFUSES the local build lane (deny-by-default, ADR-0214).
#   ...plus anything scripts/live-stack/env.sh honors (KDIVE_DATABASE_URL, KDIVE_HTTP_PORT, ...).
#
# Assumes the compose backends (Postgres/MinIO/OIDC) are already up (`just compose-up`).
set -euo pipefail

# This script lives in scripts/live-stack/, so the repo root is two levels up (matches start.sh).
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

py="${repo_root}/.venv/bin/python"
log_dir="${KDIVE_STACK_LOG_DIR:-${repo_root}/.live-stack-logs}"
worker_as_root="${KDIVE_WORKER_AS_ROOT:-1}"
# The unprivileged account a root worker drops to for local builds; without it the root worker
# refuses the local build lane (deny-by-default, ADR-0214). Defaults to whoever runs this script.
build_user="${KDIVE_BUILD_USER:-$(id -un)}"
# Matches the real daemon argv, e.g. ".venv/bin/python -m kdive server". `[.]` is a literal dot
# without a backslash escape, so awk's dynamic-regex engine does not warn about it.
match='[.]venv/bin/python -m kdive (server|worker|reconciler)'

[[ -x "$py" ]] || {
  echo "no venv python at ${py}; run 'just setup' first" >&2
  exit 1
}
mkdir -p "$log_dir"

# PIDs of the real python daemons only — comm must be python, so a `bash -c '... kdive worker'`
# launcher wrapper (whose argv also contains the pattern) is excluded.
daemon_pids() {
  ps -eo pid=,comm=,args= | awk -v re="$match" '$2 ~ /^python/ && $0 ~ re {print $1}'
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

start_user_daemon() {
  local name="$1"
  setsid nohup "$py" -m kdive "$name" >"${log_dir}/${name}.log" 2>&1 </dev/null &
}

start_worker() {
  local kernel_src="${KDIVE_KERNEL_SRC:-${HOME}/src/linux}"
  if [[ "$worker_as_root" == "1" && "$(id -un)" != "root" ]]; then
    # The worker needs root (install staging + libvirt/VM ops). Export KDIVE_KERNEL_SRC *before*
    # sourcing env.sh so env.sh honors it verbatim instead of defaulting to ${HOME}/src/linux,
    # which under sudo (HOME=/root) would silently point at a nonexistent /root/src/linux.
    sudo bash -c "cd '${repo_root}' \
      && export KDIVE_KERNEL_SRC='${kernel_src}' KDIVE_BUILD_USER='${build_user}' \
      && source scripts/live-stack/env.sh \
      && setsid nohup '${py}' -m kdive worker >>'${log_dir}/worker-root.log' 2>&1 </dev/null &"
  else
    KDIVE_KERNEL_SRC="$kernel_src" start_user_daemon worker
  fi
}

stop_daemons

echo "starting kdive stack @ $(git rev-parse --short HEAD 2>/dev/null || echo '?') ..."
# shellcheck disable=SC1091 # repo-relative env script resolved from this script's location
source scripts/live-stack/env.sh
start_user_daemon server
start_user_daemon reconciler
start_worker

sleep 5

head_sha="$(git rev-parse --short HEAD 2>/dev/null || echo '?')"
# worker-root.log is append-only, so report the LAST stamp of each service's own log (not a
# tail of the concatenation, which old appended worker lines would dominate).
worker_log="${log_dir}/worker.log"
[[ "$worker_as_root" == "1" && "$(id -un)" != "root" ]] && worker_log="${log_dir}/worker-root.log"

echo
echo "=== running kdive daemons ==="
ps -eo pid,user,lstart,args | awk -v re="$match" '$0 ~ re && $0 !~ /awk -v re=/'
echo
echo "=== build stamps (expect g${head_sha}) ==="
for entry in "server:${log_dir}/server.log" "reconciler:${log_dir}/reconciler.log" "worker:${worker_log}"; do
  stamp="$(grep -h "starting kdive" "${entry#*:}" 2>/dev/null | tail -1 |
    grep -oE 'g[0-9a-f]+ [(][a-z]+[)]' || true)"
  printf '  %-11s %s\n' "${entry%%:*}" "${stamp:-<no startup log line>}"
done
echo
host="${KDIVE_HTTP_HOST:-127.0.0.1}"
port="${KDIVE_HTTP_PORT:-8000}"
printf 'server http://%s:%s/mcp -> %s (401 = up, auth required)\n' "$host" "$port" \
  "$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://${host}:${port}/mcp" || echo 000)"
