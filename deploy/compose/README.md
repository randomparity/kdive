# Running KDIVE with Docker Compose

The repo-root [`docker-compose.yml`](../../docker-compose.yml) brings up the three kdive
processes (`server` / `worker` / `reconciler`) plus a `migrate` one-shot, on top of the
existing dev backends (Postgres, SeaweedFS, mock OIDC). Everything is wired purely through
`KDIVE_*` (see the [config reference](../../docs/guide/reference/config.md)); the shared
backend env is declared once as the `x-backends` anchor and merged into each service.

This is a dev/demo deployment with fixed local credentials. The default app image is built
from the repo [Dockerfile](../../Dockerfile) (`kdive:dev`). Before first use, complete the
[source setup](../../docs/operating/install.md#from-source); the lifecycle recipes need the
checkout's Python environment as well as Docker Compose.

## Capture publication protocol 4

Start this release with a new empty Postgres database and a new versioned object-store bucket
or namespace. Migration 0113 refuses existing worker, job, capture-operation, or artifact rows.
KDIVE has no migration, cutover, rollback, preservation, inspection, or cleanup path for
protocol-3 state or objects. Do not copy an old anonymous-volume database into this deployment.
Use a separate Compose project for a fresh stack, with non-conflicting published ports; retain
old state separately if it is needed.

Before readiness and job claims, each worker probes the store with concurrent conditional
creates, verifies one winner and one precondition failure, deletes the exact probe version,
and confirms absence. Correct store versioning or conditional-create behavior if the probe
fails; use `just compose-recreate-worker` to retry after correcting the cause.

## Bring-up

The dependency graph is self-contained, so a single `up` brings the whole stack. The checked-in
passwords below are allowlisted for local development only; never reuse them in a
production deployment. Postgres, SeaweedFS, and the mock OIDC issuer bind `127.0.0.1` by default
(ADR-0554), so they are reachable on `localhost` only. The backend port variables accept an
`ADDR:PORT` host mapping. Exposing the fixed-credential backends is an explicit operator choice. If `KDIVE_POSTGRES_PORT` includes an address, also set
`KDIVE_LIFECYCLE_WITNESS_DATABASE_URL` to a valid witness DSN with a reachable host and numeric
port: the recipe's default DSN interpolates the port variable and cannot parse `ADDR:PORT` there.
The MCP host port is selected separately by `KDIVE_HTTP_PORT` and is not loopback-only by default.

```bash
just compose-up   # builds the image, runs the backends + migrate, then gates the worker
```

The four supported worker lifecycle recipes are `just compose-up`, `just compose-stop`,
`just compose-recreate-worker`, and `just compose-down`. `just compose-stop` preserves named
volumes after recording worker termination; `just compose-down` removes named volumes for a
destructive teardown. Those volumes are `kdive-pgdata` (the database), `kdive-seaweedfs-data`
(the artifacts bucket), and `kdive-build` / `kdive-install` — Docker prefixes each with the
Compose project name, so `docker volume ls` shows them as `<project>_kdive-pgdata` and so on.
The operator-side lifecycle wrapper binds the exact full container ID to a
random nonce in Postgres before start and records retained terminal inspect evidence before removal.

### SeaweedFS data-volume transition

`kdive-minio-data` is incompatible input for SeaweedFS. Stop the old stack and retain that volume
unchanged as a rollback artifact, then start the new stack with an empty `kdive-seaweedfs-data`
volume. Do not mount, reuse, or convert the old volume; export/import needs a separately approved
operator procedure.
Compose does not run a persistent lifecycle-witness service. Raw Compose/Docker lifecycle commands
and host-launched workers bypass that chain and are unsupported. On a database failure, the wrapper
leaves the never-started or terminal worker retained; restore Postgres and retry.
On a clean local database, Postgres creates the separate `kdive-migration` owner first. Migrations
create the four NOLOGIN capabilities, then the `role-bootstrap` one-shot creates distinct
`kdive-server-member`, `kdive-worker-member`, `kdive-reconciler-member`, and
`kdive-witness-member` logins, resets their fixed local development passwords, removes every wrong
capability membership, and restores each intended runtime-role membership before a runtime process
starts. The migration owner is absent from every runtime container. Production Compose-derived and
Helm deployments retain the external-provisioning contract: operators provide secret-backed login
members and do not run this explicitly local bootstrap. Set `KDIVE_LOCAL_ROLE_BOOTSTRAP=0` and
supply the migration, server, worker, reconciler, and lifecycle-witness DSNs to use that external
path; the bootstrap one-shot then performs no database mutation.

The server, worker, and reconciler capabilities have ordinary application-table access. The
lifecycle witness has none. Protected worker-incarnation and investigation-build-use mutation remains
security-definer-function-only. Server build-use diagnostics are capped and filtered to caller projects
with at least viewer by their dedicated function;
the reconciler has column-level read access only to the use table's exact investigation/generation pin
key for GC. New migrations must grant process-role access explicitly for each new relation.

The migration owner and lifecycle witness DSNs are never present in the worker container; its random
256-bit incarnation credential is copied from a supervisor-owned file into the never-started container
as UID 10001 with mode 0400 and is not placed in its environment or a shared mount.

The worker's internal claim and heartbeat lease is a PostgreSQL interval applied once to the
database's `clock_timestamp()` captured after the blocking incarnation lock and, for heartbeat, the
exact running-attempt row lock. Its computed deadline must be after that post-lock reference and no
more than one hour later; this elapsed bound includes calendar and time-zone effects. An out-of-range
deadline raises SQLSTATE `22023` before job state or attempt data changes. Retry the same operation
with an interval whose computed deadline is valid; the reference worker uses five minutes. Each
heartbeat begins another bounded lease, so the ceiling is not a total job-runtime limit.

## Upgrading worker-fence authority

This sequence applies only when the target release supports an upgrade. It does not bypass
[protocol 4's fresh-resource requirement](#capture-publication-protocol-4).

This three-command path is local-bootstrap-only: with `KDIVE_LOCAL_ROLE_BOOTSTRAP=1`, use
`just compose-stop`, select the new image and configuration, then `just compose-up`. It records
old-worker termination and preserves named volumes; the Compose graph runs the migrate one-shot and
local role bootstrap before the operator-side lifecycle wrapper registers the current worker. That
bootstrap resets fixed local development passwords and restores the intended runtime-role
memberships.

`KDIVE_LOCAL_ROLE_BOOTSTRAP=0` disables local mutation. An externally provisioned Compose-derived
deployment must supply an equivalent stop-old, migrate, provision credentials and memberships, and
start gate outside this reference workflow. Verify registered current incarnations and the server's
recovery-tool exposure before resuming queue processing. An image rollback cannot restore old
claiming after the protocol migration, so recover forward with a current worker image. Do not invoke
`python -m kdive.processes.lifecycle.compose.compose_worker_lifecycle` directly or use raw
Docker/Compose commands;
they bypass the public lifecycle path and retain pins.

## Startup ordering and recovery

The lifecycle wrapper resolves the graph rather than relying on the operator to order it:
the app services pull in a healthy Postgres, the `seaweedfs-init` bucket-creation one-shot
(which itself waits for healthy SeaweedFS), the OIDC issuer, and the `migrate` one-shot. They
declare `depends_on: migrate` with `condition: service_completed_successfully`, so they
never reach the database before the schema is rolled forward (ADR-0088 decision 4); a
non-zero `migrate` exit blocks app start. The bucket-creation one-shot completes before any
app process starts, so the worker's first artifact write never races a missing bucket.

The bucket initializer enables bucket-wide versioning and requires S3 to report `Enabled`. An
external replacement needs versioning and runtime permissions, including `s3:GetObjectVersion`; follow the
[object-store preflight](../../docs/operating/install.md#object-store-preflight).

The three app processes acquire their first database connection with a ten-second timeout after
startup initialization. If acquisition fails, read the error and preceding `psycopg.pool` warning
for the database, credential, or network cause. App services and backends use `restart: on-failure`
to retry failures; `migrate`, `role-bootstrap`, and `seaweedfs-init` are one-shots. After a Docker daemon
restart, explicitly run `just compose-up`; `on-failure` does not bring the stack back on reboot.

The `migrate` service defines the shared app image build; `KDIVE_IMAGE` selects an alternative
image reference. To pre-build the default image:

```bash
docker build -t kdive:dev .
```

## Verify

```bash
docker inspect "$(docker compose ps -q migrate)" --format '{{.State.ExitCode}}'  # 0
docker compose logs migrate                                                       # "applied N migration(s)"
docker compose ps server worker reconciler                                        # all running
curl -i http://localhost:8000/mcp                                                 # server accepts (HTTP 401 unauthenticated)
```

`migrate` exits 0, the three processes stay up, and the server accepts connections. An
unauthenticated probe returns `401` — that is the server's auth layer responding.

### Health probes (ADR-0090 §5)

Each app process runs the aux health/metrics listener and compose health-checks it on its
own `/readyz`. The listener binds `0.0.0.0:<port>` *inside* the container (`server` 9464,
`worker` 9465, `reconciler` 9466) via `KDIVE_HEALTH_BIND_ADDR`, set per service. The port
is **never published to the host** — the container network namespace is its only access
boundary, so the unauthenticated `/readyz`/`/metrics` stay non-public. A backend going down
flips the container to `unhealthy`:

```bash
docker compose ps                          # STATUS shows (healthy)/(unhealthy) per process
# Inspect the aux endpoints from inside a container (the port is not on the host):
docker compose exec server python -c \
  'import urllib.request;print(urllib.request.urlopen("http://127.0.0.1:9464/readyz").read())'
```

### Metrics collection (opt-in — ADR-0189)

Those `/metrics` are produced and discarded unless something scrapes them. An opt-in Prometheus
behind the `obs` compose profile (so the turnkey `up` graph is unchanged) collects all three on
the compose network:

```bash
docker compose --profile obs up -d prometheus
# open http://localhost:9090/targets — server/worker/reconciler should be UP
# then query e.g. kdive_job_queue_depth to confirm kdive_* series are present
```

It scrapes `server:9464` / `worker:9465` / `reconciler:9466` over the compose network (those
aux ports stay unpublished — only the `9090` UI is published to the host) using the static
config in [`prometheus.yml`](prometheus.yml). TSDB is a container-local tmpfs (ADR-0189
keeps the demo store ephemeral; ADR-0552 made that true rather than orphaning an anonymous
volume on every `up`), so stopping the container drops the history.

## Driving an authenticated request

Connect the client to `http://localhost:8000/mcp` (adjust the mapped host/port), and send
`Authorization: Bearer <token>`. A bare token without the `Bearer ` prefix is rejected.

The mock OIDC issuer derives a token's `iss` claim from the URL it is minted through. The
in-network server validates against `KDIVE_OIDC_ISSUER=http://oidc:8080/default` (the
issuer's address *inside* the compose network), so a token minted from the host via the
published `http://localhost:8090/default` carries `iss=http://localhost:8090/default` and is
rejected. To exercise an authenticated call against the compose server, mint the token from
*inside* the network (a one-off container joined to the compose network, hitting
`http://oidc:8080`), so its `iss` matches what the server expects.

The token flow itself (authorize → code → exchange) is the same one the live-stack drivers
use — see [`src/kdive/mcp/dev_harness.py`](../../src/kdive/mcp/dev_harness.py),
[`tests/integration/live_stack/spine.py`](../../tests/integration/live_stack/spine.py), and the
[live-stack runbook](../../docs/operating/runbooks/live-stack.md), which runs the server *on the
host* (where `iss=http://localhost:8090/default` matches host-minted tokens).

## Teardown

```bash
just compose-down   # records worker termination, then removes named volumes
```

## Image provenance — verify before you run a published image

The default image is built locally. Before using a published image, follow the
[release-image verification instructions](../../docs/development/releasing.md#container-image-publishing).
They own signature identity, image tags, SBOM, and provenance checks. Git release tags use
`vX.Y.Z`; the corresponding container tag is `X.Y.Z`.

## Using the stack as a test backend

Tests can use the Compose Postgres and SeaweedFS through explicit overrides:

```bash
export KDIVE_TEST_PG_URL=postgresql://kdive:kdive@localhost:5432/kdive  # pragma: allowlist secret
export KDIVE_TEST_S3_URL=http://localhost:8333
```

Those are the checked-in local admin credentials. For another server, provide a dedicated test
admin DSN that can create/drop test databases and run their migrations. S3 credentials default
to local `kdive` / `kdive-demo-secret`; override `KDIVE_TEST_S3_ACCESS_KEY` and
`KDIVE_TEST_S3_SECRET_KEY` when needed.
Adjust endpoints to the published ports. Never point test overrides at a production backend.

Each worker gets a unique `kdive_test_<worker>_<token>` database and
`kdive-test-<worker>-<token>` bucket. A crashed run can leave both on this persistent backend.
Once no test runs use it, remove only the identified abandoned test databases and buckets, or
use `just compose-down` if the entire Compose project's state is disposable. Do not apply a
wildcard force-drop while other runs may own matching names. Named volumes preserve leftovers
across `just compose-stop`; that command does not reclaim them.

Without overrides, the fixtures share one disposable Postgres and one SeaweedFS container per run.
They stop on normal teardown; a later run can reap a killed run's containers using their liveness
locks. The [fixture coordination source](../../tests/support/xdist_backend.py) owns those rules.

## Host-process local stack

For host processes against Compose backends, use the
[live-stack runbook](../../docs/operating/runbooks/live-stack.md). It owns the lifecycle scripts,
worker accounts, migrations, diagnostics, and reset behavior for that deployment. Keep one app
tier active against a given database; do not mix it with this container app tier.
