# Bound the acknowledged-retry grant per budget — plan

Goal: a granted external-boot attempt cannot earn a second ADR-0626 grant, and the reconciler
fails a `boot` job past that bound (#2960).

Architecture: migration 0165 renames the 0151 proof predicate to an evidence-only function and
puts a bound-checking wrapper under the old name, which the claim, queue-depth, and consume
functions already call. The same migration replaces the 0162 dead-letter function so it also
ends a job past the bound, under the journal-head lock. Spec:
`docs/workflow/specs/2026-09-29-bound-acknowledged-retry-grant-design.md`; decision: ADR-0711.

Tech stack: PostgreSQL SQL/PL/pgSQL migrations (`src/kdive/db/schema/`), pytest with a real
Postgres (`migrated_url`, `authority_role_dsns` fixtures).

Expected implementation size: 190–250 changed lines (M) — migration about 90, new tests about
120, registration lists 8, ADR-0626 banner 3.

## Global Constraints

- Migration number `0165` and ADR number `0711` are assigned; do not pick others.
- Never edit an applied migration file (0150, 0151, 0162); `just schema-guard` enforces this.
- Each new function has `SET search_path = ''` and the 0150 REVOKE list:
  `FROM PUBLIC, kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness,
  kdive_provider_authority`. The dead-letter function keeps its 0162 grant to
  `kdive_reconciler` (`CREATE OR REPLACE` keeps the ACL).
- Lines stay at most 100 characters (ruff, sqlfluff-free repo).
- Run gates bare with Homebrew bash 4.4+ and gnubin on `PATH` (macOS).

## File map

| File | Change | Owner of |
|---|---|---|
| `src/kdive/db/schema/0165_bound_acknowledged_retry_grant.sql` | create | bound predicate; dead-letter past the bound |
| `tests/db/test_external_boot_authority_journal_migration.py` | modify | ADR-0626/0711 claim and dead-letter proofs |
| `tests/db/test_migrate.py`, `tests/db/test_migration_0091_system_object_sweep_cursors.py`, `tests/db/test_migration_0102_build_gc_cursors.py`, `tests/db/test_migration_0115_capture_reap_state.py` | modify | pinned migration lists |
| `docs/adr/0626-recover-exhausted-acknowledged-authority-claims.md` | modify Status only | `Amended by` banner |

No Python runtime file changes: `reconciler/repairs/jobs.py` already calls
`dead_letter_unowned_external_boot_jobs()` and logs each returned id.

## Task 1 — The bound and the terminal path

Interfaces: consumes `public.external_boot_acknowledged_retry_consumptions` (0150),
`public.has_acknowledged_external_boot_retry_proof(public.jobs)` (0151 body). Produces
`public.has_acknowledged_external_boot_no_mutation_head(public.jobs) RETURNS boolean`,
the new `public.has_acknowledged_external_boot_retry_proof(public.jobs) RETURNS boolean`, and
`public.dead_letter_unowned_external_boot_jobs() RETURNS SETOF uuid` (same signature as 0162).

Verification:

- Contract S1 (bound). Mode: focused-test. Test
  `test_granted_attempt_cannot_earn_a_second_grant`. Red before the migration: the first
  assertion fails because `has_acknowledged_external_boot_no_mutation_head` does not exist.
  Green: `uv run pytest -q -p no:cacheprovider
  tests/db/test_external_boot_authority_journal_migration.py -k "second_grant"` → `2 passed`.
- Contract S2 (terminal). Mode: focused-test. Test
  `test_reconciler_dead_letters_a_job_past_the_grant_bound`. Red: the dead-letter returns `[]`.
  Green: same command with `-k "past_the_grant_bound"` → `2 passed`.
- Contract S4 (skip kept). Mode: focused-test. Test
  `test_dead_letter_skips_a_job_without_a_spent_grant_and_proof` (green before and after; it
  guards against an over-wide change) plus `tests/db/test_exhausted_authority_job.py`.
- Contract S3 and S5. Mode: focused-test. Existing
  `test_exhausted_acknowledged_authority_job_gets_one_recovery_claim` stays green;
  `test_acknowledged_retry_proof_helper_is_private` gains the new signature.

Steps:

1. Add the helper and the three tests below to
   `tests/db/test_external_boot_authority_journal_migration.py`, after
   `test_exhausted_job_validates_unanchored_successor_authorities`. Add
   `"has_acknowledged_external_boot_no_mutation_head(jobs)"` to the signature tuple in
   `test_acknowledged_retry_proof_helper_is_private`.

```python
def _granted_attempt_head(
    migrated_url: str,
    role_dsns: _RoleDsns,
    suffix: str,
    *,
    promote: bool,
    acknowledge: bool = True,
) -> tuple[Any, Any, JournalRecordV1]:
    """Claim the one grant, then end the granted attempt at a new head and lapse its lease."""
    case, _first, first_ack = _seed_exhausted_acknowledged_job(
        migrated_url, role_dsns, suffix, promote=True
    )
    credential = b"g" * 32
    worker_id = _register_worker(migrated_url, f"granted-{suffix}", credential)
    with psycopg.connect(role_dsns("kdive_worker"), autocommit=True) as worker:
        claimed = worker.execute(
            "SELECT id,attempt,max_attempts FROM claim_worker_job("
            "%s,%s,interval '1 minute',ARRAY['default'])",
            (worker_id, credential),
        ).fetchone()
        assert claimed == (case.job_id, 4, 4)
        granted_case = replace(case, worker_id=worker_id, credential=credential, attempt=4)
        granted = _allocate(worker, granted_case)
    attempt_id = str(granted.authority_id)
    head = _record(
        granted_case,
        granted,
        3,
        record_digest(first_ack),
        JournalPhase.WATERMARK_INSTALLED,
        attempt_id=attempt_id,
    )
    with psycopg.connect(
        role_dsns("kdive_provider_authority"), autocommit=True
    ) as connection:
        assert (
            _advance_raw(
                connection, granted_case, granted, 2, record_digest(first_ack), _payload(head)
            )
            == "advanced"
        )
        if acknowledge:
            watermark = head
            head = _record(
                granted_case,
                granted,
                4,
                record_digest(watermark),
                JournalPhase.TAKEOVER_ACKNOWLEDGED,
                watermark_sequence=3,
                watermark_digest=record_digest(watermark),
                attempt_id=attempt_id,
            )
            assert (
                _advance_raw(
                    connection, granted_case, granted, 3, record_digest(watermark), _payload(head)
                )
                == "advanced"
            )
    if acknowledge and promote:
        _promote(migrated_url, granted_case, granted, head)
    with psycopg.connect(migrated_url) as connection:
        connection.execute(
            "UPDATE jobs SET lease_expires_at=now()-interval '1 minute' WHERE id=%s",
            (case.job_id,),
        )
    return granted_case, granted, head


def _dead_letter_unowned(role_dsns: _RoleDsns) -> list[UUID]:
    with psycopg.connect(role_dsns("kdive_reconciler"), autocommit=True) as reconciler:
        rows = reconciler.execute(
            "SELECT * FROM dead_letter_unowned_external_boot_jobs()"
        ).fetchall()
    return [row[0] for row in rows]


@pytest.mark.parametrize("promote", [False, True])
def test_granted_attempt_cannot_earn_a_second_grant(
    migrated_url: str, authority_role_dsns: _RoleDsns, *, promote: bool
) -> None:
    """ADR-0711: the attempt a grant claimed stays exhausted despite its new acknowledged head."""
    case, _granted, _head = _granted_attempt_head(
        migrated_url, authority_role_dsns, "b" if promote else "c", promote=promote
    )
    with psycopg.connect(migrated_url) as connection:
        assert connection.execute(
            "SELECT public.has_acknowledged_external_boot_no_mutation_head(j),"
            "public.has_acknowledged_external_boot_retry_proof(j) "
            "FROM public.jobs AS j WHERE j.id=%s",
            (case.job_id,),
        ).fetchone() == (True, False)
    credential = b"h" * 32
    worker_id = _register_worker(migrated_url, "past-the-bound", credential)
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        assert worker.execute(
            "SELECT count_claimable_worker_jobs(ARRAY['default'])"
        ).fetchone() == (0,)
        assert (
            worker.execute(
                "SELECT id FROM claim_worker_job(%s,%s,interval '1 minute',ARRAY['default'])",
                (worker_id, credential),
            ).fetchone()
            is None
        )


@pytest.mark.parametrize("promote", [False, True])
def test_reconciler_dead_letters_a_job_past_the_grant_bound(
    migrated_url: str, authority_role_dsns: _RoleDsns, *, promote: bool
) -> None:
    """ADR-0711: past the bound, the job fails and its authority can no longer admit work."""
    case, granted, head = _granted_attempt_head(
        migrated_url, authority_role_dsns, "d" if promote else "e", promote=promote
    )
    assert _dead_letter_unowned(authority_role_dsns) == [case.job_id]
    with psycopg.connect(migrated_url) as connection:
        assert connection.execute(
            "SELECT state,error_category FROM jobs WHERE id=%s", (case.job_id,)
        ).fetchone() == ("failed", "lease_expired")
        assert connection.execute(
            "SELECT state FROM external_boot_authorities WHERE job_id=%s ORDER BY generation",
            (case.job_id,),
        ).fetchall() == [("superseded",), ("superseded",)]
    admitted = _record(case, granted, 5, record_digest(head), JournalPhase.ADMITTED)
    with psycopg.connect(
        authority_role_dsns("kdive_provider_authority"), autocommit=True
    ) as connection:
        assert (
            _advance_raw(connection, case, granted, 4, record_digest(head), _payload(admitted))
            == "superseded"
        )
    assert _dead_letter_unowned(authority_role_dsns) == []


@pytest.mark.parametrize("shape", ["unspent-grant", "unacknowledged-granted-attempt"])
def test_dead_letter_skips_a_job_without_a_spent_grant_and_proof(
    migrated_url: str, authority_role_dsns: _RoleDsns, shape: str
) -> None:
    """A job keeps its `current`/`allocating` authority unless it is past the bound with a proof."""
    if shape == "unspent-grant":
        case, _authority, _ack = _seed_exhausted_acknowledged_job(
            migrated_url, authority_role_dsns, "i", promote=True
        )
    else:
        case, _authority, _ack = _granted_attempt_head(
            migrated_url, authority_role_dsns, "j", promote=False, acknowledge=False
        )
    assert _dead_letter_unowned(authority_role_dsns) == []
    with psycopg.connect(migrated_url) as connection:
        assert connection.execute(
            "SELECT state FROM jobs WHERE id=%s", (case.job_id,)
        ).fetchone() == ("running",)
```

2. Run the Task 1 focused command; expect the S1 and S2 tests to fail as stated above.
3. Create `src/kdive/db/schema/0165_bound_acknowledged_retry_grant.sql`:

```sql
-- ADR-0711 amends ADR-0626 (#2960): one acknowledged-retry grant per exhausted budget.
-- 0150 granted a claim for every new exact acknowledged no-mutation head, so a job whose every
-- attempt the authority refused before provider admission re-claimed at each lease lapse.
-- The attempt a grant claimed (its consumption row's claimed_attempt) now earns no other grant.
-- Claim, queue depth and consume call has_acknowledged_external_boot_retry_proof by name, so they
-- share the bound.  The reconciler then fails a boot job past the bound whose head still proves
-- no mutation, superseding its authority under the lock every journal-head advance takes.
ALTER FUNCTION public.has_acknowledged_external_boot_retry_proof(public.jobs)
    RENAME TO has_acknowledged_external_boot_no_mutation_head;

CREATE FUNCTION public.has_acknowledged_external_boot_retry_proof(p_job public.jobs)
RETURNS boolean
LANGUAGE sql
STABLE
RETURNS NULL ON NULL INPUT
SET search_path = ''
AS $$
    SELECT NOT EXISTS (
        SELECT 1
        FROM public.external_boot_acknowledged_retry_consumptions AS consumed
        WHERE consumed.job_id = p_job.id AND consumed.claimed_attempt = p_job.attempt
    ) AND public.has_acknowledged_external_boot_no_mutation_head(p_job)
$$;

REVOKE ALL ON FUNCTION public.has_acknowledged_external_boot_retry_proof(public.jobs)
    FROM PUBLIC, kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness,
         kdive_provider_authority;

CREATE OR REPLACE FUNCTION public.dead_letter_unowned_external_boot_jobs()
RETURNS SETOF uuid
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_job public.jobs%ROWTYPE;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_reconciler', 'member') THEN
        RAISE EXCEPTION 'reconciler authority is required' USING ERRCODE = '42501';
    END IF;
    FOR v_job IN
        SELECT j.* FROM public.jobs AS j
        WHERE j.state = 'running' AND j.kind = 'boot'
          AND j.attempt >= j.max_attempts
          AND j.lease_expires_at < clock_timestamp()
          AND jsonb_typeof(j.payload -> 'external_boot_authority_v1') = 'object'
        ORDER BY j.id
        FOR UPDATE OF j SKIP LOCKED
    LOOP
        IF EXISTS (
            SELECT 1 FROM public.external_boot_authorities AS authority
            WHERE authority.job_id = v_job.id AND authority.state IN ('allocating', 'current')
        ) THEN
            CONTINUE WHEN NOT EXISTS (
                SELECT 1
                FROM public.external_boot_acknowledged_retry_consumptions AS consumed
                WHERE consumed.job_id = v_job.id AND consumed.claimed_attempt = v_job.attempt
            );
            PERFORM pg_advisory_xact_lock(hashtextextended(
                'kdive:system:' || (v_job.payload #>> '{external_boot_authority_v1,system_id}'),
                2126
            ));
            CONTINUE WHEN NOT public.has_acknowledged_external_boot_no_mutation_head(v_job);
            UPDATE public.external_boot_authorities
            SET state = 'superseded', superseded_at = clock_timestamp()
            WHERE job_id = v_job.id AND state IN ('allocating', 'current');
        END IF;
        UPDATE public.jobs SET state = 'failed', error_category = 'lease_expired'
        WHERE id = v_job.id;
        UPDATE public.runs SET state = 'failed', failure_category = 'lease_expired'
        WHERE id = (v_job.payload #>> '{external_boot_authority_v1,run_id}')::uuid
          AND state IN ('created', 'running');
        RETURN NEXT v_job.id;
    END LOOP;
END
$$;
```

4. Run the Task 1 focused commands; expect the stated green counts. Then run
   `uv run pytest -q -p no:cacheprovider tests/db/test_external_boot_authority_journal_migration.py
   tests/db/test_exhausted_authority_job.py tests/db/test_worker_fence_authority.py` → all pass.
5. Commit: `fix(db): bound the acknowledged-retry grant per budget (#2960)`.

Rollback: forward-only migrations (ADR-0015); a fix lands as a later migration.

## Task 2 — Registration and the ADR-0626 banner

Verification:

- Contract: pinned migration lists. Mode: focused-test. Red: `uv run pytest -q -p
  no:cacheprovider tests/db/test_migrate.py tests/db/test_migration_0091_system_object_sweep_cursors.py
  tests/db/test_migration_0102_build_gc_cursors.py tests/db/test_migration_0115_capture_reap_state.py`
  fails on the list comparisons once 0165 exists. Green after the edit: all pass.
- Contract: ADR-0626 banner. Mode: task-test-not-applicable — a Status-section prose line; `just
  records` checks that only the Status region of the merged record changed.

Steps:

1. In `tests/db/test_migrate.py`, add `"0165",` after each `"0164",` (lines near 290, 1112,
   1550) and `("0165", "0165_bound_acknowledged_retry_grant.sql"),` after the 0164 tuple (near
   364). Add the same tuple after the 0164 tuple in the 0091, 0102, and 0115 test files.
2. In ADR-0626's `## Status` section, after `Accepted (2026-09-06)`, add:
   `> **Amended by [ADR-0711](0711-bound-acknowledged-retry-grant-per-budget.md) (#2960):** a`
   `> claim this ADR granted cannot earn another grant; a job past that bound is dead-lettered.`
3. Run the registration command (green), `just records` after `git fetch origin main`,
   `just schema-guard`, `just migration-order-check`, `just lint`, `just type`; each exits 0.
4. Commit: `test(db): register migration 0165 (#2960)` and
   `docs(adr): note the ADR-0711 amendment on ADR-0626 (#2960)`.

## Spec coverage

| Spec item | Task |
|---|---|
| Scope 1 bound, S1, S3, S5 | Task 1 |
| Scope 2 terminal, S2, S4 | Task 1 |
| Scope 3 ADR banner | Task 2 (ADR-0711 is already on the branch) |
| Scope 4 registration | Task 2 |
