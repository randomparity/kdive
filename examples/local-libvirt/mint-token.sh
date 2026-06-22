#!/usr/bin/env bash
# Mint a developer bearer token for the local-libvirt example and print it to stdout:
#
#   export KDIVE_TOKEN=$(examples/local-libvirt/mint-token.sh)
#
# The token carries roles={<KDIVE_PROJECT>: admin} plus platform_admin/platform_operator.
# admin is required so the token reaches every tool, including the admin-gated
# control.force_crash used by the kdump / host_dump capture legs. The same project name the
# stack was seeded with (env.sh) goes into the claim, so the grant lands on a project that
# actually has budget and quota.
#
# The token is minted from the local mock-OIDC issuer (KDIVE_OIDC_ISSUER, default
# http://localhost:8090/default) via its authorization-code flow, reusing the single
# source of truth in kdive.cli.login. Its `iss` matches what the host processes validate
# against, so no kubectl/port-forward dance is needed (unlike the Helm demo-token.sh).
#
# DEV ONLY: the bundled mock issuer mints a valid token for any caller. Never run this
# against a real deployment; production supplies its own OIDC token via $KDIVE_TOKEN.
set -euo pipefail

example_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=examples/local-libvirt/env.sh disable=SC1091
source "${example_dir}/env.sh"

exec "${KDIVE_PYTHON}" - "${KDIVE_PROJECT}" <<'PY'
import sys

from kdive.cli.login import (
    OidcIssuer,
    _authorization_code,
    _build_claims,
    _exchange_code,
)

project = sys.argv[1]
issuer = OidcIssuer.from_config()
claims = _build_claims(
    subject="local-dev",
    audience=issuer.audience,
    projects=[project],
    roles={project: "admin"},
    platform_roles=["platform_admin", "platform_operator"],
    agent_session=None,
)
code = _authorization_code(issuer, claims)
print(_exchange_code(issuer, code))
PY
