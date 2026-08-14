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
artifacts bucket), and `kdive-build` / `kdive-install` — Docker prefixes each with the
Compose project name, so `docker volume ls` shows them as `<project>_kdive-pgdata` and so on.
`docker compose down --volumes` and `scripts/live-stack/down.sh --wipe` drop them too. Those
are the only supported paths that do. These recipes preserve exact worker-incarnation evidence in Postgres. Raw
Compose/Docker lifecycle commands and host workers bypass that evidence boundary. A database failure
is fail-closed, retaining the never-started or terminal worker so the same recipe can be retried
after Postgres recovers.

Configuration is read from `KDIVE_*` variables; see
[the config reference](../guide/reference/config.md) for every setting.

## Backend port binding

Postgres, MinIO, and the mock OIDC issuer all carry fixed credential literals in the
repository and are published to the host for local access only (ADR-0554). Their port
mappings bind `127.0.0.1` by default, so they are reachable on `localhost` only and are not
exposed on other network interfaces. The `KDIVE_*_PORT` override variables accept a full
`ADDR:PORT` left side, so remote access is an explicit opt-in:

```bash
KDIVE_POSTGRES_PORT=0.0.0.0:5432 just compose-up
```

The MCP server port (8000) is not bound to loopback because agents may connect from another
machine. Its port is separately overridable via `KDIVE_HTTP_PORT`.

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
before.

If your stack is **not** running, or its contents do not matter, there is nothing to do.

If it **is** running and the database matters, copy the bytes across while the container still
names the old volume. Do this before bringing up the new configuration, so the copy lands in an
empty volume rather than on top of a migrated one:

```bash
# 1. While the container still exists, read the volume it is attached to and the Compose
#    project name (which is what Docker prefixes volume names with).
cid=$(docker compose ps -q postgres)
old=$(echo "$cid" | xargs -r docker inspect -f \
  '{{range .Mounts}}{{if eq .Destination "/var/lib/postgresql/data"}}{{.Name}}{{end}}{{end}}')
project=$(echo "$cid" | xargs -r docker inspect -f \
  '{{index .Config.Labels "com.docker.compose.project"}}')
echo "old=$old project=$project"
# `old` is a 64-hex name. If it is empty, there is nothing to migrate: either the stack is
# not running, or it is already on the named volume. Stop here in that case.

# 2. Stop the stack, then check out the revision that names the volumes.
just compose-stop

# 3. Create the new volume through Compose, so it carries the labels Compose expects
#    (`docker volume create` makes an unlabelled one, and the next `up` then warns and
#    suggests `external: true` — which would stop `just compose-down` ever removing it).
docker compose create postgres
docker run --rm -v "$old":/from:ro -v "${project}_kdive-pgdata":/to \
  postgres:17 sh -c 'cp -a /from/. /to/'

# 4. Bring the new configuration up. `migrate` rolls the copied database forward.
just compose-up

# 5. Once you have confirmed the data is there, drop the old volume by name.
docker volume rm "$old"
```

Do **not** use `docker volume prune` for step 5: it is host-wide and removes every unused
volume on the machine, including other projects' detached data.

The MinIO bucket can be moved the same way (`kdive-minio-data` at `/data`), or left behind —
in this reference stack its artifacts are reproducible.

This applies once. Afterwards a plain `down` genuinely preserves both.

## Upgrading worker-fence authority

This three-command path is local-bootstrap-only: with `KDIVE_LOCAL_ROLE_BOOTSTRAP=1`, use
`just compose-stop`, select the new image and configuration, then `just compose-up`. It records
old-worker termination and preserves named volumes; the Compose graph runs the migrate one-shot and
local role bootstrap before the operator-side lifecycle wrapper registers the current worker. That
bootstrap resets fixed local development passwords and restores the intended runtime-role
memberships.

### Protocol-3 capture cutover

Migration 0112 is an unconditional replacing cutover, not a rolling worker-fence upgrade. Supply
both a new custom-format backup path and the exact target image:

