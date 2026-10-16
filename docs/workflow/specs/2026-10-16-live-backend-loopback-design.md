# Preserve live-stack backend loopback defaults

Issue #3235; approved scope `q3235-7c6c7ca3`.

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
Keep `backend_endpoint` unchanged: address-prefixed defaults already derive localhost URLs.
Existing ownership remains: env.sh exports publish values and derives host client endpoints;
Compose consumes those values. Callers need no migration and no old path is removed.
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

## Failure model

- Actors and deployments: local operators and CI sourcing env.sh for reference Compose or host stack.
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
  validates PORT/ADDR:PORT before deriving DSNs/URLs and rejects malformed input.
- Out of scope: explicit operator exposure, local hostile processes, firewall rules, observability
  listeners and other services; these require separate operator policy.

## Validation

Extend existing script tests for unset/empty exports and default endpoint derivation.
Extend existing Compose tests to source env.sh in a scrubbed environment before rendering,
covering default/empty plus explicit bare-port, loopback and wildcard overrides for all three
backends. First observe default cases fail without host_ip, then change defaults and rerun.
Run focused script endpoint and Compose tests, `just lint`, `just type`, `just lint-shell`.
The mandatory clean-checkout pre-push `just ci` owns full local verification; CI is separate.
No live VM proof is required for shell interpolation and rendered network configuration.
