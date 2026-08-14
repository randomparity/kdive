#!/usr/bin/env bash
# Offline, replacing protocol 2 -> protocol 3 cutover for host-run KDIVE processes.
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/live-stack/lib.sh
source "${here}/lib.sh"
# shellcheck disable=SC1091 # repo-relative env script
source "${here}/env.sh"
# shellcheck source=scripts/cutover-capture-protocol-lib.sh
source "${repo_root}/scripts/cutover-capture-protocol-lib.sh"
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
for tool in awk gio ln mktemp nohup pg_dump pg_restore ps setsid sleep timeout; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "${tool} is required before the cutover can stop workers" >&2
    exit 2
  }
done
cutover_init_contract

mapfile -t initial_daemon_pids < <(daemon_pids)
for pid in "${initial_daemon_pids[@]}"; do
  if pids_need_sudo "$pid" && ! command -v sudo >/dev/null 2>&1; then
    echo "sudo is required to stop worker PID ${pid} before the cutover" >&2
    exit 2
  fi
done

# Validate that every recorded legacy incarnation belongs to this authority before stopping.
cutover_bounded "database local-authority preflight" \
  "$py" -m kdive.processes.lifecycle.worker_incarnation check-local-cutover-authority

phase=precondition
recovery() {
  local stop_rc=0 force_rc=0 survivors=""
  set +e
  stop_daemons >/dev/null 2>&1 || stop_rc=$?
  force_stop_daemons >/dev/null 2>&1 || force_rc=$?
  survivors="$(daemon_pids)"
  echo "capture protocol cutover failed during ${phase}." >&2
  if [[ -z "$survivors" ]]; then
    echo "host stopped-state proof found no KDIVE daemon process." >&2
  else
    echo "workers may still be running; graceful stop status=${stop_rc}," >&2
    echo "forced stop status=${force_rc}." >&2
    echo "surviving host daemon PIDs: ${survivors//$'\n'/,}" >&2
  fi
  if [[ "$phase" == post-migration ]]; then
    echo "Protocol 3 may be installed. Do not restart a protocol-2 worker." >&2
    echo "Rollback database exactly with:" >&2
    printf "  pg_restore --clean --if-exists --dbname=\"\$KDIVE_DATABASE_URL\" %q\n" \
      "$backup_path" >&2
    echo "Then deploy the prior binary before starting its workers." >&2
  elif [[ "$phase" == migration ]]; then
    echo "The named backup is complete; correct the blocker and resume with:" >&2
    printf "  timeout --kill-after=5 \"\${KDIVE_CUTOVER_OPERATION_TIMEOUT_SECONDS:-600}s\"" >&2
    printf ' %q -m kdive migrate\n' "$py" >&2
    echo "Then source the live-stack libraries and run restart_host_processes." >&2
  else
    echo "The old schema remains authoritative; partial backup state was rejected." >&2
    echo "Correct the named blocker and rerun the same command:" >&2
    printf '  scripts/live-stack/cutover-capture-protocol.sh %q\n' "$backup_path" >&2
  fi
}

on_exit() {
  local rc=$? cleanup_rc=0
  trap - EXIT
  if ((rc != 0)); then
    recovery
  fi
  cutover_cleanup_temporary_backup || cleanup_rc=$?
  ((rc != 0)) && exit "$rc"
  exit "$cleanup_rc"
}
trap on_exit EXIT

stop_daemons
cutover_remaining="$(daemon_pids)"
if [[ -n "$cutover_remaining" ]]; then
  force_stop_daemons
fi
cutover_remaining="$(daemon_pids)"
[[ -z "$cutover_remaining" ]] || {
  echo "host-process cutover blocked by still-running daemon PIDs:" >&2
  echo "${cutover_remaining//$'\n'/,}" >&2
  exit 1
}
cutover_bounded "database local-termination persistence" \
  "$py" -m kdive.processes.lifecycle.worker_incarnation terminate-local-cutover

phase=backup
cutover_prepare_backup "$backup_path"
cutover_publish_backup "$backup_path" "$KDIVE_DATABASE_URL"
phase=migration
cutover_bounded "host database migration" "$py" -m kdive migrate
phase=post-migration
restart_host_processes
phase=complete
trap - EXIT
echo "protocol 3 cutover complete; backup retained at ${backup_path}"
