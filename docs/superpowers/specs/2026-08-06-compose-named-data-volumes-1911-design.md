# Compose named backend data volumes (#1911)

Decision record: [ADR-0552](../../adr/0552-compose-named-backend-data-volumes.md).

## Scope charter

- **Interaction:** unattended (orchestrator-dispatched background run).
- **Scope identity:** https://github.com/randomparity/kdive/issues/1911 plus annotation token
  `WORK-SCOPE-1911-a3f1c2`.
- **Outcome:** give the reference Compose `postgres` and `minio` services project-declared
  named data volumes so a plain `docker compose down` genuinely preserves their state instead
  of orphaning an anonymous volume, and so the destructive teardown paths remain the only way
  to drop it.
- **Completion criteria:**
  1. `docker compose config` shows `postgres` mounting a project-declared named volume at
     `/var/lib/postgresql/data` and `minio` one at `/data`, both declared in the top-level
     `volumes:` block.
  2. Write a row → `docker compose down` (no `-v`) → `up` → the row is still there.
  3. A plain `down` leaves no new dangling anonymous volume for the project.
  4. `docker compose down -v`, `just compose-down`, and `scripts/live-stack/down.sh --wipe`
     still drop both volumes and the next `up` starts empty.
  5. Existing guards in `tests/guards/test_install_topology_contract.py` and
     `tests/compose/test_compose_lifecycle_recipe.py` stay green; docs are reworded only where
     they become true.
- **Provenance:** issue #1911 body (problem, evidence, proposed fix, affected callers); the
  dispatching orchestrator's triage brief (acceptance criteria, ADR number assignment, file
  scope, and the instruction to decide and state the migration consequence rather than ask);
  ADR-0533 and the accepted compose non-destructive stop design for the promises the current
  compose file does not keep.
