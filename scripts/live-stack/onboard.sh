#!/usr/bin/env bash
# Onboard a dev-stack project so a fresh agent's first allocations.request is granted (#834,
# ADR-0256). One command that funds a project against the SAME database the server reads, makes
# the project string the single source of truth, verifies the seed landed, and mints a token.
#
#   just onboard                 # project "demo" (default)
#   KDIVE_PROJECT=acme just onboard
#
# Order: source env.sh -> advisory preflight -> migrate -> seed -> verify -> mint + contract.
# Hard gates: migrate and verify-project (the funding rows are present). Advisory (warn,
# non-fatal): the provider preflight, the seed's resource-discovery side effect, and the token
# mint. seed-project commits the budget/quota upserts BEFORE it registers discovered resources,
# so a discovery failure (provider unreachable) still leaves a funded project that verify
# confirms — verify, not the seed exit code, is the funding source of truth.
#
# DEMO ONLY: the bundled mock-OIDC issuer mints a valid token for any caller. Never run this
# against a real deployment; production onboards a project with the audited admin tools
# (docs/operating/project-onboarding.md).
#
# Env overrides: KDIVE_PROJECT (demo), KDIVE_ROLE (admin), KDIVE_TOKEN_TTL (86400 = 24h),
#   KDIVE_LIMIT_KCU (1000000), KDIVE_MAX_ALLOC (4), KDIVE_MAX_SYS (4).
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${here}/../.." && pwd)"
# shellcheck source=scripts/live-stack/env.sh disable=SC1091
source "${here}/env.sh"
cd "$repo_root"

PROJECT="${KDIVE_PROJECT:-demo}"
ROLE="${KDIVE_ROLE:-admin}"
TTL="${KDIVE_TOKEN_TTL:-86400}"
LIMIT_KCU="${KDIVE_LIMIT_KCU:-1000000}"
MAX_ALLOC="${KDIVE_MAX_ALLOC:-4}"
MAX_SYS="${KDIVE_MAX_SYS:-4}"

banner() { printf '\n=== %s ===\n' "$1"; }

banner "preflight (advisory)"
if ! "${repo_root}/scripts/check-local-libvirt.sh"; then
  echo "WARN: local-libvirt preflight reported problems; funding the project anyway." >&2
  echo "      A later 'no schedulable resource' denial is provider readiness, not funding." >&2
fi

banner "migrate (idempotent)"
uv run python -m kdive migrate

banner "seed (funding rows commit before resource discovery)"
seed_rc=0
if ! uv run python -m kdive seed-project \
  --project "$PROJECT" \
  --limit-kcu "$LIMIT_KCU" \
  --max-concurrent-allocations "$MAX_ALLOC" \
  --max-concurrent-systems "$MAX_SYS"; then
  seed_rc=1
fi

banner "verify (the hard funding gate)"
uv run python -m kdive verify-project --project "$PROJECT"

if [[ "$seed_rc" -ne 0 ]]; then
  echo "WARN: seed-project exited non-zero but the funding rows verified — its resource-discovery" >&2
  echo "      step likely failed (provider unreachable; see the preflight). Funding is committed." >&2
fi

if [[ "$ROLE" == "viewer" ]]; then
  echo "WARN: role '$ROLE' is below 'contributor'; the minted token cannot pass allocations.request." >&2
fi

banner "token + contract"
if token="$(
  uv run python - "$PROJECT" "$TTL" "$ROLE" <<'PY'
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

cat <<EOF

Token contract — these THREE strings must match for allocations.request to be granted:
  projects:["${PROJECT}"]
  roles:{"${PROJECT}":"${ROLE}"}
  project arg: "${PROJECT}"

The minted token expires in $((TTL / 3600))h. WHEN IT EXPIRES, re-run 'just onboard' (or the mint
command above) and reconnect your MCP client — the client only re-reads KDIVE_TOKEN on reconnect.
DEMO ONLY: the bundled mock issuer mints a valid token for any caller — never against production.
EOF
