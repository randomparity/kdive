#!/usr/bin/env bash
# Offline, replacing protocol 2 -> protocol 3 cutover for the reference Compose deployment.
set -euo pipefail
umask 077

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"
# shellcheck source=scripts/cutover-capture-protocol-lib.sh
source "${repo_root}/scripts/cutover-capture-protocol-lib.sh"

usage() {
  echo "usage: $0 BACKUP_PATH TARGET_IMAGE" >&2
  echo "A rolling protocol 2 to protocol 3 upgrade is refused." >&2
  exit 2
}

[[ "$#" -eq 2 ]] || usage
backup_path="$1"
target_image="$2"
backup_parent="$(dirname -- "$backup_path")"
[[ "$backup_path" == /* && "$backup_path" != */ ]] || {
  echo "BACKUP_PATH must be an absolute file path" >&2
  exit 2
}
[[ -n "$target_image" && "$target_image" != *[[:space:]]* ]] || usage
[[ -d "$backup_parent" && -w "$backup_parent" && ! -e "$backup_path" ]] || {
  echo "backup target must be a new file in an existing writable directory" >&2
  exit 2
}
for tool in cmp docker gio just ln mktemp pg_dump pg_restore psql python3 timeout; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "${tool} is required before the cutover can stop workers" >&2
    exit 2
  }
done
cutover_init_contract

if [[ -n "${KDIVE_DATABASE_URL:-}" ]]; then
  database_url="$KDIVE_DATABASE_URL"
else
  # pragma: allowlist nextline secret
  database_url="postgresql://kdive-migration:kdive-migration-local@localhost"
  database_url="${database_url}:${KDIVE_POSTGRES_PORT:-5432}/kdive" # pragma: allowlist secret
fi
export KDIVE_DATABASE_URL="$database_url"
export COMPOSE_PROGRESS=quiet

snapshot_dir="$(mktemp -d "${backup_parent}/.kdive-compose-cutover.XXXXXX")"
chmod 0700 "$snapshot_dir"
raw_model="${snapshot_dir}/approved-raw.json"
frozen_model="${snapshot_dir}/approved-compose.json"

cleanup_snapshot() {
  [[ ! -e "$snapshot_dir" ]] || gio trash "$snapshot_dir" >/dev/null 2>&1 || {
    echo "restricted cutover snapshot retained at: ${snapshot_dir}" >&2
    return 0
  }
}
cleanup_preflight() {
  local rc=$?
  trap - EXIT
  set +e
  cleanup_snapshot
  exit "$rc"
}
trap cleanup_preflight EXIT

cutover_bounded "Docker Compose version preflight" docker compose version >/dev/null
if ! cutover_bounded "target image local resolution" \
  docker image inspect "$target_image" >/dev/null 2>&1; then
  cutover_bounded "target image pull" docker pull "$target_image" >/dev/null
fi
target_image_id="$(cutover_bounded "target image identity snapshot" \
  docker image inspect --format '{{.Id}}' "$target_image")"
[[ "$target_image_id" == sha256:* ]] || {
  echo "target image did not resolve to an immutable image ID" >&2
  exit 1
}
cutover_bounded "target Compose render" env KDIVE_IMAGE="$target_image" \
  docker compose --profile cutover --profile managed-worker --profile render-only \
  config --format json >"$raw_model"
IFS=$'\t' read -r compose_project < <(
  python3 - "$raw_model" "$frozen_model" "$target_image" "$target_image_id" <<'PY'
import json
import os
import sys

source, destination, expected, image_id = sys.argv[1:]
document = json.load(open(source, encoding="utf-8"))
services = document.get("services", {})
for name in ("migrate", "server", "worker", "reconciler"):
    if services.get(name, {}).get("image") != expected:
        raise SystemExit(f"target Compose render does not pin {name} to TARGET_IMAGE")
    services[name]["image"] = image_id
# The supervisor creates one nonce after approval and before each never-started worker. Preserve
# only that lifecycle-owned interpolation seam; every operator-controlled value stays resolved.
services["worker"].setdefault("environment", {})["KDIVE_WORKER_INCARNATION_ID"] = (
    "docker:${KDIVE_WORKER_INCARNATION_NONCE:-unmanaged}"
)
project = document.get("name")
if not isinstance(project, str) or not project:
    raise SystemExit("target Compose render has no project identity")
fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as stream:
    json.dump(document, stream, sort_keys=True)
os.chmod(destination, 0o400)
print(project)
PY
)
[[ -n "$compose_project" ]] || exit 1
export COMPOSE_FILE="$frozen_model"
export COMPOSE_PROJECT_NAME="$compose_project"
compose=(docker compose --file "$frozen_model" --project-name "$compose_project")

database_identity_sql="SELECT current_database(), oid, (pg_control_system()).system_identifier
FROM pg_database WHERE datname = current_database()"
container_identity_python='import os, psycopg
with psycopg.connect(os.environ["KDIVE_DATABASE_URL"]) as conn:
 query="SELECT current_database(), oid, (pg_control_system()).system_identifier "
 query+="FROM pg_database WHERE datname=current_database()"
 row=conn.execute(query).fetchone()
 print("\t".join(map(str,row)))'
