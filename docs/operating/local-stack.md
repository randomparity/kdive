# Local KDIVE Stack Administration

This guide assumes KDIVE is installed as a Python package on the libvirt host. It does not
use `just` or require running from a source checkout.

The app processes (`server` / `worker` / `reconciler`) and the `migrate` one-shot now run
from the published image via the reference compose app tier (ADR-0088); the hand-rolled
`stack` supervisor and the `install-compose`/`print-local-env` helpers were retired. See
[`deploy/compose/README.md`](../../deploy/compose/README.md) for the compose bring-up and
[the config reference](../guide/reference/config.md) for every `KDIVE_*` variable.

## Backing Services

The repo-root [`docker-compose.yml`](../../docker-compose.yml) declares the backing services
(Postgres, MinIO, mock OIDC) alongside the app tier. Bring the backends up:

```bash
docker compose up -d --wait postgres minio oidc
docker compose run --rm minio-init
```

`minio-init` creates the configured bucket, enables versioning for the whole bucket, and fails
unless MinIO reports `Enabled`, MFA Delete off, and no prefix/folder exclusions. Do not bypass the
initializer: KDIVE permanently deletes immutable object versions and has no key-only cleanup path.

Production-like deployments may replace these containers with managed Postgres, managed
S3-compatible object storage, and a real OIDC issuer. The KDIVE processes only require the
environment variables documented in [the config reference](../guide/reference/config.md).
An external bucket must provide the same bucket-wide state and grant
`s3:GetObjectVersion`, `s3:GetBucketVersioning`, `s3:ListBucketVersions`, and
`s3:DeleteObjectVersion`. Adopt it in a
stop-old-first window: quiesce old processes, grant and verify IAM and the no-exclusions/MFA-off
policy, enable versioning, wait for activation, migrate, then start only the new image. Suspending
versioning or rolling back live to a pre-ADR-0524 image is unsupported; see
[Installing KDIVE](install.md) for the full procedure.

## Environment

Install the default local-libvirt fixture catalog:

```bash
python -m kdive install-fixtures --dest /etc/kdive/fixtures/local-libvirt
```

Set the `KDIVE_*` environment from [the config reference](../guide/reference/config.md),
especially:

- `KDIVE_DATABASE_URL`
- `KDIVE_OIDC_*`
- `KDIVE_S3_*`
- `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`
- `KDIVE_KERNEL_SRC`
- `KDIVE_FIXTURE_CATALOG_PATH`

## Schema

```bash
python -m kdive migrate
```

## Seed A Demo Project

```bash
python -m kdive seed-project \
  --project demo \
  --limit-kcu 1000000 \
  --max-concurrent-allocations 4 \
  --max-concurrent-systems 4
```

This creates the budget/quota rows needed for agent allocations and registers the local
libvirt resource discovered on the host.

`seed-project` is a token-less bootstrap: it writes the rows with raw `INSERT`s at deploy
time, before any request, so the writes are not role-gated and leave no audit row. For a
production tenant, onboard the project with the audited admin tools instead — see
[Project onboarding](project-onboarding.md).

## Start The Stack

Run the app tier from the compose reference (builds the image, runs the backends and the
`migrate` one-shot first):

```bash
docker compose up -d migrate server worker reconciler
```

To run the processes directly under a process manager such as systemd instead of compose:

```bash
python -m kdive server
python -m kdive worker
python -m kdive reconciler
```
