#!/usr/bin/env bash
# Onboard a dev-stack project so a fresh agent's first allocations.request is granted (#834,
# ADR-0256). One command that funds a project against the SAME database the server reads, makes
# the project string the single source of truth, verifies the seed landed, and mints a token.
#
#   just onboard                 # project "demo" (default)
#   KDIVE_PROJECT=acme just onboard
#
# Order: source env.sh -> preflight -> migrate -> seed -> verify -> mint + contract.
# Hard gates: migrate and verify-project (the funding rows are present). Advisory (warn,
# non-fatal): the seed's resource-discovery side effect and the token mint. The provider preflight
# is advisory BY DEFAULT and fatal when the caller declares ONBOARD_PREFLIGHT=required (ADR-0666).
# seed-project commits the budget/quota upserts BEFORE it registers discovered resources,
# so a discovery failure (provider unreachable) still leaves a funded project that verify
# confirms — verify, not the seed exit code, is the funding source of truth.
#
# Every kdive CLI invocation here aliases its own database authority (#1929): migrate connects
# through KDIVE_MIGRATION_DATABASE_URL, the funding steps through KDIVE_SERVER_DATABASE_URL, and
# each child has the other role DSNs scrubbed from its environment (#2046).
#
# DEMO ONLY: the bundled mock-OIDC issuer mints a valid token for any caller. Never run this
# against a real deployment; production onboards a project with the audited admin tools
# (docs/operating/project-onboarding.md).
#
# Env overrides: KDIVE_PROJECT (demo), KDIVE_ROLE (admin), KDIVE_TOKEN_TTL (2592000 = 30d,
#   default from live-stack/env.sh),
#   KDIVE_LIMIT_KCU (1000000), KDIVE_MAX_ALLOC (4), KDIVE_MAX_SYS (4).
#
# ONBOARD_PREFLIGHT (advisory) — 'advisory' warns and continues on a preflight FAIL; 'required'
#   stops before migrate with the preflight's own FAIL text as the reason. Unprefixed for
#   ADR-0659's reason: it is a declaration a CALLER makes about its own next step, not an operator
#   knob, so it is deliberately absent from kdive.config's registry and the generated reference.
#   Do NOT export it in an operator shell — every later onboard.sh inherits it, including the one
#   examples/local-libvirt/demo-up.sh runs, whose first-run workstation case expects 'advisory'.
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${here}/../.." && pwd)"
# shellcheck source=scripts/live-stack/env.sh disable=SC1091
source "${here}/env.sh"
cd "$repo_root"

# KDIVE_PYTHON overrides the interpreter (the #1293 self-hosted CI job points it at /opt/kdive's
# libguestfs venv); unset, fall back to `uv run python` (the operator dev-loop default).
if [[ -n "${KDIVE_PYTHON:-}" ]]; then
  py=("$KDIVE_PYTHON")
else
  py=(uv run python)
fi

PROJECT="${KDIVE_PROJECT:-demo}"
ROLE="${KDIVE_ROLE:-admin}"
TTL="${KDIVE_TOKEN_TTL}" # exported by env.sh (default 30d); see its comment
LIMIT_KCU="${KDIVE_LIMIT_KCU:-1000000}"
MAX_ALLOC="${KDIVE_MAX_ALLOC:-4}"
MAX_SYS="${KDIVE_MAX_SYS:-4}"

banner() { printf '\n=== %s ===\n' "$1"; }

# The preflight's severity is the CALLER's declaration (ADR-0666), because only the caller knows
# what it does next. This script's own hard gates are migrate and verify-project, which need the
# database and no libvirt at all, so no preflight FAIL blocks *its* work — but a caller that goes
# on to provision needs the FAIL to be fatal, and to read the preflight's own text as the reason
# rather than a generic infrastructure_failure from a later component (#2568).
PREFLIGHT="${ONBOARD_PREFLIGHT:-advisory}"
case "$PREFLIGHT" in
advisory | required) ;;
*)
  printf "ONBOARD_PREFLIGHT=%s is not valid; set 'advisory' (default) or 'required'\n" \
    "$PREFLIGHT" >&2
  exit 2
  ;;
esac

