# 0647 — SeaweedFS is the bundled S3 backend

## Status

Proposed

## Context

KDIVE's bundled Compose, Helm-demo, and disposable test paths use archived MinIO images.
The object-store contract itself is provider-neutral, but it requires bucket versioning,
version-aware reads and deletes, multipart upload, presigned GET/PUT URLs, and readiness
(ADR-0017). The normal developer stack must also work on amd64, arm64, and ppc64le under
ADR-0356.

The upstream `chrislusf/seaweedfs:3.99` image does not establish ppc64le coverage. The
selected source is SeaweedFS release `4.46`, commit
`d997fba1575583a89cf0cc50dc0150642286c86d`. Its README at that commit documents
`S3_BUCKET=<bucket> weed mini -dir=<data-dir>` as a ready-to-use S3 store on port 8333,
and its source tree includes S3 versioning, multipart, and presigned-request tests. Upstream
source coverage is not KDIVE runtime compatibility evidence.

## Decision

KDIVE will replace the bundled MinIO implementation with an in-repository SeaweedFS image
built from that exact source revision. The image implementation is owned by proposed #2446;
Compose, Helm-demo, and test-fixture adoption are owned by #2445.

The image contract is:

1. Build the `weed` binary from the pinned source revision and run a single-node
   `weed mini -dir=/data` process. Set `S3_BUCKET=kdive-artifacts`; use an explicit
   development access-key/secret configuration supplied by the consumer, not an unauthenticated
   default. The image exposes the S3 endpoint on port 8333 and persists only `/data`.
2. Follow the mock-OIDC pattern: Compose defaults to a locally built
   `kdive-seaweedfs:dev` image and permits an environment-variable image override for a
   published immutable digest. A future publish workflow must build and inspect one manifest
   containing `linux/amd64`, `linux/arm64`, and `linux/ppc64le`; it must record the inspected
   digest. A consumer never treats an upstream SeaweedFS manifest as this evidence.
3. Before a consumer changes a default backend, #2446 must prove against the built image with
   boto3 or AWS CLI that the configured bucket reports `Status=Enabled`; a version-specific
   GET and delete select the requested version; multipart create/upload/complete reads back the
   assembled object; generated presigned GET and checksum-bearing PUT URLs succeed; and the
   readiness probe waits for the S3 API and creates/enables the configured bucket idempotently.
   #2446 owns this proof on amd64, arm64, and ppc64le: each architecture must boot `weed mini`,
   pass readiness, and pass the named S3 behavior suite. Manifest inspection alone proves only
   pullability. #2445 must not make SeaweedFS the bundled default until #2446 has recorded all
   three runtime proofs.
4. Existing `kdive-minio-data` volumes are incompatible input. The supported transition is to
   stop the old stack, retain that volume unchanged as a rollback artifact, create a new named
   SeaweedFS data volume, and start the new stack with an empty bucket initialized by its own
   readiness path. Operators who need old objects export/import them with an explicit,
   separately approved procedure; neither #2445 nor #2446 reuses or converts the old volume.

ADR-0356 remains the authority for the Compose image-matrix guard. #2445 must change its
matrix row and Compose build relationship together; #2446 supplies the source, image, and
manifest proof those consumers cite.

## Consequences

- The bundled backend becomes a KDIVE-owned, source-pinned artifact with a local-build default
  and an optional digest-pinned pull path.
- A failed image build, manifest inspection, per-architecture runtime/S3 compatibility proof, or
  readiness proof blocks consumer migration rather than falling back to MinIO or an
  unauthenticated service.
- An existing MinIO volume is preserved but is not made readable by the new service. The new
  default begins empty unless an operator separately migrates data.
- External `KDIVE_S3_*` deployments remain unchanged; this decision governs only bundled
  development/demo/test consumers.

## Considered & rejected

- **Continue using Quay MinIO aliases.** verified: ADR-0639 documents registry availability,
  but the upstream MinIO image family remains archived and does not provide a maintained backend
  decision.
- **Use an upstream SeaweedFS image directly.** verified: `docker buildx imagetools inspect
  chrislusf/seaweedfs:3.99` on 2026-09-12 listed amd64 and arm64 but not ppc64le; it cannot meet
  ADR-0356's required coverage.
- **Reuse or convert `kdive-minio-data` automatically.** judgment: a backend-specific persisted
  layout is not a safe compatibility promise without a dedicated migration and rollback contract.
- **Adopt SeaweedFS in this ADR issue.** judgment: image construction/proof and consumer migration
  have independently reviewable failure modes and remain owned by #2446 and #2445.
