#!/usr/bin/env bash
# Offline, replacing protocol 2 -> protocol 3 cutover for the reference Compose deployment.
set -euo pipefail

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
  echo "backup target must be a new file in an existing writable directory: ${backup_path}" >&2
  exit 2
}
for tool in docker gio just mktemp mv pg_dump pg_restore psql python3 timeout unlink; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "${tool} is required before the cutover can stop workers" >&2
    exit 2
  }
done
cutover_init_contract

database_url="${KDIVE_DATABASE_URL:-postgresql://kdive-migration:kdive-migration-local@localhost:${KDIVE_POSTGRES_PORT:-5432}/kdive}" # pragma: allowlist secret
export KDIVE_DATABASE_URL="$database_url"
# The lifecycle authority treats any Compose-down stderr as failure so network-removal errors
# cannot hide behind exit 0. Suppress only Compose's normal progress stream.
export COMPOSE_PROGRESS=quiet

cutover_bounded "Docker Compose version preflight" docker compose version >/dev/null
if ! cutover_bounded "target image local resolution" \
  docker image inspect "$target_image" >/dev/null 2>&1; then
  cutover_bounded "target image pull" docker pull "$target_image" >/dev/null
fi
compose_render="$(mktemp)"
cleanup_compose_render() {
  [[ -z "$compose_render" || ! -e "$compose_render" ]] ||
    gio trash "$compose_render" >/dev/null 2>&1 || unlink "$compose_render"
}
cleanup_compose_preflight() {
  local rc=$?
  trap - EXIT
  set +e
  cleanup_compose_render
  exit "$rc"
}
trap cleanup_compose_preflight EXIT
cutover_bounded "target Compose render" env KDIVE_IMAGE="$target_image" \
  docker compose --profile cutover --profile managed-worker --profile render-only \
  config --format json >"$compose_render"
python3 - "$compose_render" "$target_image" <<'PY'
import json
import sys

document = json.load(open(sys.argv[1], encoding="utf-8"))
expected = sys.argv[2]
services = document.get("services", {})
for name in ("migrate", "server", "worker", "reconciler"):
    if services.get(name, {}).get("image") != expected:
        raise SystemExit(f"target Compose render does not pin {name} to TARGET_IMAGE")
PY

# Missing or malformed lifecycle authority data is known before the stop and must fail first.
cutover_bounded "database lifecycle-authority preflight" \
  psql "$database_url" --set ON_ERROR_STOP=1 --tuples-only --no-align <<'SQL'
DO $check$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_class
    WHERE oid = 'public.worker_incarnations'::regclass
      AND relowner = (SELECT usesysid FROM pg_user WHERE usename = current_user)
  ) THEN
    RAISE EXCEPTION 'migration principal does not own the lifecycle table';
  END IF;
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
gio trash "$compose_render" >/dev/null 2>&1 || unlink "$compose_render"
trap - EXIT
compose_render=""

phase=precondition
recovery() {
  local stop_rc=0 proof_rc=0 survivors=""
  set +e
  cutover_bounded "Compose recovery stop" just compose-stop >/dev/null 2>&1 || stop_rc=$?
  survivors="$(cutover_bounded "Compose stopped-state proof" \
    docker compose --profile managed-worker ps --all --status running -q worker)" || proof_rc=$?
  echo "Compose capture protocol cutover failed during ${phase}." >&2
  if [[ "$proof_rc" -eq 0 && -z "$survivors" ]]; then
    echo "Compose stopped-state proof found no running managed worker." >&2
  else
    echo "workers may still be running; recovery stop status=${stop_rc}, proof status=${proof_rc}." >&2
    [[ -z "$survivors" ]] || echo "surviving worker container identities: ${survivors//$'\n'/,}" >&2
  fi
  if [[ "$phase" == post-migration ]]; then
    echo "Protocol 3 may be installed. Do not restart a protocol-2 worker." >&2
    echo "Rollback database exactly with:" >&2
    printf '%s\n' "  pg_restore --clean --if-exists --dbname=\"\$KDIVE_DATABASE_URL\" \"${backup_path}\"" >&2
    echo "Then select the prior image before starting its workers." >&2
  elif [[ "$phase" == migration ]]; then
    echo "The named backup is complete; correct the blocker and resume with:" >&2
    printf '%s\n' "  KDIVE_IMAGE=\"${target_image}\" timeout --kill-after=5 \"\${KDIVE_CUTOVER_OPERATION_TIMEOUT_SECONDS:-600}s\" docker compose run --rm migrate" >&2
    echo "Then run this bounded start: KDIVE_IMAGE=\"${target_image}\" just compose-up" >&2
  else
    echo "The old schema remains authoritative; partial backup state was rejected." >&2
    echo "Correct the named blocker and rerun the same command:" >&2
    echo "  scripts/cutover-capture-protocol-compose.sh \"${backup_path}\" \"${target_image}\"" >&2
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

cutover_bounded "Compose lifecycle stop" just compose-stop
# compose-stop preserves named volumes but stops Postgres; restart only that backend for proof,
# backup, and migration. The managed-worker profile remains absent.
cutover_bounded "Compose Postgres restart" \
  docker compose up -d --wait --wait-timeout 120 postgres
cutover_bounded "database stopped-population proof" \
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
phase=backup
cutover_prepare_backup "$backup_path"
cutover_publish_backup "$backup_path" "$database_url"
phase=migration
cutover_bounded "Compose one-shot migration" env KDIVE_IMAGE="$target_image" \
  docker compose --profile cutover run --rm migrate
phase=post-migration
cutover_bounded "Compose protocol-3 start" env KDIVE_IMAGE="$target_image" just compose-up
phase=complete
trap - EXIT
echo "protocol 3 cutover complete; backup retained at ${backup_path}"
