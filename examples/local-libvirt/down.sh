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

# Can this (non-root) user read root pids' /proc entries directly? On a default /proc, yes;
# under `hidepid=2` a non-root user cannot see a root pid at all. Probe once against init
# (pid 1, root-owned): if its cmdline is unreadable, fall back to a privileged read so the
# kdive identity check still works under hidepid. Probed once so the common path takes no
# extra sudo.
if tr '\0' ' ' </proc/1/cmdline >/dev/null 2>&1; then
  need_sudo_proc=0
else
  need_sudo_proc=1
fi

# Echo a pid's command line (NUL→space), or nothing when the pid is gone. `cat … 2>/dev/null`
# (not a `<` redirect) so a gone pid's open error is swallowed by cat's own stderr rather than
# leaking a shell redirection error. The trailing `|| true` forces a 0 exit: with `pipefail`
# set, cat's failure on a gone pid would otherwise propagate and abort `cmd=$(read_cmdline …)`
# under `set -e`. Empty output == gone, so callers test the string.
read_cmdline() {
  local pid="$1"
  if ((need_sudo_proc)); then
    sudo cat "/proc/${pid}/cmdline" 2>/dev/null | tr '\0' ' ' || true
  else
    cat "/proc/${pid}/cmdline" 2>/dev/null | tr '\0' ' ' || true
  fi
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
# NOT a kdive process is recorded as recycled and never signalled. `|| true`: a process that
# exits in the gap between the identity check and the signal makes `kill` fail harmlessly —
# round 2 and the survivor check below decide the real outcome, so one stray failure must not
# abort the loop under `set -e`.
recycled=()
for pid in "${pids[@]}"; do
  cmd=$(read_cmdline "${pid}")
  if [[ "${cmd}" == *"-m kdive "* ]]; then
    sudo kill "${pid}" || true
  elif [[ -n "${cmd}" ]]; then
    recycled+=("${pid}")
  fi
done

sleep 2

# Round 2: SIGKILL any kdive process that ignored the term, then collect real survivors.
survivors=()
for pid in "${pids[@]}"; do
  cmd=$(read_cmdline "${pid}")
  if [[ "${cmd}" == *"-m kdive "* ]]; then
    sudo kill -9 "${pid}" || true
    sleep 1
    cmd=$(read_cmdline "${pid}")
    if [[ "${cmd}" == *"-m kdive "* ]]; then
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