host_database_identity() {
  cutover_bounded "host database identity witness" \
    psql "$database_url" --set ON_ERROR_STOP=1 --tuples-only --no-align \
    --field-separator=$'\t' --command "$database_identity_sql"
}
container_database_identity() {
  cutover_bounded "migration container database identity witness" \
    "${compose[@]}" --profile cutover run --rm --no-deps --entrypoint python migrate \
    -c "$container_identity_python"
}
prove_same_database() {
  local stage="$1"
  host_database_identity >"${snapshot_dir}/${stage}-host-db"
  container_database_identity >"${snapshot_dir}/${stage}-container-db"
  cmp --silent "${snapshot_dir}/${stage}-host-db" \
    "${snapshot_dir}/${stage}-container-db" || {
    echo "host and migration container database identities differ" >&2
    return 1
  }
}
print_shell_command() {
  printf '  '
  printf '%q ' "$@"
  printf '\n'
}

prove_same_database preflight
cutover_bounded "database lifecycle-authority preflight" \
  psql "$database_url" --set ON_ERROR_STOP=1 <<'SQL'
DO $check$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_class
    WHERE oid = 'public.worker_incarnations'::regclass
      AND relowner = (SELECT usesysid FROM pg_user WHERE usename = current_user)
  ) THEN
    RAISE EXCEPTION 'migration principal does not own the lifecycle table';
  END IF;
  IF EXISTS (
    SELECT 1 FROM public.worker_incarnations
    WHERE fence_protocol < 3
      AND (authority_kind IS DISTINCT FROM 'docker'
        OR jsonb_typeof(authority_binding -> 'container_id') IS DISTINCT FROM 'string'
        OR (authority_binding ->> 'container_id') !~ '^[0-9a-f]{64}$')
  ) THEN
    RAISE EXCEPTION 'Compose cutover has malformed container lifecycle authority data';
  END IF;
END
$check$;
SQL

phase=stop
recovery() {
  local stop_rc=0 proof_rc=0 survivors=""
  set +e
  cutover_bounded "Compose recovery stop" just compose-stop >/dev/null 2>&1
  stop_rc=$?
  survivors="$(cutover_bounded "Compose stopped-state proof" \
    "${compose[@]}" --profile managed-worker ps --all --status running -q worker)"
  proof_rc=$?
  echo "Compose capture protocol cutover failed during ${phase}." >&2
  if [[ "$proof_rc" -eq 0 && -z "$survivors" ]]; then
    echo "Compose stopped-state proof found no running managed worker." >&2
  else
    echo "workers may still be running; stop=${stop_rc}, proof=${proof_rc}." >&2
    [[ -z "$survivors" ]] || echo "surviving workers: ${survivors//$'\n'/,}" >&2
  fi
  echo "restricted frozen Compose snapshot retained at: ${snapshot_dir}" >&2
  if [[ "$phase" == post-migration ]]; then
    echo "Protocol 3 may be installed. Rollback exactly with:" >&2
    printf "  pg_restore --clean --if-exists --dbname=\"\$KDIVE_DATABASE_URL\" %q\n" \
      "$backup_path" >&2
  elif [[ "$phase" == migration ]]; then
    echo "The named backup is complete; correct the blocker and resume with:" >&2
    print_shell_command \
      "COMPOSE_FILE=${frozen_model}" "COMPOSE_PROJECT_NAME=${compose_project}" \
      timeout --kill-after=5 \
      "${KDIVE_CUTOVER_OPERATION_TIMEOUT_SECONDS:-600}s" \
      docker compose --profile cutover run --rm migrate >&2
  else
    echo "The old schema remains authoritative; rerun the same command:" >&2
    printf '  scripts/cutover-capture-protocol-compose.sh %q %q\n' \
      "$backup_path" "$target_image" >&2
  fi
  echo "Never start protocol 2 after migration 0112." >&2
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

cutover_bounded "Compose lifecycle stop" just compose-stop
cutover_bounded "Compose frozen Postgres restart" \
  "${compose[@]}" up -d --wait --wait-timeout 120 postgres
prove_same_database post-stop
cutover_bounded "database stopped-population proof" \
  psql "$database_url" --set ON_ERROR_STOP=1 <<'SQL'
DO $check$
DECLARE blockers text;
BEGIN
  SELECT string_agg(incarnation, ', ' ORDER BY incarnation) INTO blockers
  FROM public.worker_incarnations
  WHERE fence_protocol < 3
    AND (authority_kind <> 'docker' OR state <> 'terminated'
      OR terminated_at IS NULL OR outcome IS NULL);
  IF blockers IS NOT NULL THEN
    RAISE EXCEPTION 'Compose cutover still has active worker incarnations: %', blockers;
  END IF;
END
$check$;
SQL
phase=backup
cutover_prepare_backup "$backup_path"
cutover_publish_backup "$backup_path" "$database_url"
phase=migration
cutover_bounded "Compose one-shot migration" \
  "${compose[@]}" --profile cutover run --rm migrate
phase=post-migration
cutover_bounded "Compose protocol-3 start" just compose-up
phase=complete
trap - EXIT
cleanup_snapshot
echo "protocol 3 cutover complete; backup retained at ${backup_path}"
