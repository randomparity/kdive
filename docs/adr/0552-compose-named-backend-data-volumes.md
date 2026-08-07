# 0552 — Name the reference Compose Postgres and MinIO data volumes

## Status

Accepted (2026-08-06)

## Context

`docker-compose.yml` mounts nothing at `postgres`'s `/var/lib/postgresql/data`, and gives
`minio` no `volumes:` key at all even though it runs `server /data`. Both images declare a
`VOLUME` for those paths, so Compose allocates an **anonymous** volume for each service on
every `up`.

`docker compose down` removes anonymous volumes only when given `--volumes`. The plain
teardown therefore leaves each one behind with no name and no service still referencing it,
and the next `up` allocates a fresh pair. The data is neither kept nor removed — it is
orphaned, and the stack silently restarts from an empty database and an empty bucket.

Four places already promise the opposite behavior:

- `just compose-stop` is the supported non-destructive stop, and its recipe comment says it
  preserves named volumes for an upgrade;
- `scripts/live-stack/down.sh`'s header says plain teardown "keeps state (Postgres volume …)"
  and reserves the drop for `--wipe`;
- `docs/operating/docker-compose.md` and `deploy/compose/README.md` distinguish
  `just compose-stop`, which "preserves named volumes", from `just compose-down`, which
  "removes named volumes";
- `tests/guards/test_install_topology_contract.py` guards that documented distinction.

[ADR-0533](0533-role-separated-worker-fence-evidence.md) requires a stop-old-first upgrade,
and the accepted non-destructive stop design routes it through `just compose-stop` →
select the new image → `just compose-up`. Because the Postgres volume is anonymous, an
operator who follows that documented sequence loses the database. The prose, the guard, and
the upgrade procedure all describe a stack whose compose file does not exist.

Note that ADR-0533 itself never mentions volumes. It requires the upgrade *order*; it does
not decide the volume topology that makes the order survivable. That decision is this record.

## Decision

Declare two project volumes alongside the existing `kdive-build` and `kdive-install`, and
mount them:

```yaml
  postgres:
    volumes:
      - kdive-pgdata:/var/lib/postgresql/data
      - ./deploy/compose/bootstrap-migration-owner.sql:/docker-entrypoint-initdb.d/010-migration-owner.sql:ro
  minio:
    volumes:
      - kdive-minio-data:/data

volumes:
  kdive-pgdata:
  kdive-minio-data:
```

State survives a plain `docker compose down` and `just compose-stop`. It is dropped by
`docker compose down --volumes`, `just compose-down`, and `scripts/live-stack/down.sh --wipe`
— which become the only ways to drop it, deliberately named and already documented as
destructive.

**Existing installs are not migrated.** An install running today holds its data in an
anonymous volume that this change does not adopt: after the upgrade, the next `up` mounts an
empty `kdive-pgdata` and the old volume is left dangling exactly as every prior plain `down`
already left one. This is accepted rather than automated. The reference stack is
local-development-only — its Postgres and MinIO credentials are fixed literals in the compose
file — and any operator who has run a plain `down` even once has already lost the contents of
that anonymous volume. An operator who does need continuity across this one upgrade takes a
`pg_dump` before it and restores after; the operating guide records that step, and it is a
one-time note rather than a supported migration path.

## Consequences

Data now accumulates across teardowns where it previously vanished. A shared local backend
used as the test override (`KDIVE_TEST_PG_URL`) keeps its crashed runs' `kdive_test_*`
databases and `kdive-test-*` buckets until an operator drops them — the periodic cleanup that
`docs/operating/docker-compose.md` already prescribes stops being advisory and becomes the
actual reclaim path.

A plain `down` no longer adds a dangling anonymous volume per `up`, so a developer who cycles
the stack stops accruing unreferenced volumes on disk.

The compose smoke and the compose lifecycle proof already tear down with `--volumes`, so they
keep starting from an empty backend; naming the volumes does not leak state between their runs.
`prometheus` and `grafana` keep container-local ephemeral storage: their demo TSDB posture is
settled and unchanged.

`examples/local-libvirt/down.sh` told the operator to run raw `docker compose down -v`. Now
that a plain `down` preserves state, the destructive command must be named as such, so that
message points at `just compose-down` — the recipe that records worker termination first.

## Considered & rejected

- **Do nothing, and reword the docs to say a plain `down` wipes.** This is the cheaper edit,
  but it removes the non-destructive stop that ADR-0533's stop-old-first upgrade depends on
  and would require withdrawing `just compose-stop`. It resolves the contradiction by
  lowering the promise to match a defect.
- **Bind-mount host directories instead of named volumes.** Host bind mounts put the data
  outside Compose's own lifecycle, so `down --volumes` and `--wipe` could no longer drop it,
  and they inherit the host's uid/gid and SELinux labelling — which is exactly the class of
  breakage this repo already carries for the staged libvirt rootfs path.
- **Adopt the existing anonymous volume automatically on first `up`.** This needs a one-shot
  that inspects unreferenced volumes and copies from one it cannot positively identify as
  this project's. Picking wrong writes another install's bytes into this database; picking
  nothing silently does less than it claims. A documented `pg_dump` is honest and bounded.
- **Name only the Postgres volume.** MinIO leaks an anonymous volume on the same terms and
  holds the artifacts bucket the app tier writes to. Splitting the posture across the two
  backends leaves half the reported defect open and gives an operator two teardown rules to
  remember instead of one.
- **Add `--volumes` to every teardown path instead.** This also makes the file and the prose
  agree, by deleting the state deliberately rather than orphaning it. It forecloses the
  upgrade sequence entirely and makes the reference stack unable to hold anything across a
  restart.
