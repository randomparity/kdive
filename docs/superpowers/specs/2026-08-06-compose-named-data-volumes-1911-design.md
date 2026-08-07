# Compose named data volumes (#1911)

Decision record: [ADR-0552](../../adr/0552-compose-named-backend-data-volumes.md).

## Scope charter

- **Interaction:** unattended (orchestrator-dispatched background run).
- **Scope identity:** https://github.com/randomparity/kdive/issues/1911 plus annotation token
  `WORK-SCOPE-1911-a3f1c2`.
- **Outcome:** give the reference Compose services project-declared named data volumes so a
  plain `docker compose down` genuinely preserves their state instead of orphaning an anonymous
  volume, and so the destructive teardown paths remain the only way to drop it.
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
  Docker calls (owner: the accepted non-destructive stop design's own exclusions); no change to
  the test fixtures a concurrent sibling run owns — `tests/support/xdist_backend.py`,
  `tests/conftest.py`, `tests/db/conftest.py`, `tests/store/conftest.py`,
  `tests/db/test_postgres_url_fixture.py`, `tests/store/test_minio_store_fixture.py`,
  `docs/operating/runbooks/` (owner: orchestrator's file-scope split); `justfile` edits kept
  minimal and additive because a later serial issue also edits it (owner: orchestrator).
- **Surface:** `docker-compose.yml`, `tests/compose/`,
  `tests/guards/test_install_topology_contract.py`, `tests/image/test_compose_smoke.py`,
  `justfile` (one appended recipe), `docs/operating/docker-compose.md`,
  `deploy/compose/README.md`, `scripts/live-stack/down.sh`, `examples/local-libvirt/down.sh`,
  this spec, and ADR-0552.
- **Ambiguities:** none open. The one design-changing question — what happens to an existing
  install's anonymous-volume data — is resolved in ADR-0552 under the charter's provenance,
  which instructs this run to decide and state it.

Criterion 1 names `postgres` and `minio`; `prometheus` carries the identical leak
(`prom/prometheus:v3.12.0` declares `VOLUME /prometheus` and the file mounts only its config
read-only) and is swept under the same decision, because criterion 3 says *no* new dangling
anonymous volume for the project and `scripts/live-stack/down.sh` always passes
`--profile obs`. Its cover is tmpfs, not a named volume — see ADR-0552's rejected alternative.
`grafana/grafana:13.0.3` declares no `VOLUME` and is untouched.

## Problem

Neither `postgres`, `minio`, nor `prometheus` mounts anything at its data directory, and all
three images declare a `VOLUME` there. Compose allocates an anonymous volume per service per
`up`, and a `down` without `--volumes` detaches rather than removes it. Every teardown/bring-up
cycle leaks one unreferenced volume per service **and** silently resets the stack.

That contradicts the `just compose-stop` recipe comment, `scripts/live-stack/down.sh`'s header,
both operator guides, and the topology guard that asserts their wording. ADR-0533's
stop-old-first upgrade routes through `just compose-stop`, so an operator following the
documented upgrade loses the database.

## Contract

`docker-compose.yml` declares `kdive-pgdata` and `kdive-minio-data` in its top-level
`volumes:` block and mounts them at `/var/lib/postgresql/data` and `/data`. Postgres keeps its
existing read-only bootstrap-SQL bind mount alongside the new named volume. Prometheus keeps
its read-only config bind mount and gains `tmpfs: ["/prometheus:mode=1777"]`, which covers the
image's `VOLUME` without creating a volume at all.

| path | volumes after it runs |
|------|----------------------|
| `docker compose down`, `just compose-stop` | all named volumes retained |
| `scripts/live-stack/down.sh` (plain) | all named volumes retained |
| `docker compose down --volumes`, `just compose-down` | all named volumes removed |
| `scripts/live-stack/down.sh --wipe` | all named volumes removed |

The Prometheus TSDB is in no row: tmpfs dies with its container on every path. No teardown
path leaks a new anonymous volume for any service. Existing installs are not migrated;
ADR-0552 records why.

## Documentation changes

Every site that describes what a teardown keeps or drops, enumerated so none is left stale:

- the prometheus comment in `docker-compose.yml` claims the TSDB is container-local with no
  named volume and that `down` drops history. The first half becomes true under tmpfs and the
  second stays true; reword to say tmpfs explicitly and cite ADR-0189.
- `deploy/compose/README.md` — the same claim about the TSDB, reworded the same way.
- `docs/operating/docker-compose.md` — name the volumes (with their `<project>_` prefix) where
  it discusses what a teardown keeps; note in the test-override section that `kdive_test_*`
  leftovers now accumulate instead of self-clearing; and add the one-time upgrade note. That
  note documents a volume-to-volume byte copy performed while the old container still names the
  anonymous volume, not a dump/restore: `pg_dumpall` output replayed after `migrate` has already
  run against the fresh volume collides on every `CREATE`, and `psql` without `ON_ERROR_STOP=1`
  exits 0 through the whole failure. It also names the volume to drop rather than saying
  `docker volume prune`, which is host-wide.
- `deploy/compose/README.md` — the two bullets describing `up.sh --reset-db` and
  `down.sh --wipe` both say "the Postgres volume"; they now drop both data volumes.
- `scripts/live-stack/down.sh` — the header comment and the interactive `--wipe` WARNING echo
  both say "the Postgres volume"; both name the data volumes.
- `examples/local-libvirt/down.sh` — the header comment and the final `echo` both name only raw
  `docker compose down -v`; both name the preserving and the dropping form.
- `examples/local-libvirt/README.md` — its step-4 shutdown says `docker compose down -v`, which
  would now discard the database by default; same rewording.

The two clauses `tests/guards/test_install_topology_contract.py` matches verbatim —
`` `just compose-stop` preserves named volumes `` and `` `just compose-down` removes named
volumes `` — stay byte-identical in both guides. Volume names go in an adjacent sentence, never
inside those clauses.

## Testing

### Structural guard — runs on every PR

In `tests/compose/test_compose_config.py`, which already gates on the `docker compose` plugin
and renders `docker compose config --format json`. Compose emits each mount as an object keyed
`type` / `source` / `target`, and named-volume mounts carry no `read_only` key, so assertions
compare `(type, source, target)` tuples only.

An `_EXPECTED_MOUNTS` table drives the check, so a future backend must be enrolled rather than
silently inheriting the anonymous shape. It maps each service whose image declares a `VOLUME`
to its complete expected mount set. Parametrized over that table, the guard asserts:

- the service's rendered mount set, as sorted `(type, source, target)` tuples, equals the exact
  expected set — for `postgres` the bootstrap-SQL bind **and** `kdive-pgdata`, for `minio`
  `kdive-minio-data` alone, for `prometheus` the config bind alone. Full-set equality, not
  membership, so a mount that disappears, moves, or is displaced reddens it. Bind `source`
  values are rendered by Compose as absolute host paths, so the helper relativizes them
  against the compose file's directory; the expectation carries no checkout path. Sorting is
  deliberate — declaration order is cosmetic and is not part of the contract;
- the rendered top-level `volumes` block declares the unprefixed key (`kdive-pgdata`), whose
  `name` value is the project-prefixed `<project>_kdive-pgdata`;
- `prometheus` renders `tmpfs: ["/prometheus:mode=1777"]` and no `kdive-prom-data` appears in
  the top-level block. This is the case that would redden if someone "completed" the pattern
  by naming the TSDB volume — which `just compose-down` cannot reach.

`prometheus` renders only under `--profile obs`, so every case uses the existing `obs=True`
config helper.

An assertion of the form "no service mounts a data directory without a source" is deliberately
**not** written: an image's `VOLUME` never appears in the rendered model at all, so that check
passes unchanged on the broken tree. It is the vacuous shape it appears to guard against.

**Mutation step, run and reported before this lands:** delete each new `volumes:` entry from
`docker-compose.yml` one at a time, confirm the named parametrized case reddens, restore,
confirm green. Clear `__pycache__` between checks.

### Executable proof — `live_stack` tier

New `tests/compose/test_compose_volume_persistence_live.py`, driving the committed
`docker-compose.yml` under a unique `kdive-volume-proof-<token>` project name. It starts only
`postgres`, `minio`, and (under `--profile obs`) `prometheus` — none has a `build:` or a
`depends_on`, so no image is built and no other service starts. `prometheus` has no
healthcheck, so `--wait` gates on it reaching *running* while the two backends gate on
*healthy*; the TSDB is proven by mount shape rather than by a data round trip.

**Isolation.** All four published host ports are overridden to free loopback ports in the
compose process environment — `KDIVE_POSTGRES_PORT`, `KDIVE_MINIO_PORT`,
`KDIVE_MINIO_CONSOLE_PORT`, `KDIVE_PROMETHEUS_PORT` — each as `127.0.0.1:<port>`, so nothing
binds a routable interface. Every `up` names its services explicitly; a bare `up -d` would
start the default graph and build the app image through `migrate`. The proof **never** invokes
`just compose-down` or
`scripts/live-stack/down.sh`: both act on the default project, which is the operator's own
stack, and `down.sh --wipe` additionally `sudo virsh destroy`s every `kdive-*` domain and
deletes the rootfs overlays. Every `--volumes` call it makes names its own project.

**Volume identity comes from the container's own mounts, not a project-label filter.** Compose
does not label daemon-created anonymous volumes with a project, so
`docker volume ls --filter label=com.docker.compose.project=<proj>` returns empty on the broken
tree as well as the fixed one — it cannot detect the defect. The proof reads each running
container's `.Mounts` via `docker inspect` and asserts the mount at each data path has
`Type == "volume"` and `Name == "<project>_kdive-pgdata"` (and the minio equivalent). A 64-hex
name at that path is the defect, and this is the same anchor the `#1912` volume-leak proof
already uses. For `prometheus` the assertion inverts: the container must have **no** mount at
`/prometheus` at all, which is what a tmpfs looks like from outside and what distinguishes it
from both the anonymous volume it replaces and a named one.

Arms, in order:

1. `up -d postgres minio prometheus`, wait for postgres and minio healthy. Record each
   container's mount names as above; write a marker row into Postgres and put a marker object
   into MinIO.
2. `down` (no `-v`). Assert each recorded name is still present in `docker volume ls -q`.
   Arm 1 already established that those names are the project-prefixed ones rather than
   anonymous, so the pair of arms fails both when a volume is removed and when it was
   anonymous to begin with.
3. `up -d` again, wait for health, then read back. Assert the marker row is present **and** its
   value equals what arm 1 wrote, and that the marker object's bytes round-trip. This is the
   criterion the current file fails.
4. `down --volumes`. Assert none of the recorded volume names is in `docker volume ls -q`.
   Then `up -d` and wait for health. The reset proof is positive, not an absence that a failed
   connection would also produce: on an established connection, assert
   `SELECT count(*) FROM pg_tables WHERE tablename = '<marker>'` returns `0` **and** that
   writing and reading a fresh row still works; for MinIO assert `list_buckets()` succeeds,
   does not contain the marker bucket, and that a fresh bucket can be created and listed.
5. Teardown removes the project's containers, network, and volumes unconditionally, then probes
   that no container, network, or volume with the project token remains — including by the
   recorded volume names, which a project-label filter alone would miss.

**Fail-loud invocation.** `just test-live-stack` selects `-m live_stack` suite-wide and treats
a skip as success, so this proof gets the same treatment as the lifecycle proof: a dedicated
recipe appended to the `justfile`, `test-compose-volumes`, setting `KDIVE_REQUIRE_DOCKER=1` and
`KDIVE_RUN_COMPOSE_VOLUME_PROOF=1`, which the module requires before running. Without the
opt-in it skips; with it, an absent Docker is a failure rather than a skip, so the sole carrier
cannot report a skip as proof. That recipe's output is what gets reported with this change, and
`tests/compose/test_compose_lifecycle_recipe.py` gains a case asserting the recipe keeps both
environment variables and names the proof module — mirroring the guard the lifecycle proof's
recipe already has.

### Criterion 4 evidence

The live proof covers `docker compose down --volumes` directly. `just compose-down` and
`scripts/live-stack/down.sh --wipe` are covered by inspection, not execution, because both act
on the operator's default project: `tests/compose/test_compose_lifecycle_recipe.py::test_compose_stop_preserves_volumes_while_compose_down_removes_them`
already asserts `compose-down` passes `--volumes` and `compose-stop` does not, and
`scripts/live-stack/down.sh`'s `--wipe` branch is a literal
`docker compose --profile obs down -v`. Naming the volumes is what makes `--volumes` reach them; the recipes are
unchanged.

### Existing guards

`tests/guards/test_install_topology_contract.py` and
`tests/compose/test_compose_lifecycle_recipe.py` stay green unchanged.
`tests/image/test_compose_smoke.py` needs one change: it drives this compose file under the
fixed project name `kdive-smoke` and tears down with `--volumes` in a `finally` that an
interrupted run does not reach, so its volumes are now stable across runs. It gains a
`down --volumes --remove-orphans` **before** its `up`, so a killed run cannot leave the next
one testing migrations against an already-migrated database. The project name stays fixed —
that is what isolates it from the operator's stack. Two concurrent smoke runs already destroy
each other through the shared project name and the `finally` teardown; the pre-`up` teardown
does not change that, and making the name unique is a separate question this does not settle.
`tests/compose/test_compose_worker_lifecycle_live.py` drives its own fixture compose file
(`tests/compose/fixtures/worker-lifecycle-live.yml`, which already declares a named volume),
not this one, and is untouched.

## Rollout and rollback

Rollback is reverting the compose file: the stack then starts empty on the next `up`, with the
named volumes left on disk and reclaimable by `docker volume rm`. Nothing in the schema or the
application changes, so no code path tolerates both shapes at once.

Two persistence consequences are operator-visible and recorded in ADR-0552 and the operating
guide. `deploy/compose/bootstrap-migration-owner.sql` runs from `/docker-entrypoint-initdb.d`,
which Postgres executes only on an empty data directory — once per `kdive-pgdata` lifetime
instead of once per `up`, so a later edit to it or to the Postgres env literals needs a
`just compose-down` to take effect. And a `postgres:17` → `18` image bump now fails at startup
against a retained volume rather than starting fresh, with the same remedy.

## Not in scope

No change to `ComposeWorkerLifecycle`, to the `kdive-build` / `kdive-install` volumes, to
grafana (its image declares no `VOLUME`), or to any test fixture backend — the unit and
integration suites use testcontainers, not this compose file.
