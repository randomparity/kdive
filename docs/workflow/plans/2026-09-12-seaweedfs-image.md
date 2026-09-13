# SeaweedFS image — Implementation Plan

## Goal

Deliver the ADR-0647 image producer without changing a bundled object-store consumer.

## Tasks

1. Add the source- and base-pinned `weed mini` Dockerfile and its local Compose selection file.
2. Add a reusable S3 behavior proof that waits for the endpoint, then checks the ADR-0647
   versioning, exact-version, multipart, and presigned transfer requirements.
3. Add focused static tests and a least-privilege workflow that runs the proof for amd64, arm64,
   and ppc64le before pushing a manifest with the same platforms.

## Verification

- `just lint`, `just type`, and focused image tests pass.
- The local native image boots and passes `scripts/verify-seaweedfs-image.sh`.
- The publish workflow records each architecture proof and the published manifest platform list.

## Boundaries

Do not modify `docker-compose.yml`'s MinIO services, Helm, fixtures, Python, schema, artifact
keys, external S3 behavior, or existing MinIO-volume handling.
