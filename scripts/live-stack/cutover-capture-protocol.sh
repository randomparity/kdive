#!/usr/bin/env bash
# Offline, replacing protocol 2 -> protocol 3 cutover for host-run KDIVE processes.
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/live-stack/lib.sh
source "${here}/lib.sh"
# shellcheck disable=SC1091 # repo-relative env script
source "${here}/env.sh"
cd "$repo_root"

usage() {
  echo "usage: $0 BACKUP_PATH" >&2
  echo "A rolling protocol 2 to protocol 3 upgrade is refused." >&2
  exit 2
}

[[ "$#" -eq 1 ]] || usage
backup_path="$1"
backup_parent="$(dirname -- "$backup_path")"
[[ "$backup_path" == /* && "$backup_path" != */ ]] || {
  echo "BACKUP_PATH must be an absolute file path" >&2
  exit 2
}
[[ -d "$backup_parent" && -w "$backup_parent" && ! -e "$backup_path" ]] || {
  echo "backup target must be a new file in an existing writable directory: ${backup_path}" >&2
  exit 2
}
[[ -x "$py" ]] || {
  echo "no venv python at ${py}; run 'just setup' first" >&2
  exit 2
}
for tool in awk nohup pg_dump pg_restore ps setsid sleep; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "${tool} is required before the cutover can stop workers" >&2
    exit 2
  }
done

mapfile -t initial_daemon_pids < <(daemon_pids)
for pid in "${initial_daemon_pids[@]}"; do
  if pids_need_sudo "$pid" && ! command -v sudo >/dev/null 2>&1; then
    echo "sudo is required to stop worker PID ${pid} before the cutover" >&2
    exit 2
  fi
done

# Validate that every recorded legacy incarnation belongs to this authority before stopping.
"$py" -m kdive.processes.lifecycle.worker_incarnation check-local-cutover-authority

phase=precondition
recovery() {
  rc=$?
  ((rc == 0)) && return
  if [[ "$phase" == post-migration ]]; then
    stop_daemons >/dev/null 2>&1 || true
    force_stop_daemons >/dev/null 2>&1 || true
  fi
  echo "capture protocol cutover failed during ${phase}; workers remain stopped." >&2
  if [[ "$phase" == post-migration ]]; then
    echo "Protocol 3 may be installed. Do not restart a protocol-2 worker." >&2
    echo "Rollback database exactly with:" >&2
    echo "  pg_restore --clean --if-exists --dbname=\"${KDIVE_DATABASE_URL}\" \"${backup_path}\"" >&2
    echo "Then deploy the prior binary before starting its workers." >&2
  elif [[ "$phase" == migration ]]; then
    echo "The old schema remains authoritative. Correct the named blocker and rerun:" >&2
    echo "  \"${py}\" -m kdive migrate" >&2
    echo "Then run restart_host_processes from a shell that sourced scripts/live-stack/lib.sh and env.sh." >&2
  else
    echo "The old schema remains authoritative. Correct the named blocker and rerun:" >&2
    echo "  scripts/live-stack/cutover-capture-protocol.sh \"${backup_path}\"" >&2
  fi
  exit "$rc"
}
trap recovery EXIT

stop_daemons
cutover_remaining="$(daemon_pids)"
if [[ -n "$cutover_remaining" ]]; then
  force_stop_daemons
fi
cutover_remaining="$(daemon_pids)"
[[ -z "$cutover_remaining" ]] || {
  echo "host-process cutover blocked by still-running daemon PIDs: ${cutover_remaining//$'\n'/,}" >&2
  exit 1
}
"$py" -m kdive.processes.lifecycle.worker_incarnation terminate-local-cutover

pg_dump --format=custom --file="$backup_path" "$KDIVE_DATABASE_URL"
phase=migration
"$py" -m kdive migrate
phase=post-migration
restart_host_processes
phase=complete
trap - EXIT
echo "protocol 3 cutover complete; backup retained at ${backup_path}"