```bash
export TARGET_IMAGE='ghcr.io/randomparity/kdive:<target-tag>'
scripts/cutover-capture-protocol-compose.sh \
  /var/backups/kdive-before-protocol-3.dump \
  "$TARGET_IMAGE"
```

The script runs `just compose-stop`, restarts only Postgres, requires the exact
`compose_worker_lifecycle` termination rows, runs `pg_dump --format=custom`, executes the target
image's one-shot `migrate` service, and runs `just compose-up` with that same image. It does not
use `compose-down`, `--volumes`, or raw worker lifecycle commands, so the named database, artifact,
build, and install volumes remain attached to the Compose project. A fresh database with no legacy
workers is accepted and receives protocol 3 directly.

Before stopping anything, it resolves the target image to a local immutable image ID and freezes
the exact rendered Compose model, project name, and resolved operator environment in a restricted
mode-0700 cutover directory beside the backup. Every later stop, migration, and start consumes
only that frozen project and model; the lifecycle supervisor's new per-start nonce remains the
only runtime substitution. That directory also holds a mode-0400 password-free database URI and
libpq passfile. Host `psql` and `pg_dump` children receive only that URI and passfile path; the
migration-owner DSN is removed from their argv and environment. The wrapper queries the database
through both the host authority and the
frozen migration service and compares database name, database OID, and server system identifier.
It repeats that positive same-database witness after stop and before backup and migration, then
requires both post-stop observations to equal the approved preflight identity. A backend, DNS, or
proxy switch across the stop boundary therefore aborts without a backup or migration.

Each Docker, database, dump, and migration operation has a
600-second whole-operation limit measured by GNU `timeout`'s monotonic clock. Database connects
also have a 10-second limit and each statement a 300-second server-side limit. Override these
whole-second values with `KDIVE_CUTOVER_OPERATION_TIMEOUT_SECONDS`,
`KDIVE_CUTOVER_DB_CONNECT_TIMEOUT_SECONDS`, and
`KDIVE_CUTOVER_DB_STATEMENT_TIMEOUT_SECONDS`. A limit applies to one external operation; expiry
rejects its incomplete result and runs the stopped-state recovery proof after mutation. Correct
the stalled Docker or database dependency and rerun the exact command printed by the script.

A precondition or migration failure leaves workers stopped and the old schema authoritative. A
post-migration failure leaves protocol 3 installed and workers stopped. Never restart a protocol-2
image against that database. The only post-migration rollback is the exact
`pg_restore --clean --if-exists` command printed by the script, followed by the prior image.
That exact command references the retained password-free URI and restricted passfile, not the
owner DSN. Do not remove the retained cutover directory before rollback completes.
Backup publication is atomic and refuses replacement. If the destination appears while the dump
is running, the validated sibling temporary dump is retained and its exact recovery path is
printed. The restricted input snapshot is recoverably trashed on success when the filesystem
supports it; otherwise its retained path is printed for operator cleanup.
Existing paths and symlinks are rejected, including a symlink that appears during dump validation.
After a migration-command failure, the printed recovery procedure names both the frozen migration
command and the frozen `just compose-up` command needed to complete protocol 3.
Migration 0112 records operation quiescence only; #1952 still gates publication closure and
combined historical-capture coverage.

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
reuse). Since ADR-0552 named the data volumes they also survive a plain `docker compose down`,
so nothing reclaims them on its own any more. Drop them periodically with the query below, or
recreate the volume outright with `just compose-down`:

```
psql "$KDIVE_TEST_PG_URL" -tAc \
  "SELECT datname FROM pg_database WHERE datname LIKE 'kdive_test_%'" \
  | xargs -r -I{} psql "$KDIVE_TEST_PG_URL" -c 'DROP DATABASE IF EXISTS "{}" WITH (FORCE)'
```

The default `just test` run (no override) starts one throwaway container per run and
needs none of this.
