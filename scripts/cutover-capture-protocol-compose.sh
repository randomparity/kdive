#!/usr/bin/env bash
# Offline, replacing protocol 2 -> protocol 3 cutover for the reference Compose deployment.
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

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
  echo "backup target must be a new file in an existing writable directory: ${backup_path}" >&2
  exit 2
}
for tool in docker just pg_dump pg_restore psql; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "${tool} is required before the cutover can stop workers" >&2
    exit 2
  }
done
docker compose version >/dev/null

database_url="${KDIVE_DATABASE_URL:-postgresql://kdive-migration:kdive-migration-local@localhost:${KDIVE_POSTGRES_PORT:-5432}/kdive}" # pragma: allowlist secret

# Missing or malformed lifecycle authority data is known before the stop and must fail first.
psql "$database_url" --set ON_ERROR_STOP=1 --tuples-only --no-align <<'SQL'
DO $check$
BEGIN
  IF to_regclass('public.worker_incarnations') IS NOT NULL AND EXISTS (
    SELECT 1 FROM public.worker_incarnations
    WHERE fence_protocol < 3
      AND (authority_kind IS DISTINCT FROM 'docker'
           OR jsonb_typeof(authority_binding -> 'container_id') IS DISTINCT FROM 'string'
           OR (authority_binding ->> 'container_id') !~ '^[0-9a-f]{64}$')
  ) THEN
    RAISE EXCEPTION 'Compose protocol cutover has malformed container lifecycle authority data';
  END IF;
END
$check$;
SQL

phase=precondition
recovery() {
  rc=$?
  ((rc == 0)) && return
  if [[ "$phase" == post-migration ]]; then
    just compose-stop >/dev/null 2>&1 || true
  fi
  echo "Compose capture protocol cutover failed during ${phase}; workers remain stopped." >&2
  if [[ "$phase" == post-migration ]]; then
    echo "Protocol 3 may be installed. Do not restart a protocol-2 worker." >&2
    echo "Rollback database exactly with:" >&2
    echo "  pg_restore --clean --if-exists --dbname=\"${database_url}\" \"${backup_path}\"" >&2
    echo "Then select the prior image before starting its workers." >&2
  elif [[ "$phase" == migration ]]; then
    echo "The old schema remains authoritative. Correct the named blocker and rerun:" >&2
    echo "  KDIVE_IMAGE=\"${target_image}\" docker compose run --rm migrate" >&2
    echo "Then start protocol 3 with: KDIVE_IMAGE=\"${target_image}\" just compose-up" >&2
  else
    echo "The old schema remains authoritative. Correct the named blocker and rerun:" >&2
    echo "  scripts/cutover-capture-protocol-compose.sh \"${backup_path}\" \"${target_image}\"" >&2
  fi
  exit "$rc"
}
trap recovery EXIT

just compose-stop
# compose-stop preserves named volumes but stops Postgres; restart only that backend for proof,
# backup, and migration. The managed-worker profile remains absent.
docker compose up -d --wait --wait-timeout 120 postgres
psql "$database_url" --set ON_ERROR_STOP=1 <<'SQL'
DO $check$
DECLARE
  blockers text;
BEGIN
  SELECT string_agg(incarnation, ', ' ORDER BY incarnation) INTO blockers
  FROM public.worker_incarnations
  WHERE fence_protocol < 3
    AND (authority_kind <> 'docker' OR state <> 'terminated'
         OR terminated_at IS NULL OR outcome IS NULL);
  IF blockers IS NOT NULL THEN
    RAISE EXCEPTION 'Compose protocol cutover still has active worker incarnations: %', blockers;
  END IF;
END
$check$;
SQL
pg_dump --format=custom --file="$backup_path" "$database_url"
phase=migration
KDIVE_IMAGE="$target_image" docker compose run --rm migrate
phase=post-migration
KDIVE_IMAGE="$target_image" just compose-up
phase=complete
trap - EXIT
echo "protocol 3 cutover complete; backup retained at ${backup_path}"
