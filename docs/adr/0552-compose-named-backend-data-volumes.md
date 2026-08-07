# 0552 — Name every reference Compose data volume

## Status

Accepted (2026-08-06)

## Context

`docker-compose.yml` mounts nothing at `postgres`'s `/var/lib/postgresql/data`, gives `minio`
no `volumes:` key at all even though it runs `server /data`, and mounts only a read-only config
into `prometheus`. All three images declare a `VOLUME` for those paths (`postgres:17` →
`/var/lib/postgresql/data`, `minio` → `/data`, `prom/prometheus:v3.12.0` → `/prometheus`;
`grafana/grafana:13.0.3` declares none), so Compose allocates an **anonymous** volume per
service per `up`.

`docker compose down` removes anonymous volumes only when given `--volumes`. The plain teardown
leaves each one behind with no name and nothing referencing it, and the next `up` allocates a
fresh set. The data is neither kept nor removed — it is orphaned, and the stack silently
restarts from an empty database, an empty bucket, and an empty TSDB.

Four committed statements promise the opposite: the `just compose-stop` recipe comment
(`justfile:293`), `scripts/live-stack/down.sh`'s header, the `compose-stop` / `compose-down`
distinction in `docs/operating/docker-compose.md` and `deploy/compose/README.md`, and
`tests/guards/test_install_topology_contract.py`, which guards that wording. A fifth —
"TSDB is ephemeral container-local (no named volume) … a `docker compose down` drops history",
in the compose file and repeated in `deploy/compose/README.md` — is wrong in the other
direction: `down` orphans the TSDB rather than dropping it.

[ADR-0533](0533-role-separated-worker-fence-evidence.md) requires a stop-old-first upgrade, and
the accepted non-destructive stop design routes it through `just compose-stop` → select the new
image → `just compose-up`. Because the Postgres volume is anonymous, an operator following that
documented upgrade loses the database. ADR-0533 itself never mentions volumes: it fixes the
upgrade *order*, not the volume topology that makes the order survivable.

## Decision

Mount every image-declared `VOLUME` explicitly, so Compose never allocates an anonymous one.
State that is meant to survive gets a project-declared named volume;
state that is meant to be ephemeral gets tmpfs.

`postgres` and `minio` are meant to survive: `kdive-pgdata:/var/lib/postgresql/data` and
`kdive-minio-data:/data`, declared in the top-level `volumes:` block beside the existing
`kdive-build` and `kdive-install`. Postgres keeps its read-only bootstrap-SQL bind mount
alongside the new volume. A plain `docker compose down` and `just compose-stop` keep them;
`docker compose down --volumes`, `just compose-down`, and `scripts/live-stack/down.sh --wipe`
drop them, and are the only things that do.

`prometheus` is meant to be ephemeral:
`tmpfs: ["/prometheus:mode=0700,uid=65534,gid=65534,size=256m"]`.
[ADR-0189](0189-bundled-prometheus-metrics-collection.md) decided the bundled TSDB is a
throwaway demo store — `emptyDir` in the chart, short retention, "a Prometheus pod restart
drops history" — and rejected PVC-backed storage for exactly that reason. tmpfs keeps that
lifecycle, so this makes the existing "ephemeral container-local" claim true instead of
reversing the decision. It is not identical to the chart's `emptyDir`, which is node-disk
backed: tmpfs moves the TSDB to RAM, which the `size=` cap bounds.

Every tmpfs option is load-bearing. The image runs as uid/gid 65534 and a default-mode tmpfs
is root-owned `0775`, which makes Prometheus panic on its first write, so mode and ownership
must be set. `0700` rather than a world-writable `1777` keeps a second uid inside the
container from planting TSDB files. Without `size=` a tmpfs defaults to half of host RAM
(measured: 125 GB here); 256 MB holds far more than 6h of three scrape targets.

`grafana/grafana:13.0.3` declares no `VOLUME`, so it needs neither.

**Existing installs are not migrated.** An install running today holds its data in an anonymous
volume this change does not adopt, so the next `up` mounts an empty `kdive-pgdata` and the old
volume is left dangling exactly as every prior plain `down` already left one. That is accepted
rather than automated: the reference stack is local-development-only, with fixed credential
literals in the compose file, and any operator who has run a plain `down` even once has already
lost that volume's contents. `docs/operating/docker-compose.md` gains a one-time note for anyone
who does need continuity — copy the bytes from the old volume into `kdive-pgdata` before the
first new `up`, while the old container still names it.

## Consequences

Database and artifact state accumulates across teardowns where it previously vanished, and
four consequences follow from that.