banner "preflight (${PREFLIGHT})"
if ! "${repo_root}/scripts/operations/check-local-libvirt.sh"; then
  if [[ "$PREFLIGHT" == "required" ]]; then
    echo "ERROR: the local-libvirt preflight FAILED and ONBOARD_PREFLIGHT=required, so this run" >&2
    echo "       stops here, before migrate. The FAIL entries above are the reason — fix those." >&2
    echo "       To fund the project without a provisionable provider, re-run with" >&2
    echo "       ONBOARD_PREFLIGHT=advisory (the default)." >&2
    exit 1
  fi
  echo "WARN: local-libvirt preflight reported problems; funding the project anyway." >&2
  echo "      A later 'no schedulable resource' denial is provider readiness, not funding." >&2
fi

banner "migrate (idempotent)"
# Schema-current migrate still SELECTs schema_migrations inside its advisory-lock transaction
# (kdive/db/migrate.py), and no migration grants that table to the runtime capability roles, so
# migrate connects through the migration authority (the apply-migrations.sh shape), not a member's.
KDIVE_DATABASE_URL="${KDIVE_MIGRATION_DATABASE_URL}" \
  env -u KDIVE_SERVER_DATABASE_URL -u KDIVE_WORKER_DATABASE_URL \
  -u KDIVE_RECONCILER_DATABASE_URL \
  "${py[@]}" -m kdive migrate

banner "seed (funding rows commit before resource discovery)"
seed_rc=0
if ! KDIVE_DATABASE_URL="${KDIVE_SERVER_DATABASE_URL}" \
  env -u KDIVE_MIGRATION_DATABASE_URL -u KDIVE_WORKER_DATABASE_URL \
  -u KDIVE_RECONCILER_DATABASE_URL \
  "${py[@]}" -m kdive seed-project \
  --project "$PROJECT" \
  --limit-kcu "$LIMIT_KCU" \
  --max-concurrent-allocations "$MAX_ALLOC" \
  --max-concurrent-systems "$MAX_SYS"; then
  seed_rc=1
fi

banner "verify (the hard funding gate)"
KDIVE_DATABASE_URL="${KDIVE_SERVER_DATABASE_URL}" \
  env -u KDIVE_MIGRATION_DATABASE_URL -u KDIVE_WORKER_DATABASE_URL \
  -u KDIVE_RECONCILER_DATABASE_URL \
  "${py[@]}" -m kdive verify-project --project "$PROJECT"

if [[ "$seed_rc" -ne 0 ]]; then
  echo "WARN: seed-project exited non-zero but the funding rows verified — its resource-discovery" >&2
  echo "      step likely failed (provider unreachable; see the preflight). Funding is committed." >&2
fi

if [[ "$ROLE" == "viewer" ]]; then
  echo "WARN: role '$ROLE' is below 'contributor'; the minted token cannot pass allocations.request." >&2
fi

banner "token + contract"
if token="$(
  "${py[@]}" - "$PROJECT" "$TTL" "$ROLE" <<'PY'
import sys

from kdive.cli.login import mint_local_token

project, ttl_seconds, role = sys.argv[1], int(sys.argv[2]), sys.argv[3]
print(
    mint_local_token(
        project=project,
        role=role,
        platform_roles=["platform_admin", "platform_operator"],
        ttl_seconds=ttl_seconds,
    )
)
PY
)"; then
  printf 'export KDIVE_TOKEN=%s\n' "$token"
else
  echo "WARN: token mint failed (is the mock-OIDC issuer up?). Re-mint when it is, e.g.:" >&2
  echo "      export KDIVE_TOKEN=\$(examples/local-libvirt/mint-token.sh --project ${PROJECT})" >&2
fi

if ((TTL % 86400 == 0)); then
  human_ttl="$((TTL / 86400))d"
elif ((TTL % 3600 == 0)); then
  human_ttl="$((TTL / 3600))h"
else
  human_ttl="$((TTL / 60))m"
fi

cat <<EOF

Token contract — these THREE strings must match for allocations.request to be granted:
  projects:["${PROJECT}"]
  roles:{"${PROJECT}":"${ROLE}"}
  project arg: "${PROJECT}"

The minted token expires in ${human_ttl}. WHEN IT EXPIRES, re-run 'just onboard' (or the mint
command above) and reconnect your MCP client — the client only re-reads KDIVE_TOKEN on reconnect.
DEMO ONLY: the bundled mock issuer mints a valid token for any caller — never against production.
EOF
