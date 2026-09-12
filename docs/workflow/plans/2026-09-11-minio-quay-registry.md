# Goal

Move MinIO image references from unavailable Docker Hub repositories to the official Quay
repositories while preserving release tags and the pinned test digest.

## Architecture

Compose and Helm own deployed image locations. The pytest store fixture and `pull-test-images.sh`
must remain byte-for-byte aligned through the existing guard. No runtime Python code changes.

## Tech stack

YAML, shell, Python tests, Helm templates, and repository ADR/doc guards.

## Global Constraints

- Preserve `RELEASE.2025-04-22T22-12-26Z` for MinIO and `RELEASE.2025-04-16T18-13-26Z` for mc.
- Preserve fixture digest `sha256:14cea493d9a34af32f524e538b8346cf79f3321eff8e708c1e2960462bd8936e`.
- Use official `quay.io/minio/minio` and `quay.io/minio/mc` repositories; add no credentials or dependency.
- New ADR-0639 starts Proposed and is ratified only when its implementing PR merges.
- Run `just lint`, `just type`, focused tests, and relevant doc/ADR guards.

Expected implementation size: 8–20 changed lines (S) — four image-reference owners plus documentation.

## Task 1 — Update image references and documentation

Files: `docker-compose.yml`, `deploy/helm/kdive/values.yaml`, `tests/store/conftest.py`,
`scripts/pull-test-images.sh`, `scripts/guards/check_container_arch_matrix.py`, its tests,
`tests/helm/test_helm_render.py`,
and affected operational/ADR documentation.

Interfaces: the fixture constant and pre-pull array are consumed by the existing consistency
guard; Compose and Helm consumers expect image strings with the same tags.

### Verification

- `focused-test`: consistency guard; before the edit it passes for Docker Hub and after the edit
  it must pass for Quay-qualified refs: `uv run pytest tests/guards/test_prepull_images_match_fixtures.py -q`.
- `task-test-not-applicable`: plain YAML/doc reference edits have no narrower executable contract;
  `just docs-links`, `just docs-paths`, and `just adr-status-check` validate repository structure.

Steps:

1. Replace only the MinIO registry hostname in Compose, Helm, fixture, and pre-pull references.
2. Canonicalize only the two exact official Quay MinIO repositories (preserving tag/digest suffixes)
   to the historical Docker Hub names for ADR-0356 set equality; unrelated registries remain distinct.
   Add README-format dated amendments to ADR-0356 and ADR-0017, and add ADR-0639 as Proposed.
3. Update the Helm assertion to accept the Quay mc prefix. Run consistency, alias, Helm, and
   compose-smoke focused tests; a skipped compose smoke is inconclusive. Confirm
   `docker buildx imagetools inspect` resolves both exact deployed Quay tags and the pinned fixture digest.
4. Run `just lint`, `just type`, and relevant doc/ADR guards; review the diff for unrelated changes.

Rollback: restore the prior hostnames only if Docker Hub republishes the repositories; that rollback
would reproduce the observed failure and is not a valid CI repair while Docker Hub remains unavailable.
