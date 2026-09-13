# KDIVE SeaweedFS image — Design

## Problem

ADR-0647 selects SeaweedFS 4.46 for the future bundled object-store paths, but its upstream
container does not establish the required ppc64le support. KDIVE needs a reproducible image that
can be built locally and published as a manifest before #2445 changes any consumer.

## Scope

`deploy/seaweedfs/` will build only the static `weed` binary from the ADR-pinned source archive
and run `weed mini -dir=/data` as an unprivileged user. The source archive and both multi-arch
base-image indexes are checksum/digest pinned. A separate Compose file offers the OIDC-style
local tag default and a digest-override environment variable, but does not replace the current
MinIO service or mount its volume.

### Failure model

- **Actors and deployments:** local developers, GitHub Actions, and the future #2445 consumer;
  external S3 users are unaffected.
- **Invariants:** source and base bytes are immutable; the image has only `/data` persistent;
  its S3 endpoint is port 8333; all three required architectures boot and pass the ADR suite.
- **Accepted failures:** an unset published-image override builds locally; a failed build,
  runtime proof, or manifest inspection blocks publication and #2445 adoption.
- **Covered elsewhere:** #2445 owns MinIO replacement and data transition execution; an operator
  owns export/import of old data.

## Architecture

The Dockerfile downloads the exact GitHub source tarball and verifies its SHA-256 before a
`CGO_ENABLED=0` cross-build. The runtime contains only the binary and `/data`. A workflow first
loads and runs one image for each required architecture under Buildx/QEMU, using the same
repository proof script, then publishes a GHCR manifest only after those jobs pass. Its override
is intentionally optional: a digest cannot be both a Compose build tag and a local build target.

## Success

The image and workflow have no mutable source or base reference. The runtime proof exercises
readiness, versioning, exact-version reads/deletes, multipart upload, and presigned GET/PUT on
amd64, arm64, and ppc64le. The existing Compose graph remains MinIO-based.

## Validation

- `focused-test`: static image/Compose/workflow contract tests; run the focused image tests.
- `focused-test`: `scripts/verify-seaweedfs-image.sh` against a locally built amd64 image.
- `focused-test`: workflow matrix executes that same proof for amd64, arm64, and ppc64le before
  publishing; its manifest inspection verifies all three platforms.
- `task-test-not-applicable`: no Helm or external-S3 test changes belong to this image-only PR.
