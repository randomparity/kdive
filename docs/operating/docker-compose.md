# Running KDIVE with Docker Compose

The repo-root [`docker-compose.yml`](../../docker-compose.yml) brings up the three KDIVE
processes (`server` / `worker` / `reconciler`), the `migrate` one-shot, and a set of dev
backends (Postgres, MinIO, mock OIDC) in a single dependency graph. This is the fastest way
to a working MCP endpoint for demos and evaluation; it is not a production deployment.

The full value reference for the app tier — image selection, the `x-backends` anchor, and
pre-building the image — is in [`deploy/compose/README.md`](../../deploy/compose/README.md).

## Bring-up

The operator-side lifecycle wrapper resolves the graph and gates the worker, so one command starts
the stack. Compose does not run a persistent lifecycle-witness service:

```bash
just compose-up
```

The four supported worker lifecycle recipes are `just compose-up`, `just compose-stop`,
`just compose-recreate-worker`, and `just compose-down`. `just compose-stop` preserves named
volumes after recording worker termination; `just compose-down` removes named volumes for a
destructive teardown. Those volumes are `kdive-pgdata` (the database), `kdive-minio-data` (the
artifacts bucket), and `kdive-build` / `kdive-install`;
`docker compose down --volumes` and `scripts/live-stack/down.sh --wipe` drop them too, and
nothing else does. These recipes preserve exact worker-incarnation evidence in Postgres. Raw
Compose/Docker lifecycle commands and host workers bypass that evidence boundary. A database failure
is fail-closed, retaining the never-started or terminal worker so the same recipe can be retried
after Postgres recovers.

Configuration is read from `KDIVE_*` variables; see
[the config reference](../guide/reference/config.md) for every setting.

## Backend and migrate ordering

The app services declare `depends_on: migrate` with
`condition: service_completed_successfully`, so no process reaches the database before the
schema is rolled forward. The `migrate` one-shot itself waits on a healthy Postgres, and the
`minio-init` bucket initializer completes before any app process starts, so the worker's first
artifact write never races a missing bucket. After creating the configured bucket, it enables
bucket-wide versioning and verifies `Enabled`, MFA Delete off, and no MinIO prefix/folder
exclusions. A suspended, malformed, or excluded state makes the one-shot fail and blocks app
start. A non-zero `migrate` exit also blocks app start. You do not order these services by hand —
Compose does it from the graph.

## One-time note: upgrading a stack created before the volumes were named

Before ADR-0552 the backends had no declared data volume, so Compose allocated an anonymous
one per `up` and a plain `down` orphaned it — the stack already restarted empty after every
teardown. Naming the volumes does not adopt that old anonymous volume: the first `up` after
this change mounts an empty `kdive-pgdata`, and the old volume is left dangling exactly as
before. Reclaim it with `docker volume prune`.

If a stack is running right now and its database contents matter, capture them before the
upgrade and restore afterwards:

```bash
docker compose exec -T postgres pg_dumpall -U kdive > kdive-predates-named-volumes.sql
just compose-stop            # select the new image and configuration
just compose-up
docker compose exec -T postgres psql -U kdive -d kdive < kdive-predates-named-volumes.sql
```

This applies once. Afterwards a plain `down` genuinely preserves the database.

## Upgrading worker-fence authority

This three-command path is local-bootstrap-only: with `KDIVE_LOCAL_ROLE_BOOTSTRAP=1`, use
`just compose-stop`, select the new image and configuration, then `just compose-up`. It records
old-worker termination and preserves named volumes; the Compose graph runs the migrate one-shot and
local role bootstrap before the operator-side lifecycle wrapper registers the current worker. That
bootstrap resets fixed local development passwords and restores the intended runtime-role
memberships.

