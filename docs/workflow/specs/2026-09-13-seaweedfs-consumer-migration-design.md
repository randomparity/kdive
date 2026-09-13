# SeaweedFS bundled-consumer migration

## Status

Proposed for issue #2445.

## Scope

Replace only KDIVE's bundled MinIO consumers: Compose, the Helm demo, Docker-backed store fixture,
ADR-0356 matrix evidence, and operator documentation. External `KDIVE_S3_*` configuration remains
unchanged. This design does not change Python object-store behavior, schema, artifact keys, or
perform data conversion.

## Decision

Consumers use the KDIVE-owned SeaweedFS image contract from ADR-0647 and #2446. Compose keeps
the locally built `kdive-seaweedfs:dev` default and the documented digest override. Helm cannot
build that tag, so its demo defaults to the publicly pullable published image
`ghcr.io/randomparity/kdive-seaweedfs@sha256:6a4e9f013ecd9c1f86136ed3d3c7eb089c8eb13ff3eee83044ab2a2950f7ce6c`;
that OCI index contains linux/amd64, linux/arm64, and linux/ppc64le. Values may override it only
as an explicit operator choice. They run `weed mini` with a dedicated SeaweedFS data volume and
an authenticated S3 endpoint on port 8333.

The initializer is a repository-owned Python entry point, added under `scripts/`, run from the
already-owned, pinned KDIVE application image (which has the locked boto3 dependency). Compose's
one-shot and Helm's Job/barrier invoke that same entry point with the bundled endpoint and
credentials. Its retry count and delay are explicit constants; it creates the bucket idempotently,
enables versioning, verifies `Status=Enabled`, and exits non-zero for any connection, create, or
status failure. No companion client image is introduced.

Compose replaces the `minio` and `minio-init` service names, endpoint, environment names, health
probe, dependency barriers, ports, and volume with SeaweedFS equivalents. The default image is the
local KDIVE image, with the ADR-0647 override mechanism retained. Helm's demo objects, helper
names, barrier, service and NetworkPolicy names make the same substitution without changing the
external endpoint override path. The Docker fixture starts the same KDIVE image and exercises the
contract through boto3 rather than relying on MinIO-only administration tooling.

The transition is deliberately empty-store-only: operators stop the old stack, retain
`kdive-minio-data` untouched for rollback, create the new SeaweedFS volume, and start the stack.
Export/import old objects needs a separately approved procedure. No service may mount or infer the
old volume.

## Failure model

- A SeaweedFS image cannot start or expose S3: health/readiness stays failing; dependents do not
  start as initialized.
- Bucket creation/versioning fails: the initializer exits non-zero and application barriers fail
  closed.
- An operator has an existing MinIO volume: it is retained, never mounted by SeaweedFS, and the new
  bucket begins empty.
- An external S3 operator supplies `KDIVE_S3_*`: bundled-demo substitutions do not rewrite their
  endpoint or credentials.
- Required architecture evidence is absent: ADR-0356 guard/matrix rejects the bundled image
  relationship before merge.

## Verification

Focused Compose/Helm render and fixture tests prove the pinned Helm image, endpoint, shared
initializer invocation, readiness/barrier, versioning,
version-specific operations, multipart, presigned operations, and named-volume separation. Update
ADR-0356's matrix and guard test for the KDIVE-owned image across amd64, arm64, and ppc64le.
Run relevant tests plus the repository gate; live Docker behavior runs where Docker is available.
