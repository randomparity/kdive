#!/usr/bin/env bash
# Shared fail-closed bounds and backup publication for protocol cutover authorities.

CUTOVER_BACKUP_TEMP=""
# shellcheck disable=SC2034 # sourced cutover authorities inspect this state after failures
CUTOVER_BACKUP_COMPLETE=0

cutover_positive_seconds() {
  local name="$1" value="$2"
  [[ "$value" =~ ^[1-9][0-9]*$ && "${#value}" -le 5 ]] || {
    echo "${name} must be a positive whole number of seconds" >&2
    return 2
  }
}

cutover_init_contract() {
  CUTOVER_OPERATION_TIMEOUT_SECONDS="${KDIVE_CUTOVER_OPERATION_TIMEOUT_SECONDS:-600}"
  CUTOVER_DB_CONNECT_TIMEOUT_SECONDS="${KDIVE_CUTOVER_DB_CONNECT_TIMEOUT_SECONDS:-10}"
  CUTOVER_DB_STATEMENT_TIMEOUT_SECONDS="${KDIVE_CUTOVER_DB_STATEMENT_TIMEOUT_SECONDS:-300}"
  cutover_positive_seconds KDIVE_CUTOVER_OPERATION_TIMEOUT_SECONDS \
    "$CUTOVER_OPERATION_TIMEOUT_SECONDS"
  cutover_positive_seconds KDIVE_CUTOVER_DB_CONNECT_TIMEOUT_SECONDS \
    "$CUTOVER_DB_CONNECT_TIMEOUT_SECONDS"
  cutover_positive_seconds KDIVE_CUTOVER_DB_STATEMENT_TIMEOUT_SECONDS \
    "$CUTOVER_DB_STATEMENT_TIMEOUT_SECONDS"
  export PGCONNECT_TIMEOUT="$CUTOVER_DB_CONNECT_TIMEOUT_SECONDS"
  export PGOPTIONS="-c statement_timeout=$((CUTOVER_DB_STATEMENT_TIMEOUT_SECONDS * 1000)) ${PGOPTIONS:-}"
}

cutover_bounded() {
  local label="$1" rc
  shift
  if timeout --kill-after=5 "${CUTOVER_OPERATION_TIMEOUT_SECONDS}s" "$@"; then
    return 0
  else
    rc=$?
  fi
  if [[ "$rc" -eq 124 || "$rc" -eq 137 ]]; then
    echo "${label} exceeded ${CUTOVER_OPERATION_TIMEOUT_SECONDS} seconds on timeout's monotonic clock." >&2
    echo "The bound covers this one external operation; its incomplete result is rejected." >&2
    echo "Recovery: correct the stalled dependency and rerun the command named below." >&2
  fi
  return "$rc"
}

cutover_prepare_backup() {
  local backup_path="$1"
  CUTOVER_BACKUP_TEMP="$(mktemp "${backup_path}.partial.XXXXXX")"
  chmod 0600 "$CUTOVER_BACKUP_TEMP"
}

cutover_publish_backup() {
  local backup_path="$1" database_url="$2"
  cutover_bounded "database backup" \
    pg_dump --format=custom --file="$CUTOVER_BACKUP_TEMP" "$database_url"
  cutover_bounded "database backup validation" \
    pg_restore --list "$CUTOVER_BACKUP_TEMP" >/dev/null
  mv -- "$CUTOVER_BACKUP_TEMP" "$backup_path"
  CUTOVER_BACKUP_TEMP=""
  # shellcheck disable=SC2034 # sourced cutover authorities inspect this state after failures
  CUTOVER_BACKUP_COMPLETE=1
}

cutover_cleanup_temporary_backup() {
  [[ -n "$CUTOVER_BACKUP_TEMP" && -e "$CUTOVER_BACKUP_TEMP" ]] || return 0
  gio trash "$CUTOVER_BACKUP_TEMP" >/dev/null 2>&1 || {
    echo "temporary backup remains for inspection: ${CUTOVER_BACKUP_TEMP}" >&2
    return 1
  }
  CUTOVER_BACKUP_TEMP=""
}
