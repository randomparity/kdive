#!/usr/bin/env bash
# Stop the local-libvirt example processes started by up.sh.
#
# The processes run as root (qemu:///system), so they are stopped with `sudo kill`. The
# backends are left running; remove them with `docker compose down -v` when you are done.
set -euo pipefail

example_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=examples/local-libvirt/env.sh disable=SC1091
source "${example_dir}/env.sh"
pid_file="${KDIVE_STACK_PID_FILE}"

if [[ ! -f "${pid_file}" ]]; then
  echo "no pid file (${pid_file}); nothing to stop"
  exit 0
fi

while read -r pid; do
  [[ -n "${pid}" ]] || continue
  sudo kill "${pid}" 2>/dev/null || true
done <"${pid_file}"
rm -f "${pid_file}"

echo "stopped local-libvirt example processes"
echo "backends still running — 'docker compose down -v' from the repo root removes them"
