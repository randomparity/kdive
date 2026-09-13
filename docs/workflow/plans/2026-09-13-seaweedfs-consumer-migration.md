# Plan: SeaweedFS bundled-consumer migration

1. Inventory existing Compose, Helm, fixture, matrix, and operator MinIO references; retain
   Python/external-S3 references outside the approved closure.
2. Add one repository-owned boto3 readiness/versioning initializer and test its bounded retry,
   idempotent create, enabled-status, and failure behavior; invoke it from the existing KDIVE
   application image in both deployment paths.
3. Replace Compose service, endpoint, volume, dependencies, and focused config/live tests with
   the ADR-0647 local-build/digest-override SeaweedFS contract.
4. Replace Helm demo templates, values, endpoint helper, initialization barrier, NetworkPolicy,
   render tests, and demo documentation. Pin the default to #2446's published immutable KDIVE
   image digest; keep external-S3 mode free of the bundled workload.
5. Change the Docker-backed store fixture and focused behavior tests to the KDIVE-owned image.
6. Update ADR-0356 matrix/guard and operator transition documentation, including retained old
   `kdive-minio-data` and a distinct new SeaweedFS volume.
7. Run focused tests, relevant configuration/render checks, then full pre-push guardrails. Obtain
   independent review, security scan, simplification pass, and exact-head CI before publication.

## Non-goals

No Python ObjectStore, database, artifact-key, external-S3 contract, automatic conversion,
in-place volume reuse, unrelated POWER/OIDC/Grafana work, or broad refactor.