- **Exclusions:** no change to `ComposeWorkerLifecycle` behavior or the lifecycle wrapper's
  Docker calls (owner: the accepted non-destructive stop design's own exclusions); no change
  to the test fixtures a concurrent sibling run owns — `tests/support/xdist_backend.py`,
  `tests/conftest.py`, `tests/db/conftest.py`, `tests/store/conftest.py`,
  `tests/db/test_postgres_url_fixture.py`, `tests/store/test_minio_store_fixture.py`,
  `docs/operating/runbooks/` (owner: orchestrator's file-scope split); `justfile` edits kept
  minimal and additive because a later serial issue also edits it (owner: orchestrator).
- **Surface:** `docker-compose.yml`, `tests/compose/`,
  `tests/guards/test_install_topology_contract.py`, `docs/operating/docker-compose.md`,
  `deploy/compose/README.md`, `scripts/live-stack/down.sh`, `examples/local-libvirt/down.sh`,
  this spec, and ADR-0552.
- **Ambiguities:** none open. The one design-changing question — what happens to an existing
  install's anonymous-volume data — is resolved in ADR-0552 under the charter's provenance,
  which instructs this run to decide and state it.

## Problem

Neither `postgres` nor `minio` mounts anything at its data directory. Both images declare a
`VOLUME`, so Compose allocates an anonymous volume per service per `up`, and a `down` without
`--volumes` detaches rather than removes it. Every teardown/bring-up cycle therefore leaks one
unreferenced volume per backend **and** silently resets the stack.

That contradicts four committed promises: the `just compose-stop` recipe comment,
`scripts/live-stack/down.sh`'s header, the two operator guides, and the topology guard that
asserts their wording. ADR-0533's stop-old-first upgrade routes through `just compose-stop`,
so an operator following the documented upgrade loses the database.

## Contract

`docker-compose.yml` declares `kdive-pgdata` and `kdive-minio-data` in its top-level
`volumes:` block and mounts them at `/var/lib/postgresql/data` and `/data`. Postgres keeps its
existing read-only bootstrap-SQL bind mount alongside the new named volume.

Consequently:

| path | volumes after it runs |
|------|----------------------|
| `docker compose down`, `just compose-stop` | both named volumes retained |
| `scripts/live-stack/down.sh` (plain) | both named volumes retained |
| `docker compose down --volumes`, `just compose-down` | both named volumes removed |
| `scripts/live-stack/down.sh --wipe` | both named volumes removed |

No teardown path leaks a new anonymous volume for either backend.

Existing installs are not migrated; ADR-0552 records why, and the operating guide carries the
one-time `pg_dump`/restore note for an operator who needs continuity across this upgrade.

## Documentation changes

Only where the prose becomes true or newly misleading:

- `docs/operating/docker-compose.md` — name the two volumes where it discusses what a teardown
  keeps, and add the one-time upgrade note for an existing install.
- `deploy/compose/README.md` — same naming; keep its existing `just compose-down` teardown
  section, which is already correct.
- `scripts/live-stack/down.sh` header — name both volumes rather than only "Postgres volume",
  since the plain path now genuinely keeps both.
- `examples/local-libvirt/down.sh` final message — point at `just compose-down` instead of raw
  `docker compose down -v`, per ADR-0552's consequence.

## Testing

**Structural guard, runs on every PR** (`tests/compose/test_compose_config.py`, which already
gates on the `docker compose` plugin and renders `docker compose config --format json`):

- `postgres` has a mount whose `type` is `volume`, whose `source` is `kdive-pgdata`, and whose
  `target` is `/var/lib/postgresql/data`;
- `minio` has a mount whose `type` is `volume`, whose `source` is `kdive-minio-data`, and whose
  `target` is `/data`;
- the rendered model's top-level `volumes` declares both names;
- `postgres` still carries its bootstrap-SQL bind mount (the existing assertion, which the new
  mount must not displace);
- no service in the default model mounts a data directory with no `source` — the assertion that
  fails if a future service reintroduces the anonymous-volume shape.

Each assertion names the exact expected source and target rather than checking a count or an
emptiness, so a mount that disappears or moves reddens it.

**Executable proof, `live_stack` tier** (new
`tests/compose/test_compose_volume_persistence_live.py`): drive the committed
`docker-compose.yml` under a unique `-p` project name and free host ports, starting only
`postgres` and `minio` (neither has a `build:` or a `depends_on`, so no image is built and no
other service starts). It never touches a stack it did not create, and its own teardown is the
only `--volumes` call it makes.

1. `up -d postgres minio`, wait for both healthy; write a marker row into Postgres and put a
   marker object into MinIO; record the project's volume list.
2. `down` (no `-v`); assert the project's volume list is unchanged — non-empty, and containing
   both named volumes — and that no new unreferenced anonymous volume appeared.
3. `up -d postgres minio` again; assert the marker row and the marker object are both still
   readable. This is the criterion the current file fails.
4. `down --volumes`; assert both named volumes are gone, then `up` once more and assert the
   marker row and object are absent — proving the destructive path still resets.
5. Teardown removes the project's containers, network, and volumes unconditionally and probes
   that none remain, matching the cleanup discipline in
   `tests/compose/test_compose_worker_lifecycle_live.py`.

The structural guard is what gates PRs; the live proof is the end-to-end evidence run once on a
Docker-bearing host and reported with the change.

**Unchanged guards that must stay green:** `tests/guards/test_install_topology_contract.py`
(the documented stop-versus-destructive distinction) and
`tests/compose/test_compose_lifecycle_recipe.py` (the recipes' `--volumes` split).
`tests/image/test_compose_smoke.py` and `tests/compose/test_compose_worker_lifecycle_live.py`
already tear down with `--volumes`, so they keep starting empty.

## Rollout and rollback

Rollback is reverting the compose file: a stack whose data lives in `kdive-pgdata` and is then
reverted to the anonymous shape starts empty on the next `up`, with the named volumes left on
disk and reclaimable by `docker volume rm`. Nothing in the schema or the application changes,
so no code path has to tolerate both shapes at once.

## Not in scope

No change to the observability profile's ephemeral TSDB, to `ComposeWorkerLifecycle`, to the
`kdive-build` / `kdive-install` volumes, or to any test fixture backend (which uses
testcontainers, not this compose file).
