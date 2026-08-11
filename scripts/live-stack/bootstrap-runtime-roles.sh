#!/usr/bin/env bash
# Bootstrap development-only database members after the authoritative host migration pass.
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck disable=SC1091 # repo-relative path computed from this script location
source "${repo_root}/scripts/live-stack/env.sh"

if [[ "${KDIVE_LOCAL_ROLE_BOOTSTRAP:-1}" == "0" ]]; then
  exit 0
fi

env -u KDIVE_DATABASE_URL -u KDIVE_MIGRATION_DATABASE_URL docker compose run --rm --no-deps role-bootstrap
