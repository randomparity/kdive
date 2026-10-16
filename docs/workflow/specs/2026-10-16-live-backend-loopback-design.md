# Preserve live-stack backend loopback defaults

Issue #3235; approved scope `q3235-f053d130`.

## Problem

Sourcing `scripts/live-stack/env.sh` exports bare Postgres, SeaweedFS and OIDC ports.
Those values override Compose's loopback fallbacks and expose fixed-credential backends.
A clean-environment source followed by `docker compose config --format json` reproduces
Postgres publishing without `host_ip`. [ADR-0554](../../adr/0554-compose-backends-bind-loopback-by-default.md)
already requires loopback defaults and full-left-side operator overrides.

## Scope and design

Change only the three env.sh defaults to `127.0.0.1:5432`, `127.0.0.1:8333`,
and `127.0.0.1:8090`. Retain `${VAR:-default}` semantics: unset/empty uses the safe default;
explicit nonempty bare PORT and ADDR:PORT values remain verbatim.
Move `backend_endpoint` unchanged from env.sh into `scripts/live-stack/backend-endpoint.sh`.
The helper defines the sourceable function and, when directly executed, prints the endpoint
for the named exported publish variable. env.sh sources it before its existing endpoint calls.
The four `justfile` Compose lifecycle recipes use the executable helper to derive a witness
endpoint only when `KDIVE_LIFECYCLE_WITNESS_DATABASE_URL` is unset or empty. Default the helper's
Postgres input to 5432 when unset/empty, preserving localhost:5432 before env.sh is sourced.
Guard endpoint assignment before invoking uv; invalid publish inputs must stop the recipe.
Explicit witness URLs bypass parsing unchanged. Preserve current worker URL defaults/overrides;
do not source env.sh in the recipe. Bare port and loopback/wildcard addresses resolve to
localhost; other addresses remain the endpoint host, including bracketed IPv6.
The shared helper owns publish-to-endpoint parsing; env.sh retains exports/host URL defaults;
recipes retain witness/worker authority defaults. Remove the old inline parser and four raw-port
witness URL constructions. The human approved this caller correction after reproducing the
source-env then compose-stop invalid witness DSN.
No new policy or dependency is introduced; ADR-0554 already governs publish defaults/overrides.
No new ADR is necessary; the assigned optional 0771 reservation is unused.
Prometheus/Grafana exposure policy and broader network policy remain operator-owned exclusions.

## Success

- For the three named backend variables, unset and empty inputs source to loopback defaults.
- The sourced environment renders Postgres, SeaweedFS and OIDC bindings on IPv4 loopback,
  preserving canonical target ports 5432, 8333 and 8080 and published ports 5432, 8333 and 8090.
- Explicit nonempty bare-port, loopback and wildcard overrides remain usable after sourcing;
  their published port and bind address survive Compose interpolation.
- The five existing database DSNs, S3 endpoint, OIDC issuer and JWKS URI retain correct
  localhost ports for defaults; existing address/port parsing and invalid-input tests pass.

- The four lifecycle recipes derive valid witness endpoints for sourced defaults, relocated bare
  ports, loopback/wildcard IPv4/IPv6 and non-loopback addresses; explicit witness URLs remain
  verbatim even with an invalid publish value. Invalid default inputs stop before uv.
- Existing worker URL behavior survives: caller values stay verbatim and absent values use
  the existing container-internal default for up/recreate, with no host defaults sourced by recipes.

## Failure model

- Actors and deployments: local operators and CI sourcing env.sh for reference Compose or host stack, and invoking
  compose-up/stop/recreate-worker/down recipes with or without a sourced host environment.
- Invariants and assets: backend database/artifact confidentiality and mock issuer access;
  operator override compatibility and reachable derived endpoints.
- Accepted failure classes: explicitly widened bindings are intentional operator control;
  tests render configuration without starting containers, so they do not certify firewall behavior.
- Covered elsewhere: Docker publish implementation and host firewall policy are operator/platform
  responsibilities; endpoint validation remains the existing backend_endpoint checks.

## Threat model

- Boundary inventory: existing shell environment enters Compose interpolation; host port publishing
  crosses the host network boundary. No boundary is added or widened by this change.
- Actor model: off-host anonymous clients must not reach default fixed-credential backends;
  local operators are trusted to choose explicit exposure overrides.
- Controls: full-left-side loopback defaults prevent implicit wildcard publishing; existing parser
  validates PORT/ADDR:PORT before deriving DSNs/URLs and rejects malformed input. Recipes
  use guarded endpoint assignment; explicit witness DSNs remain trusted operator configuration.
- Out of scope: explicit operator exposure, local hostile processes, firewall rules, observability
  listeners and other services; these require separate operator policy.

## Validation

Extend existing script tests for unset/empty exports and default endpoint derivation.
Extend existing Compose tests to source env.sh in a scrubbed environment before rendering,
covering default/empty plus explicit bare-port, loopback and wildcard overrides for all three
backends. First observe default cases fail without host_ip, then change defaults and rerun.
Run actual four-recipe tests with only the uv process boundary stubbed; assert witness and worker
URLs for the specified inputs and no uv invocation on malformed default publish input.
The prepared recipe regression produced 28 expected failures and eight passes before correction.
Preserve existing source/parser validation tests and structural recipe authority assertions.
Run focused script endpoint and Compose tests, `just lint`, `just type`, `just lint-shell`.
The mandatory clean-checkout pre-push `just ci` owns full local verification; CI is separate.
No live VM proof is required for shell interpolation and rendered network configuration.
