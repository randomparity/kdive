# The authority teardown finishes a `tearing_down` System (#3026) — implementation plan

Goal: `systems.teardown` finishes a `tearing_down` System with external-boot history through the
authority teardown while its Allocation is `active`, and the stalled lane makes every skip
visible once.

Spec: [2026-10-01-tearing-down-authority-teardown-design.md](../specs/2026-10-01-tearing-down-authority-teardown-design.md).
Decision: ADR-0620 amendment (2026-10-01), on the branch.

Architecture: one forward-only migration, `0168`, adds `'tearing_down'` to the purpose-`teardown`
System-state list in three SQL functions. The worker precondition gains the same state. The
reconciler's warned set becomes a `system_id -> cause` map pruned to each pass's candidates.

Tech stack: Postgres plpgsql, Python 3.14, psycopg 3, pytest.

Expected implementation size: 380–480 changed lines (M) — migration (~30), worker set (~1),
reconciler (~40), test-helper move (~130 counted both sides), new tests (~180), runbook rewrite
and ADR amendment (~100).

## Global Constraints

- Migration number `0168`, file `src/kdive/db/schema/0168_tearing_down_authority_teardown.sql`.
  Applied migrations are byte-immutable (ADR-0015): edit no earlier migration.
- No change to any predicate other than the System-state list for purpose `teardown`. That
  includes the allocator's `v_allocation.state <> 'active'` fence, which #2992 owns.
- ADR-0620 is append-only: add one `### Amendment (2026-10-01): ... (#3026)` block before
  `## Consequences` and change no existing line.
- Guardrails: `just lint`, `just type`, focused `just test-verbose <paths>`, `just docs-links`,
  `just docs-paths`, `just records` after `git fetch origin main`, and the push hook's `just ci`
  run as `just ci > <file> 2>&1 < /dev/null`.

## Task 1: migration 0168 and the worker precondition

Files: create `src/kdive/db/schema/0168_tearing_down_authority_teardown.sql` and
`tests/db/test_migration_0168_tearing_down_authority_teardown.py`. Modify
`src/kdive/jobs/handlers/external_boot/lifecycle.py` (`_teardown_prerequisites`),
`tests/db/external_boot_authority_support.py`,
`tests/db/test_migration_0160_external_boot_teardown_failure_commit.py`, and
`tests/integration/test_external_boot_unrouted_teardown.py`.

Interfaces (existing at `3ed5b5c0e`):
- `tests/db/external_boot_authority_support.py`: `_ALLOCATE_SIGNATURE`, `_COMMIT_SIGNATURE`,
  `_PLAN`, `_JOURNAL`, `_QUIESCENCE`, `_AuthorityCase`, `_Allocated`, `_RoleDsns`,
  `_apply_through(conn, version)`, `_allocate(worker, case) -> _Allocated`.
- `tests/db/external_boot_journal_support.py`: `_ready_teardown_case(url, suffix)`,
  `_proof(case, disposition)`, `_make_current(conn, case, authority, proof, sequence) -> str`,
  `_finalize(role_dsns, case, authority, proof, sequence, digest) -> str`.
- `tests/integration/test_external_boot_unrouted_teardown.py`: `_boot`, `_teardown`, `_worker`,
  `_OUTCOME_SQL`, `_MARKER`; `kdive.jobs.queue.enqueue`, `kdive.jobs.payloads.Authorizing`,
  `TeardownPayload`.
- Moved by this task: `_FAILURE`, `_acknowledge(conn, case, authority)` and
  `_commit(worker, case, authority, *, attempt=1, terminal=False) -> tuple[str, str | None]`
  move unchanged from the 0160 test into `external_boot_authority_support.py`. The 0160 test
  imports them from there.

Verification:
- Contract: the allocator admits `tearing_down` for teardown. Mode: focused-test,
  `test_0168_tearing_down_teardown_credits_once`. Red before the migration:
  `_allocate` asserts `row[0] == "allocated"` and fails on `superseded`.
