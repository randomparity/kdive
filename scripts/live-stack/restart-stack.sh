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

# Shared helpers (repo_root, py, log_dir, daemon_pids, stop_daemons, report_build_stamps,
# server_health). lib.sh resolves repo_root from its own location.
# shellcheck source=scripts/live-stack/lib.sh
# shellcheck disable=SC1091 # path resolved at runtime; shellcheck -x follows it correctly
source "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
cd "$repo_root"

worker_as_root="${KDIVE_WORKER_AS_ROOT:-1}"
# The unprivileged account a root worker drops to for local builds; without it the root worker
# refuses the local build lane (deny-by-default, ADR-0214). Defaults to whoever runs this script.
build_user="${KDIVE_BUILD_USER:-$(id -un)}"

[[ -x "$py" ]] || {
  echo "no venv python at ${py}; run 'just setup' first" >&2
  exit 1
}
mkdir -p "$log_dir"

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

echo "starting kdive stack @ $(git -C "$repo_root" rev-parse --short HEAD 2>/dev/null || echo '?') ..."
# shellcheck disable=SC1091 # repo-relative env script resolved from this script's location
source scripts/live-stack/env.sh
start_user_daemon server
start_user_daemon reconciler
start_worker

sleep 5

echo
echo "=== running kdive daemons ==="
# $1 is the numeric PID column, so this both matches real daemons and drops the awk self-line
# and any header. (Do NOT test $2 — that is the username, never numeric.)
ps -eo pid,user,lstart,args | awk -v re="$_daemon_match" '$0 ~ re && $1 ~ /^[0-9]+$/'
echo
report_build_stamps
echo
server_health || true