A shared local backend used as the test override (`KDIVE_TEST_PG_URL`) keeps crashed runs'
`kdive_test_*` databases and `kdive-test-*` buckets until an operator drops them: the periodic
cleanup `docs/operating/docker-compose.md` already prescribes stops being advisory and becomes
the reclaim path. A plain `scripts/live-stack/down.sh` followed by `up.sh` likewise now leaves
the live-stack database populated across cycles.

`deploy/compose/bootstrap-migration-owner.sql` runs from `/docker-entrypoint-initdb.d`, which
Postgres executes only when the data directory is empty. That was every `up` after a `down`; it
becomes once per `kdive-pgdata` lifetime. A later edit to that file, or to `POSTGRES_USER` /
`POSTGRES_PASSWORD` / `POSTGRES_DB`, silently does not reach an existing stack until
`just compose-down`. A `postgres:17` → `18` image bump now fails at startup on a retained
volume rather than starting on a fresh one.

`tests/image/test_compose_smoke.py` drives the committed compose file under the fixed project
name `kdive-smoke`, and its `--volumes` teardown lives in a `finally` an interrupted run does
not reach. Its volumes are now stable across runs, so it must also tear down *before* it starts
— otherwise one killed run leaves the next one testing migrations against an already-migrated
database. `tests/compose/test_compose_worker_lifecycle_live.py` drives its own fixture compose
file, not this one, and is unaffected.

`examples/local-libvirt/down.sh` tells the operator twice that `docker compose down -v` removes
the backends. That command is still correct and still destructive, but it is no longer the only
teardown worth naming, so both places name the preserving and the dropping form.

The Prometheus TSDB moves from disk to RAM, capped at 256 MB by the mount. At the 6h retention
this file already sets, three scrape targets at 15s produce a few megabytes.

Retained data is also retained exposure. This stack ships fixed credential literals
(`kdive:kdive`, `minioadmin:minioadmin`) on published host ports, and the artifacts bucket
holds unredacted `sensitive` artifacts. Before this change a plain `down` emptied that store;
now a machine that ran the demo once keeps every prior investigation until someone runs a
destructive teardown. The reference stack was already documented as local-development-only,
and binding its backend ports to loopback is tracked separately (#1918) rather than folded in
here.

## Considered & rejected

- **Do nothing, and reword the docs to say a plain `down` wipes.** Cheaper, but it removes the
  non-destructive stop ADR-0533's upgrade depends on and would mean withdrawing
  `just compose-stop`. It resolves the contradiction by lowering the promise to match a defect.
- **Make the non-destructive stop run `docker compose stop` instead of `down`.** A stopped
  container keeps its anonymous volume, and a later `up -d` reattaches it, so this preserves
  data with no compose-file change. It is rejected because it preserves state only while the
  containers survive — any real `down`, `--remove-orphans`, or `docker system prune` still
  discards it — and it leaves the per-`up` volume leak this issue reports untouched. It would
  also require changing `ComposeWorkerLifecycle`, which the charter excludes.
- **Give the Prometheus TSDB a named volume too, for one uniform rule.** Rejected on two
  counts. It reverses ADR-0189's ephemeral-demo-store decision without a reason that decision
  did not already consider. And it is unreachable by the destructive path: `just compose-down`
  runs `docker compose down --volumes --remove-orphans` with no `--profile obs`
  (`ComposeWorkerLifecycle.down`), which — verified against this compose file — leaves the
  running profile-gated `prometheus` container up, leaves `<project>_kdive-prom-data`
  unremoved, and fails the network delete while still exiting 0. The result would be a named
  volume that only `scripts/live-stack/down.sh --wipe` can drop: worse than the anonymous
  volume it replaced. The repo already carries `_remove_managed_worker_volumes` as a
  by-name workaround for this same class, and changing that module is outside this charter.
- **Bind-mount host directories instead of named volumes.** Host binds put the data outside
  Compose's lifecycle, so `down --volumes` and `--wipe` could no longer drop it, and they
  inherit the host's uid/gid and SELinux labelling — the class of breakage this repo already
  carries for the staged libvirt rootfs path.
- **Adopt the existing anonymous volume automatically on first `up`.** Once the old volume is
  unreferenced, nothing identifies it: Compose does not label daemon-created anonymous volumes
  with a project or service, so a one-shot could not tell this install's from another's.
  Picking wrong writes another install's bytes into this database; picking nothing does less
  than it claims. While the old container still exists the volume *is* identifiable, by
  `docker inspect` on its mounts — so the operating guide documents a manual adoption for that
  case rather than automating either one.