- Contract: the finalizer takes `tearing_down -> torn_down` and credits once. Mode:
  focused-test, the same test (two `applied` returns, one release row, one audit row).
- Contract: the failure commit admits `tearing_down`. Mode: focused-test,
  `test_0168_tearing_down_teardown_failure_commits` (red: `("superseded", None)`).
- Contract: an expired Allocation still allocates nothing. Mode: focused-test,
  `test_0168_expired_allocation_still_supersedes`. This passes before and after the migration,
  so it guards against widening the fence. Its bite: replacing `'active'` in the allocator would
  turn it red.
- Contract: each patch target exists exactly once. Mode: focused-test,
  `test_0168_patch_targets_exist_once`.
- Contract: the worker admits `tearing_down`. Mode: focused-test,
  `test_tearing_down_system_completes_through_the_authority`. Red with only the migration: the
  job fails `configuration_error` ("teardown requires the newest activation on nonterminal
  system").

Steps:

1. Move `_FAILURE`, `_acknowledge` and `_commit` into `external_boot_authority_support.py`
   (add `from psycopg.types.json import Jsonb` if missing; it is already imported) and import
   them in the 0160 test. Run
   `just test-verbose tests/db/test_migration_0160_external_boot_teardown_failure_commit.py`.
   Expect it to stay green.
2. Write the migration test:

```python
"""Real-Postgres proofs that the authority teardown admits a `tearing_down` System (#3026)."""

from __future__ import annotations

import re

import psycopg

from kdive.db import migrate
from tests.db.external_boot_authority_support import (
    _ALLOCATE_SIGNATURE,
    _COMMIT_SIGNATURE,
    _PLAN,
    _acknowledge,
    _allocate,
    _apply_through,
    _AuthorityCase,
    _commit,
    _RoleDsns,
)
from tests.db.external_boot_journal_support import (
    _finalize,
    _make_current,
    _proof,
    _ready_teardown_case,
)

_FINALIZE_SIGNATURE = (
    "finalize_external_boot_authority_teardown(bytea,uuid,integer,uuid,bigint,bigint,text,bytea)"
)


def _migration_sql() -> str:
    return next(m for m in migrate.discover_migrations() if m.version == "0168").sql


def _tearing_down_case(migrated_url: str, suffix: str) -> _AuthorityCase:
    case = _ready_teardown_case(migrated_url, suffix)
    with psycopg.connect(migrated_url) as conn:
        conn.execute("UPDATE systems SET state = 'tearing_down' WHERE id = %s", (case.system_id,))
    return case


def test_0168_patch_targets_exist_once(pg_conn: psycopg.Connection) -> None:
    _apply_through(pg_conn, "0167")
    (target,) = re.findall(r"v_old constant text := \$old\$(.*?)\$old\$;", _migration_sql())
    for signature in (_ALLOCATE_SIGNATURE, _FINALIZE_SIGNATURE, _COMMIT_SIGNATURE):
        row = pg_conn.execute(
            "SELECT pg_get_functiondef(%s::regprocedure)", (f"public.{signature}",)
        ).fetchone()
        assert row is not None
        assert row[0].count(target) == 1, signature
        assert "'tearing_down'" not in row[0], signature


def test_0168_tearing_down_teardown_credits_once(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    case = _tearing_down_case(migrated_url, "u")
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        authority = _allocate(worker, case)
    proof = _proof(case, "complete_ready")
    with psycopg.connect(migrated_url) as conn:
        digest = _make_current(conn, case, authority, proof, 2)
    assert _finalize(authority_role_dsns, case, authority, proof, 2, digest) == "applied"
    assert _finalize(authority_role_dsns, case, authority, proof, 2, digest) == "applied"
    with psycopg.connect(migrated_url) as conn:
        outcome = conn.execute(
            "SELECT s.state, "
            "(SELECT count(*) FROM external_boot_reservation_releases r "
            " WHERE r.activation_id = %s), "
            "(SELECT count(*) FROM external_boot_reservations r WHERE r.activation_id = %s), "
            "(SELECT array_agg(a.transition) FROM audit_log a "
            " WHERE a.object_id = s.id AND a.tool = 'systems.teardown') "
            "FROM systems s WHERE s.id = %s",
            (case.activation_id, case.activation_id, case.system_id),
        ).fetchone()
    assert outcome == ("torn_down", 1, 0, ["tearing_down->torn_down"])


def test_0168_tearing_down_teardown_failure_commits(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    case = _tearing_down_case(migrated_url, "v")
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        authority = _allocate(worker, case)
    with psycopg.connect(migrated_url) as conn:
        _acknowledge(conn, case, authority)
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        assert _commit(worker, case, authority) == ("applied", "queued")
    with psycopg.connect(migrated_url) as conn:
        assert conn.execute(
            "SELECT state FROM systems WHERE id = %s", (case.system_id,)
        ).fetchone() == ("tearing_down",)


def test_0168_expired_allocation_still_supersedes(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    case = _tearing_down_case(migrated_url, "w")
    with psycopg.connect(migrated_url) as conn:
        conn.execute(
            "UPDATE allocations SET state = 'expired' WHERE id = %s", (case.allocation_id,)
        )
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        row = worker.execute(
            "SELECT status FROM allocate_external_boot_authority"
            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                case.credential,
                case.job_id,
                case.attempt,
                case.activation_id,
                case.run_id,
                case.system_id,
                _PLAN,
                case.purpose,
                case.provider_kind,
                case.authority_instance,
                case.operation_identity,
            ),
        ).fetchone()
    assert row == ("superseded",)
    with psycopg.connect(migrated_url) as conn:
        assert conn.execute(
            "SELECT count(*) FROM external_boot_authorities WHERE system_id = %s",
            (case.system_id,),
        ).fetchone() == (0,)
```

3. Run `just test-verbose tests/db/test_migration_0168_tearing_down_authority_teardown.py`.
   Expect the collection error `StopIteration` from `_migration_sql` (no 0168 yet) or a failure
   on `_allocate`.
4. Write the migration:

```sql
-- 0168_tearing_down_authority_teardown.sql — #3026, ADR-0620 amendment (2026-10-01).
-- Forward-only (ADR-0015). A pre-#2966 ordinary teardown can leave a System with external-boot
-- history in `tearing_down`, which only the authority teardown may finish. Admit that state in
-- the three purpose-`teardown` System-state lists: the allocator (0147), the success finalizer
-- (0147, 0149) and the failure commit (0160). Each definition carries the list's tail exactly
-- once. Every other fence, including the `active` Allocation check (#2992), is unchanged.
DO $$
DECLARE
    v_old constant text := $old$'paused', 'crashing', 'crashed', 'failed'$old$;
    v_new constant text := $new$'paused', 'crashing', 'crashed', 'failed', 'tearing_down'$new$;
    v_function regprocedure;
    v_definition text;
BEGIN
    FOREACH v_function IN ARRAY ARRAY[
        'public.allocate_external_boot_authority(bytea,uuid,integer,uuid,uuid,uuid,text,text,'
        'text,text,text)',
        'public.finalize_external_boot_authority_teardown(bytea,uuid,integer,uuid,bigint,bigint,'
        'text,bytea)',
        'public.commit_external_boot_authority_result(bytea,uuid,integer,uuid,bigint,uuid,uuid,'
        'uuid,text,text,text,text,text,text,bigint,text,text,jsonb)'
    ]::regprocedure[] LOOP
        v_definition := pg_get_functiondef(v_function);
        IF (length(v_definition) - length(replace(v_definition, v_old, ''))) / length(v_old) <> 1
           OR position('''tearing_down''' IN v_definition) <> 0 THEN
            RAISE EXCEPTION 'external boot teardown System-state list changed in %', v_function;
        END IF;
        EXECUTE replace(v_definition, v_old, v_new);
    END LOOP;
END
$$;
```

5. Run the 0168 test file again; expect all five tests to pass.
6. In `_teardown_prerequisites`, add `"tearing_down",` after `"failed",` in `admitted_states`.
7. Append the integration test to `tests/integration/test_external_boot_unrouted_teardown.py`.
   Add `from kdive.jobs import queue` and
   `from kdive.jobs.payloads import Authorizing, TeardownPayload`, and widen the module
   docstring to name #3026:

```python
def test_tearing_down_system_completes_through_the_authority(
    migrated_url: str, authority_role_dsns: Callable[[str], str]
) -> None:
    """#3026: a pre-#2966 `tearing_down` residue reaches `torn_down` through systems.teardown."""

    async def body() -> None:
        configure_external_boot()
        resolver = provider_resolver(external_boot=PreparingProvider())
        worker_id = "local:tearing-down-teardown"
        async with (
            AsyncConnectionPool(migrated_url, min_size=2, max_size=6) as pool,
            await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as seed,
        ):
            await register_incarnation(pool, worker_id)
            executor = RecordingTeardownExecutor(seed)
            worker = _worker(
                pool, resolver, authority_role_dsns("kdive_provider_authority"), executor, worker_id
            )
            system_id, job_id = await _boot(pool, resolver)
            canceled = await cancel_job(pool, runs_support.ctx(), job_id)
            assert canceled.status == "canceled", canceled.model_dump()
            # The residue: a pre-#2966 ordinary teardown moved the System and then failed.
            project = (
                await fetch_one(
                    seed, "SELECT project FROM systems WHERE id = %s", (UUID(system_id),)
                )
            )["project"]
            ordinary = await queue.enqueue(
                seed,
                JobKind.TEARDOWN,
                TeardownPayload(system_id=system_id),
                Authorizing(principal="reconciler", agent_session=None, project=project),
                f"{system_id}:teardown",
            )
            await seed.execute(
                "UPDATE jobs SET state = 'failed', error_category = 'conflict' WHERE id = %s",
                (ordinary.id,),
            )
            await seed.execute(
                "UPDATE systems SET state = 'tearing_down' WHERE id = %s", (UUID(system_id),)
            )
            response = await _teardown(pool, system_id, resolver)
            assert response.status == "queued", response.model_dump()
            lane = await fetch_one(
                seed, "SELECT dispatch_lane FROM jobs WHERE id = %s", (UUID(response.object_id),)
            )
            claimed = await worker.run_once(lane["dispatch_lane"])
            assert claimed is not None and str(claimed.id) == response.object_id
            outcome = await fetch_one(seed, _OUTCOME_SQL, (UUID(system_id),))
            job = await fetch_one(
                seed,
                "SELECT state, payload ? %s AS marked FROM jobs WHERE id = %s",
                (_MARKER, UUID(response.object_id)),
            )
            audit = await fetch_one(
                seed,
                "SELECT array_agg(transition) AS transitions FROM audit_log "
                "WHERE object_id = %s AND tool = 'systems.teardown'",
                (UUID(system_id),),
            )

        assert response.object_id == str(ordinary.id)
        assert job == {"state": "succeeded", "marked": True}
        assert len(executor.calls) == 1
        assert outcome == {
            "state": "torn_down",
            "system_state": "torn_down",
            "reservations": 0,
            "releases": 0,
            "authorities": ["teardown"],
        }
        assert audit == {"transitions": ["tearing_down->torn_down"]}

    asyncio.run(body())
```

8. Run `just test-verbose tests/integration/test_external_boot_unrouted_teardown.py`. Expect
   all tests to pass. Then revert step 6 locally, rerun, observe the new test fail on
   `job == {"state": "failed", ...}`, and restore step 6. Commit once the implementation is
   committed, per the controlled-fault rule.
9. `just lint`, `just type`; commit `feat(external-boot): authority teardown admits tearing_down (#3026)`.

Acceptance: every listed test passes, and `git diff --stat` touches no earlier migration.

## Task 2: stalled-lane visibility and pruning

Files: modify `src/kdive/reconciler/repairs/systems.py` and
`tests/reconciler/test_stalled_teardown_recovery.py`.

Interfaces (existing): `repair_stalled_tearing_down_systems(conn) -> int`; `Job.id`,
`Job.state: JobState`; test helpers `connect`, `run_repair`, `seed_system`,
`_seed_teardown_job`, `_seed_completed_activation`.
Produces: `_warned_stalled_teardowns: dict[UUID, str]`, which replaces
`_warned_stalled_teardown_history`, and
`_warn_stalled_teardown_once(system_id, cause, message, *args) -> None`, which replaces
`_warn_stalled_teardown_history`.

Verification:
- Contract: a marked prior row warns once per System. Mode: focused-test,
  `test_authority_marked_row_warns_once` (red: zero warnings).
- Contract: the warned map drops a System that leaves `tearing_down`, and a System that
  returns warns again. Mode: focused-test,
  `test_warned_map_drops_a_system_that_leaves_tearing_down` (red: the entry is still present).
- Contract: the history warning still fires once. Mode: focused-test, the existing
  `test_external_boot_history_is_not_requeued_and_warns_once`, renamed to use the new map.

Steps:

1. Replace the set and the helper:

```python
# System id -> the cause last warned about for a skipped stalled `tearing_down` (#3015, #3026).
# Pruned every pass to that pass's candidates, so a System that leaves `tearing_down` is dropped.
_warned_stalled_teardowns: dict[UUID, str] = {}
_STUCK_TEARING_DOWN_RUNBOOK = "docs/operating/runbooks/stuck-tearing-down-system.md"


def _warn_stalled_teardown_once(system_id: UUID, cause: str, message: str, *args: object) -> None:
    if _warned_stalled_teardowns.get(system_id) == cause:
        return
    _warned_stalled_teardowns[system_id] = cause
    _log.warning(message, system_id, *args)
```

2. After `candidates` is read, prune:

```python
    for gone in _warned_stalled_teardowns.keys() - set(candidates):
        del _warned_stalled_teardowns[gone]
```

3. Replace the marker `continue` and the history warning:

```python
if existing is not None and _AUTHORITY_TEARDOWN_PAYLOAD_KEYS & existing.payload.keys():
    _warn_stalled_teardown_once(
        system_id,
        "authority_marked",
        "reconciler: system %s is stuck in tearing_down behind authority-marked "
        "teardown job %s (%s); this lane never replaces it, so re-run "
        "systems.teardown (%s)",
        existing.id,
        existing.state.value,
        _STUCK_TEARING_DOWN_RUNBOOK,
    )
    continue
activation = await _EXTERNAL_BOOT_ACTIVATIONS.get_latest_for_system(conn, system_id)
if activation is not None:
    _warn_stalled_teardown_once(
        system_id,
        "external_boot_history",
        "reconciler: system %s is stuck in tearing_down with external-boot "
        "activation %s; the ordinary teardown is refused "
        "(external_boot_teardown_not_supported), so no job is requeued; run "
        "systems.teardown while its Allocation is active (%s)",
        activation.id,
        _STUCK_TEARING_DOWN_RUNBOOK,
    )
    continue
```

4. Docstring: replace "The skip logs one WARNING per System per process; the supported exit is
   tracked in #3026." with "Each skip, including an authority-marked prior row, logs one WARNING
   per System and cause; the warned map is pruned to each pass's candidates (#3026)."
5. Tests: change `_warned_stalled_teardown_history.clear()` to `_warned_stalled_teardowns.clear()`,
   then add:

```python
def test_authority_marked_row_warns_once(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=system_repairs.__name__)
    system_repairs._warned_stalled_teardowns.clear()

    async def _run() -> None:
        conn = await connect(migrated_url)
        system_id = await seed_system(conn, system_state=SystemState.TEARING_DOWN)
        await _seed_teardown_job(conn, system_id, state=JobState.FAILED)
        await conn.execute(
            "UPDATE jobs SET payload = payload || %s WHERE dedup_key = %s",
            (Jsonb({"external_boot_authority_v1": {"marker": 1}}), f"{system_id}:teardown"),
        )
        async with AsyncConnectionPool(migrated_url, min_size=1, open=False) as pool:
            await pool.open()
            for _ in range(2):
                assert await run_repair(pool, repair_stalled_tearing_down_systems) == 0
        warnings = [r for r in caplog.records if str(system_id) in r.message]
        assert len(warnings) == 1
        assert "authority-marked" in warnings[0].message
        await conn.close()

    asyncio.run(_run())


def test_warned_map_drops_a_system_that_leaves_tearing_down(
    migrated_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger=system_repairs.__name__)
    system_repairs._warned_stalled_teardowns.clear()

    async def _run() -> None:
        conn = await connect(migrated_url)
        system_id = await seed_system(conn, system_state=SystemState.TEARING_DOWN)
        await _seed_completed_activation(conn, system_id)
        await _seed_teardown_job(conn, system_id, state=JobState.FAILED)
        async with AsyncConnectionPool(migrated_url, min_size=1, open=False) as pool:
            await pool.open()
            await run_repair(pool, repair_stalled_tearing_down_systems)
            assert system_id in system_repairs._warned_stalled_teardowns
            await conn.execute("UPDATE systems SET state = 'torn_down' WHERE id = %s", (system_id,))
            await run_repair(pool, repair_stalled_tearing_down_systems)
            assert system_id not in system_repairs._warned_stalled_teardowns
            await conn.execute(
                "UPDATE systems SET state = 'tearing_down' WHERE id = %s", (system_id,)
            )
            await run_repair(pool, repair_stalled_tearing_down_systems)
        warnings = [r for r in caplog.records if str(system_id) in r.message]
        assert len(warnings) == 2
        await conn.close()

    asyncio.run(_run())
```

6. Run `just test-verbose tests/reconciler/test_stalled_teardown_recovery.py`; expect all to
   pass. Then remove the prune loop locally, observe the prune test fail on
   `system_id not in ...`, and restore it. `just lint`, `just type`; commit
   `feat(reconciler): warn on a marked stalled teardown and prune the warned map (#3026)`.

## Task 3: runbook and ADR-0620 amendment

Files: rewrite `docs/operating/runbooks/stuck-tearing-down-system.md` and update its index row in
`docs/operating/index.md`.

Verification:
- Contract: links and doc paths resolve. Mode: focused-test, `just docs-links` and
  `just docs-paths` exit 0.
- Contract: the ADR stays a valid accepted record and is append-only. Mode: focused-test,
  `git fetch origin main && just records` exits 0, and
  `git diff origin/main -- docs/adr/0620-authority-owned-system-teardown.md` shows only added
  lines.
- Contract: runbook prose. Mode: task-test-not-applicable — operator prose with no executable
  consumer; the reconciler log names its path, which `just docs-paths` resolves.

Steps:

1. Runbook sections: Symptom (both WARNINGs and their texts); Confirm the state (keep both SQL
   queries); Recover: (a) while the Allocation is `active`, run `systems.teardown` (project
   `admin`) or `ops.force_teardown` (platform admin, `--force` and a reason); it replaces the
   refused job and the authority teardown ends at `torn_down`; (b) `allocations.renew` extends a
   near-expiry lease, clamped to the lease maximum; (c) then `allocations.release` and
   `investigations.close`. After expiry: no supported exit until #2992; do not write
   `systems.state`.
2. Index row: "Ending a `tearing_down` System with external-boot history through the authority
   teardown".
3. The ADR-0620 amendment was written with the design and is on the branch. Change it only if
   the build contradicts it, and keep it append-only.
4. Run the three doc guardrails; commit `docs(operating): runbook names the authority teardown exit (#3026)`.

## Spec coverage

| Spec item | Task |
|---|---|
| Design 1, 3; Success 2, 3 | Task 1 (migration tests) |
| Design 2; Success 1 | Task 1 (integration test) |
| Design 4; Success 4 | Task 2 |
| Design 5 | Task 3 |
