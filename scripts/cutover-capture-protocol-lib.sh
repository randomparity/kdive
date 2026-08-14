#!/usr/bin/env bash
# Shared fail-closed bounds and backup publication for protocol cutover authorities.

CUTOVER_BACKUP_TEMP=""
CUTOVER_BACKUP_VALIDATED=0
CUTOVER_DATABASE_REFERENCE=""
CUTOVER_DATABASE_PASSFILE=""
CUTOVER_DATABASE_ENV=()

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
  local statement_timeout_ms=$((CUTOVER_DB_STATEMENT_TIMEOUT_SECONDS * 1000))
  export PGOPTIONS="-c statement_timeout=${statement_timeout_ms} ${PGOPTIONS:-}"
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
    echo "${label} exceeded ${CUTOVER_OPERATION_TIMEOUT_SECONDS} seconds" >&2
    echo "on timeout's monotonic clock." >&2
    echo "The bound covers this one external operation; its incomplete result is rejected." >&2
    echo "Recovery: correct the stalled dependency and rerun the command named below." >&2
  fi
  return "$rc"
}

cutover_init_database_access() {
  local database_url="$1" authority_dir="$2" python="$3"
  local reference_path="${authority_dir}/database-reference"
  local passfile_path="${authority_dir}/database.pgpass"
  [[ -d "$authority_dir" && ! -L "$authority_dir" ]] || {
    echo "database authority directory must be an existing non-symlink directory" >&2
    return 2
  }
  "$python" - "$reference_path" "$passfile_path" 3<<<"$database_url" <<'PY'
import os
import sys
import urllib.parse

reference_path, passfile_path = sys.argv[1:]
with os.fdopen(3, encoding="utf-8") as source:
    database_url = source.read().rstrip("\n")
parsed = urllib.parse.urlsplit(database_url)
if parsed.scheme not in {"postgres", "postgresql"} or not parsed.path:
    raise SystemExit("database DSN must be a PostgreSQL URI with a database path")
if parsed.fragment:
    raise SystemExit("database DSN fragments are not supported")
host = parsed.netloc.rsplit("@", 1)[-1]
raw_user = parsed.netloc.rsplit("@", 1)[0].split(":", 1)[0]
netloc = f"{raw_user}@{host}" if "@" in parsed.netloc else host
query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
query_passwords = [value for key, value in query if key == "password"]
if parsed.password is not None and query_passwords:
    raise SystemExit("database DSN has more than one password authority")
if len(query_passwords) > 1:
    raise SystemExit("database DSN has more than one password query parameter")
if any(key == "sslpassword" for key, _value in query):
    raise SystemExit("database DSN sslpassword is unsupported for cutover")
safe_query = urllib.parse.urlencode(
    [(key, value) for key, value in query if key != "password"]
)
reference = urllib.parse.urlunsplit(
    (parsed.scheme, netloc, parsed.path, safe_query, "")
)
password_value = parsed.password
if query_passwords:
    password_value = query_passwords[0]
password = urllib.parse.unquote(password_value or "")
escaped = password.replace("\\", "\\\\").replace(":", "\\:")
for path, value in (
    (reference_path, reference + "\n"),
    (passfile_path, f"*:*:*:*:{escaped}\n"),
):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(value)
    os.chmod(path, 0o400)
PY
  CUTOVER_DATABASE_REFERENCE="$(<"$reference_path")"
  CUTOVER_DATABASE_PASSFILE="$passfile_path"
  CUTOVER_DATABASE_ENV=(
    env -u KDIVE_DATABASE_URL -u KDIVE_MIGRATION_DATABASE_URL
    -u PGPASSWORD -u PGSERVICE -u PGSERVICEFILE
    PGDATABASE="$CUTOVER_DATABASE_REFERENCE"
    PGPASSFILE="$CUTOVER_DATABASE_PASSFILE"
  )
}

cutover_print_restore_command() {
  local backup_path="$1"
  printf '  env -u KDIVE_DATABASE_URL -u KDIVE_MIGRATION_DATABASE_URL '
  printf '%q ' -u PGPASSWORD -u PGSERVICE -u PGSERVICEFILE
  printf 'PGDATABASE=%q PGPASSFILE=%q ' \
    "$CUTOVER_DATABASE_REFERENCE" "$CUTOVER_DATABASE_PASSFILE"
  printf 'pg_restore --clean --if-exists --dbname=%q %q\n' \
    "$CUTOVER_DATABASE_REFERENCE" "$backup_path"
}

cutover_prepare_backup() {
  local backup_path="$1"
  CUTOVER_BACKUP_TEMP="$(mktemp "${backup_path}.partial.XXXXXX")"
  chmod 0600 "$CUTOVER_BACKUP_TEMP"
}

cutover_publish_backup() {
  local backup_path="$1"
  cutover_bounded "database backup" \
    "${CUTOVER_DATABASE_ENV[@]}" pg_dump --format=custom --file="$CUTOVER_BACKUP_TEMP"
  cutover_bounded "database backup validation" \
    "${CUTOVER_DATABASE_ENV[@]}" pg_restore --list "$CUTOVER_BACKUP_TEMP" >/dev/null
  CUTOVER_BACKUP_VALIDATED=1
  if ! ln --no-target-directory -- "$CUTOVER_BACKUP_TEMP" "$backup_path"; then
    echo "backup destination appeared during cutover; refusing to overwrite it" >&2
    echo "validated backup retained at: ${CUTOVER_BACKUP_TEMP}" >&2
    echo "Recovery: choose a new absolute BACKUP_PATH and publish this validated file." >&2
    return 1
  fi
  gio trash "$CUTOVER_BACKUP_TEMP" >/dev/null 2>&1 ||
    echo "published backup sibling retained at: ${CUTOVER_BACKUP_TEMP}" >&2
  CUTOVER_BACKUP_TEMP=""
}

cutover_cleanup_temporary_backup() {
  [[ -n "$CUTOVER_BACKUP_TEMP" && -e "$CUTOVER_BACKUP_TEMP" ]] || return 0
  if [[ "$CUTOVER_BACKUP_VALIDATED" -eq 1 ]]; then
    echo "validated backup retained at: ${CUTOVER_BACKUP_TEMP}" >&2
    return 0
  fi
  gio trash "$CUTOVER_BACKUP_TEMP" >/dev/null 2>&1 || {
    echo "temporary backup remains for inspection: ${CUTOVER_BACKUP_TEMP}" >&2
    return 1
  }
  CUTOVER_BACKUP_TEMP=""
}
