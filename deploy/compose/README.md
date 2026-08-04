# Reference compose — app tier (ADR-0088)

The repo-root [`docker-compose.yml`](../../docker-compose.yml) brings up the three kdive
processes (`server` / `worker` / `reconciler`) plus a `migrate` one-shot, on top of the
existing dev backends (Postgres, MinIO, mock OIDC). Everything is wired purely through
`KDIVE_*` (see the [config reference](../../docs/guide/reference/config.md)); the shared
backend env is declared once as the `x-backends` anchor and merged into each service.

This is a dev/demo reference, not a production deployment. It runs the app tier from the
image built by the repo [`Dockerfile`](../../Dockerfile) (`image: kdive:dev`).

## Bring-up

The dependency graph is self-contained, so a single `up` brings the whole stack. The checked-in
passwords below are allowlisted for local development only; never reuse them in a
production deployment:

```bash
just compose-up   # builds the image, runs the backends + migrate, then gates the worker
```

The four supported worker lifecycle recipes are `just compose-up`, `just compose-stop`,
`just compose-recreate-worker`, and `just compose-down`. `just compose-stop` preserves named
volumes after recording worker termination; `just compose-down` removes named volumes for a
destructive teardown. Their operator-side lifecycle wrapper binds the exact full container ID to a
random nonce in Postgres before start and records retained terminal inspect evidence before removal.
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
`python -m kdive.processes.compose_worker_lifecycle` directly or use raw Docker/Compose commands;
they bypass the public lifecycle path and retain pins.

`docker compose up` resolves the graph rather than relying on the operator to order it:
the app services pull in a healthy Postgres, the `minio-init` bucket-creation one-shot
(which itself waits for a healthy MinIO), the OIDC issuer, and the `migrate` one-shot. They
declare `depends_on: migrate` with `condition: service_completed_successfully`, so they
never reach the database before the schema is rolled forward (ADR-0088 decision 4); a
non-zero `migrate` exit blocks app start. The bucket-creation one-shot completes before any
app process starts, so the worker's first artifact write never races a missing bucket.

The image is built once from the repo `Dockerfile` via the `migrate` service's `build: .`
and reused by the others. Pre-build it explicitly if you prefer:

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
config in [`prometheus.yml`](prometheus.yml). TSDB is ephemeral container-local (no named
volume): a `docker compose down` drops the history, matching the demo posture.

## Driving an authenticated request

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

This reference builds the image locally (`image: kdive:dev`). When you instead pull a
**published** image from `ghcr.io/randomparity/kdive`, verify its signature first. The
[`release-image`](../../.github/workflows/release-image.yml) workflow signs each released
digest keyless/OIDC on a SemVer tag and attaches an SBOM (ADR-0088 decision 8), so a
consumer can confirm the image was built by this repo's release workflow before trusting it.

Install [cosign](https://docs.sigstore.dev/cosign/system_config/installation/), then verify
the tag you intend to run:

```bash
cosign verify ghcr.io/randomparity/kdive:vX.Y.Z \
  --certificate-identity-regexp '^https://github.com/randomparity/kdive/' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

The identity regexp pins the signer to a workflow in this repository and the issuer pins it
to GitHub's OIDC provider; a signature from any other identity or issuer fails the check.

The SBOM and provenance are attached by BuildKit (`docker/build-push-action` `sbom: true`,
`provenance: mode=max`) as in-toto **attestations** referring to the image index — distinct
from the image signature `cosign verify` checks. Inspect them with `buildx imagetools`:

```bash
docker buildx imagetools inspect ghcr.io/randomparity/kdive:vX.Y.Z \
  --format '{{ json .SBOM }}'        # or '{{ json .Provenance }}'
```

## Local lifecycle scripts (this dev host)

For a hand-rolled local stack (host-run server/reconciler/worker against compose backends),
use the lifecycle scripts under `scripts/live-stack/`. They self-elevate with `sudo` for the
root worker and libvirt, so run them via the `!` prefix in the agent or directly in a shell:

- `up.sh` — full bring-up in order: backends → host migrations → libvirt → host processes →
  status. `--skip-obs` omits prometheus/grafana; `--reset-db` runs a full `down.sh --wipe` first
  (drops the Postgres volume AND reaps all `kdive-*` libvirt domains/overlays — live VMs are
  destroyed); recovery from migration drift — see below.
- `down.sh` — stop host processes + compose backends, keeping state. `--wipe` is a full reset:
  drops the Postgres volume and reaps `kdive-*` libvirt domains + their `/var/lib/kdive/rootfs`
  overlays.
- `status.sh` — read-only per-layer health (backends, host daemons + build stamps, server,
  database, libvirt + provision prereqs).

The scripts never start the compose `kdive:dev` app tier (`migrate`/`server`/`worker`/
`reconciler`); the host processes own that tier and `apply-migrations.sh` (current checkout) is
the authoritative migrator.

**Migration drift:** the ADR-0015 immutable-migration guard fires when the persisted DB's
applied-migration history diverges from your checkout (e.g. after switching branches). `up.sh`
aborts at the migrations step with a clear message; recover with `up.sh --reset-db`.

**Grafana:** `up.sh` brings up Grafana (obs profile) at http://localhost:3000 with the
kdive-overview dashboard auto-provisioned against Prometheus. Anonymous access is enabled for
local convenience only.
