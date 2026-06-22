#!/usr/bin/env bash
# Stop the local-libvirt example processes started by up.sh.
#
# The processes run as root (qemu:///system), so they are stopped with `sudo kill`. Each pid
# is verified to STILL be a kdive process before it is signalled — a pid from a stale pid file
# may have been recycled by the OS onto an unrelated (possibly root) process, and killing it
# by bare number would take down the wrong thing. Survivors get SIGKILL; the pid file is kept
# if a real kdive process won't die so a re-run can finish the job. The backends are left
# running; remove them with `docker compose down -v` when you are done.
set -euo pipefail

example_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=examples/local-libvirt/env.sh disable=SC1091
source "${example_dir}/env.sh"
pid_file="${KDIVE_STACK_PID_FILE}"

# True only when pid is alive AND its command line is still a `python -m kdive` process.
# Reads /proc/<pid>/cmdline (NUL-separated) without sudo; an unreadable or non-matching
# cmdline (gone, recycled, or hidden by hidepid) reads as "not ours" and is left alone.
is_kdive_proc() {
  local pid="$1" cmdline
  cmdline=$(tr '\0' ' ' <"/proc/${pid}/cmdline" 2>/dev/null) || return 1
  [[ "${cmdline}" == *"-m kdive "* ]]
}

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

# Round 1: ask each pid that is still a kdive process to terminate. A pid that is alive but
# NOT a kdive process is recorded as recycled and never signalled.
recycled=()
for pid in "${pids[@]}"; do
  if is_kdive_proc "${pid}"; then
    sudo kill "${pid}"
  elif [[ -d /proc/${pid} ]]; then
    recycled+=("${pid}")
  fi
done

sleep 2

# Round 2: SIGKILL any kdive process that ignored the term, then collect real survivors.
survivors=()
for pid in "${pids[@]}"; do
  if is_kdive_proc "${pid}"; then
    sudo kill -9 "${pid}" || true
    sleep 1
    if is_kdive_proc "${pid}"; then
      survivors+=("${pid}")
    fi
  fi
done

if ((${#recycled[@]} > 0)); then
  echo "pid(s) ${recycled[*]} are alive but no longer kdive processes (recycled pid); left" \
    "untouched" >&2
fi

if ((${#survivors[@]} > 0)); then
  echo "could not stop kdive pid(s): ${survivors[*]}; keeping ${pid_file} — re-run down.sh" >&2
  exit 1
fi

rm -f "${pid_file}"
echo "stopped local-libvirt example processes"
echo "backends still running — 'docker compose down -v' from the repo root removes them"
