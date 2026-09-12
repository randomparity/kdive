# 0639 — MinIO images use the official Quay registry

## Status

Superseded (2026-09-12)

> **Superseded by [0647](0647-supported-s3-backend-and-compose-data-transition.md)**
> (2026-09-12)

## Context

CI run 34654301800 and PR 2430's CI run 34662806148 both failed before tests completed:
Docker Hub returned `pull access denied` for `minio/minio` and `minio/mc`. Quay serves the
same release tags. `docker buildx imagetools inspect` resolved the exact deployed tags
`quay.io/minio/minio:RELEASE.2025-04-22T22-12-26Z` and
`quay.io/minio/mc:RELEASE.2025-04-16T18-13-26Z`, plus the fixture tag; the latter reported the
existing digest `sha256:14cea493d9a34af32f524e538b8346cf79f3321eff8e708c1e2960462bd8936e`.

## Decision

Use `quay.io/minio/minio` and `quay.io/minio/mc` for Compose, Helm, and test image references.
Keep the existing release tags and the store fixture's immutable digest. The consistency guard
continues to require the pre-pull script and fixture to request the same image.

## Consequences

- CI and local/Helm startup no longer depend on Docker Hub's removed MinIO repositories.
- Image content remains pinned for the test fixture; deployed images retain their current tags.
- Quay availability is now an external operational dependency and is not retried or mirrored here.

## Considered & rejected

- **Keep Docker Hub and retry longer.** verified: `docker manifest inspect minio/minio:RELEASE.2025-09-07T16-13-09Z` and the two cited CI logs returned `denied: requested access to the resource is denied`; retries cannot restore a removed repository.
- **Add a private mirror or credentials.** judgment: this expands operator setup and supply-chain ownership beyond the requested CI/compose repair.
- **Upgrade MinIO while changing registries.** judgment: changing image content would obscure whether the registry migration fixed the failure and adds unrelated compatibility risk.
