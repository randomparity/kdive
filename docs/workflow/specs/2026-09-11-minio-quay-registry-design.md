# MinIO image registry migration

## Problem

CI and compose smoke tests fail before application code runs because Docker Hub now rejects
the pinned `minio/minio` and `minio/mc` repositories. Official Quay images expose the same
release tags. `docker buildx imagetools inspect` resolves the exact deployed MinIO and mc tags,
and the test image's manifest digest is unchanged (`sha256:14cea...d8936e`).

## Scope

Change the registry hostname for the existing MinIO server and client image references in
Compose, Helm, the store fixture, and the CI pre-pull script. Preserve release tags and digest
pins. Teach the existing ADR-0356 drift guard that the historical Docker Hub and official Quay
names are aliases for this image set. Update affected documentation and add an ADR recording
the source decision.

## Failure model

- Actors and deployments: CI jobs, local Compose operators, Helm operators, and pytest store fixtures.
- Invariants and assets at stake: reproducible image content, stack startup, and CI availability.
- Accepted failure classes: Quay outage or tag removal remains an external availability failure;
  immutable digests and bounded CI retries limit content drift and diagnosis cost.
- Covered elsewhere: Docker daemon and registry authentication are owned by CI/registry operators;
  image-content vulnerabilities are covered by the supply-chain jobs.

### Threat model

- Boundary inventory: CI and deployment configuration select an external registry; image bytes cross
  from Quay into Docker daemons. No untrusted user input controls the references.
- Actor model: CI runners and authorized operators are trusted to pull public images; Quay is the
  external publisher and is not treated as a mutable trusted source when a digest is pinned.
- Controls: retain immutable digests for pytest, pin release tags for deployed images, and keep the
  existing fixture/pre-pull consistency guard. Pull failures remain fail-fast diagnostics.
- Out of scope: private mirrors, credentials, image signing policy, and remediation of a future Quay
  outage are owned by infrastructure and supply-chain workflows.

## Success

The CI pre-pull step and compose smoke graph resolve MinIO images from Quay. The store fixture
continues to request the same digest, Helm renders the same release versions from Quay, and
the ADR-0356 guard passes without rewriting its accepted historical matrix. The guard
canonicalizes only `quay.io/minio/minio:<ref>` and `quay.io/minio/mc:<ref>` to their historical
Docker Hub names; unrelated registries remain distinct. Compose smoke is conclusive only when
`KDIVE_IMAGE` and Docker Compose are available and the test is not skipped.

## Validation

- `focused-test`: `tests/guards/test_prepull_images_match_fixtures.py` passes and confirms the
  pre-pull image equals the fixture's Quay-qualified digest; run `uv run pytest tests/guards/test_prepull_images_match_fixtures.py -q`.
- `focused-test`: `tests/image/test_compose_smoke.py::test_migrate_then_server_reaches_readyz`
  starts the graph with Quay images; run `uv run pytest tests/image/test_compose_smoke.py::test_migrate_then_server_reaches_readyz -q`.
- `focused-test`: `tests/helm/test_helm_render.py::test_bundled_app_workloads_share_minio_versioning_startup_barrier` confirms rendered MinIO references remain
  valid; run `uv run pytest tests/helm/test_helm_render.py -q`.
- `focused-test`: `tests/scripts/test_check_container_arch_matrix.py::test_official_quay_alias_matches_matrix`
  proves Quay refs satisfy the historical matrix; run `uv run pytest tests/scripts/test_check_container_arch_matrix.py::test_official_quay_alias_matches_matrix -q`.
- `focused-test`: `tests/scripts/test_check_container_arch_matrix.py::test_unrelated_registry_remains_distinct`
  proves aliasing cannot hide another registry; run `uv run pytest tests/scripts/test_check_container_arch_matrix.py::test_unrelated_registry_remains_distinct -q`.
- `task-test-not-applicable`: ADR and documentation amendments have no executable consumer beyond
  the repository records and docs-link guards; `just adr-status-check` and `just docs-links` cover structure.