`KDIVE_LOCAL_ROLE_BOOTSTRAP=0` disables local mutation. An externally provisioned Compose-derived
deployment must supply an equivalent stop-old, migrate, provision credentials and memberships, and
start gate outside this reference workflow. Verify that every current worker has registered its
incarnation and that the server lists the recovery tools before resuming queue processing.
Do not roll an old worker image back into this sequence. Rollback cannot restore its ability
to claim protocol-required jobs; recover forward with a current image. Do not invoke
`python -m kdive.processes.lifecycle.compose_worker_lifecycle` directly or use raw Docker/Compose
commands;
they bypass the public lifecycle path and retain pins rather than releasing them.

The Compose-managed bucket supplies the ADR-0524 store contract. When replacing it with an
external store, follow the stop-old-first adoption order and IAM requirements in
[Installing KDIVE](install.md): quiesce old processes, grant and verify
`s3:GetObjectVersion`/`s3:GetBucketVersioning`/`s3:ListBucketVersions`/
`s3:DeleteObjectVersion`, verify bucket policy,
enable versioning without exclusions or MFA Delete, wait for activation, migrate, and start only
the version-aware image. Suspension and live rollback to a pre-ADR-0524 image are unsupported.

That graph orders the first bring-up only. Each app process also waits up to ten seconds at
start for its own first database connection and exits if Postgres is unreachable, so every
long-running service — the three app processes and `postgres`, `minio`, `oidc` — carries
`restart: on-failure` to cover each *later* recreate: a Postgres image bump, or a bare
`docker compose restart server` while the database is down. Without it the container would
sit in `Exited (1)`. With it, the container retries until the database answers;
`docker compose ps` shows it restarting and `docker compose logs server` names the cause.

The backends carry the policy for the same reason the app services do — policing only the
app tier leaves them restarting against something that stayed stopped. The `migrate` and
`minio-init` one-shots are deliberately unpoliced: they are meant to run once and exit.

`on-failure` matches the systemd units' `Restart=on-failure` and, unlike `unless-stopped`,
does not start containers when the Docker daemon does. That is deliberate for a stack whose
MinIO uses root demo credentials and whose mock OIDC issuer mints accepted bearer tokens,
both on published host ports: they should not come back on every reboot of a machine that
once ran the stack. After a reboot, bring the stack up again explicitly with
`just compose-up`.

## Pointing an agent at the endpoint

The server publishes the MCP endpoint over streamable HTTP. Point an agent at
`http://localhost:8000/mcp` (or the host/port you mapped) and supply a bearer token your
OIDC issuer accepts. The agent's MCP client config names the server and its URL; consult
your client's documentation for the exact `mcpServers` shape.

The `Authorization` header value must include the `Bearer ` scheme prefix —
`Authorization: Bearer <token>`, not a bare `<token>` (RFC 6750). A bare token is
rejected with a 401 that names the missing prefix.

## Using this stack as a test override backend

The test suite can reuse this Compose Postgres/MinIO instead of starting its own
per-run containers, by pointing the fixtures at it (ADR-0401):

```
export KDIVE_TEST_PG_URL=postgresql://kdive:kdive@localhost:5432/kdive  # pragma: allowlist secret
export KDIVE_TEST_S3_URL=http://localhost:9000   # creds default to minioadmin/minioadmin
```

Each test run then creates per-run, per-worker `kdive_test_<worker>_<token>` databases
and `kdive-test-<worker>-<token>` buckets on this shared backend. The Postgres service
is started with `max_connections=500` so ~18 xdist workers do not exhaust it.

**Required cleanup:** a run that crashes leaves its `kdive_test_*` databases and
`kdive-test-*` buckets behind (the uuid names never recur, so they are not reclaimed by
reuse). Periodically drop them, or recreate the Compose volume:

```
psql "$KDIVE_TEST_PG_URL" -tAc \
  "SELECT datname FROM pg_database WHERE datname LIKE 'kdive_test_%'" \
  | xargs -r -I{} psql "$KDIVE_TEST_PG_URL" -c 'DROP DATABASE IF EXISTS "{}" WITH (FORCE)'
```

The default `just test` run (no override) starts one throwaway container per run and
needs none of this.
