#!/usr/bin/env bash
# Stop the local-libvirt example processes started by up.sh.
#
# The processes run as root (qemu:///system), so they are stopped with `sudo kill`. Each pid
# is verified gone; survivors get SIGKILL, and the pid file is kept if cleanup is uncertain so
# a re-run can finish the job. The backends are left running; remove them with
# `docker compose down -v` when you are done.
set -euo pipefail

example_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=examples/local-libvirt/env.sh disable=SC1091
source "${example_dir}/env.sh"
pid_file="${KDIVE_STACK_PID_FILE}"

if [[ ! -f "${pid_file}" ]]; then
  echo "no pid file (${pid_file}); nothing to stop"
  exit 0
fi

mapfile -t pids < <(grep -E '^[0-9]+$' "${pid_file}" || true)
if ((${#pids[@]} == 0)); then
  echo "pid file (${pid_file}) holds no pids; removing it"
  rm -f "${pid_file}"
  exit 0
fi

# Round 1: ask each live process to terminate. `[[ -d /proc/$pid ]]` tells alive from gone
# regardless of owner (kill -0 on a root pid as a non-root user is ambiguous: EPERM).
for pid in "${pids[@]}"; do
  if [[ -d /proc/${pid} ]]; then
    sudo kill "${pid}"
  fi
done

sleep 2

# Round 2: SIGKILL anything that ignored the term, then collect survivors.
survivors=()
for pid in "${pids[@]}"; do
  if [[ -d /proc/${pid} ]]; then
    sudo kill -9 "${pid}" || true
    sleep 1
    if [[ -d /proc/${pid} ]]; then
      survivors+=("${pid}")
    fi
  fi
done

if ((${#survivors[@]} > 0)); then
  echo "could not stop pid(s): ${survivors[*]}; keeping ${pid_file} — re-run down.sh" >&2
  exit 1
fi

rm -f "${pid_file}"
echo "stopped local-libvirt example processes"
echo "backends still running — 'docker compose down -v' from the repo root removes them"
