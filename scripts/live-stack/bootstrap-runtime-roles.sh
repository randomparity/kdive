#!/usr/bin/env bash
# Bootstrap development-only database members after the authoritative host migration pass.
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
if [[ -v KDIVE_MIGRATION_DATABASE_URL ]]; then
  migration_database_url_was_explicit=1
  explicit_migration_database_url="$KDIVE_MIGRATION_DATABASE_URL"
else
  migration_database_url_was_explicit=0
  explicit_migration_database_url=""
fi
# shellcheck disable=SC1091 # repo-relative path computed from this script location
source "${repo_root}/scripts/live-stack/env.sh"

if [[ "${KDIVE_LOCAL_ROLE_BOOTSTRAP:-1}" == "0" ]]; then
  exit 0
fi

if [[ $migration_database_url_was_explicit == 1 ]]; then
  KDIVE_MIGRATION_DATABASE_URL="$explicit_migration_database_url" \
    env -u KDIVE_DATABASE_URL -u KDIVE_SERVER_DATABASE_URL \
    -u KDIVE_WORKER_DATABASE_URL -u KDIVE_RECONCILER_DATABASE_URL \
    docker compose run --rm --no-deps role-bootstrap
else
  env -u KDIVE_DATABASE_URL -u KDIVE_MIGRATION_DATABASE_URL \
    -u KDIVE_SERVER_DATABASE_URL -u KDIVE_WORKER_DATABASE_URL \
    -u KDIVE_RECONCILER_DATABASE_URL \
    docker compose run --rm --no-deps role-bootstrap
fi
