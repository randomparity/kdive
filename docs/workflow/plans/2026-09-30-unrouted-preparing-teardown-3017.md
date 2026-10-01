# Unrouted preparing-activation teardown (#3017) — plan

Goal: `systems.teardown` routes a `preparing` activation that has no authority row by its
activate job's marker. Architecture: one resolver body replaced in migration 0167; no Python
change; tests in the existing integration file. Spec:
[design](../specs/2026-09-30-unrouted-preparing-teardown-3017-design.md).

Expected implementation size: 150–220 changed lines (M) — one ~45-line migration, ~110 lines of
integration tests, four one-line migration-ledger entries.

## Global Constraints

- Python 3.14, `uv`; psycopg 3. No new dependency.
- Migrations are forward-only (ADR-0015); number `0167` is assigned; no other number.
- ADR-0620 is append-only; its 2026-09-30 #3017 amendment is already on the branch.
- Gates: `just lint`, `just type`, `just test-verbose <paths>`, `just records`, pre-push
  `just ci > <file> 2>&1 < /dev/null`.

## Task 1 — route by the activate marker

Files: create `src/kdive/db/schema/0167_unrouted_preparing_teardown_route.sql`; modify
`tests/integration/test_external_boot_unrouted_teardown.py`, `tests/db/test_migrate.py` (both
version lists), `tests/db/test_migration_0091_system_object_sweep_cursors.py`,
`tests/db/test_migration_0102_build_gc_cursors.py`, `tests/db/test_migration_0115_capture_reap_state.py`.

Interfaces: consumes `resolve_external_boot_system_teardown_dispatch_binding(uuid)` (0147;
columns `activation_id, run_id, plan_identity, provider_kind, authority_instance`), `boot_run`,
`cancel_job`, `teardown_system(pool, ctx, system_id, *, resolver)`, `seed_public_external_boot`,
`configure_external_boot`, `fetch_one`, `PreparingProvider` (`tests/integration/external_boot_support.py`).
Provides `_boot(pool, resolver) -> tuple[str, str]` (system id, activate job id) and
`_teardown(pool, system_id, resolver)` to Task 2.

Verification:
- `Mode: focused-test` — contract: route arm. Test
  `test_teardown_routes_by_the_activate_marker[canceled|queued]`. Red before the migration:
  `response.status == "error"` (`external_boot_teardown_authority_unresolved`). Green:
  `just test-verbose tests/integration/test_external_boot_unrouted_teardown.py`.
- `Mode: focused-test` — contract: migration ledger. Red: `test_migrate.py` version-list
  assertions list `0167` as unexpected. Green: `just test-verbose tests/db/test_migrate.py
  tests/db/test_migration_0091_system_object_sweep_cursors.py
  tests/db/test_migration_0102_build_gc_cursors.py tests/db/test_migration_0115_capture_reap_state.py`.

Steps:
1. Replace the reachability test with the route arm (helpers shown in Task 2's file content):
   parametrize `activate_job` over `canceled` (call `cancel_job`) and `queued` (no call); call
   `_teardown`; assert `queued`, the teardown marker's `provider_kind`, `authority_instance`,
   `activation_id` equal the activate marker's, purpose/operation `teardown`, reservation
   `pending`. Run; expect the red above.
2. Write the migration:

```sql
-- 0167_unrouted_preparing_teardown_route.sql — #3017, ADR-0620 amendment (2026-09-30).
-- Forward-only (ADR-0015). A `preparing` activation whose activate job never allocated authority
-- has no authority row, so the 0147 resolver returned no route. Its durable route is the activate
-- marker the server minted under the System lock with the activation. CREATE OR REPLACE keeps the
-- signature, owner and the 0147 grants.
CREATE OR REPLACE FUNCTION public.resolve_external_boot_system_teardown_dispatch_binding(
    p_system_id uuid
) RETURNS TABLE (activation_id uuid, run_id uuid, plan_identity text, provider_kind text,
                 authority_instance text)
LANGUAGE sql SECURITY DEFINER SET search_path = '' STABLE AS $$
    WITH newest AS (
        SELECT activation.id, activation.run_id, activation.plan_identity, activation.state
        FROM public.external_boot_activations AS activation
        WHERE activation.system_id = p_system_id
        ORDER BY activation.created_at DESC, activation.id DESC
        LIMIT 1
    )
    SELECT newest.id, newest.run_id, newest.plan_identity,
           authority.provider_kind, authority.authority_instance
    FROM newest
    JOIN LATERAL (
        SELECT a.provider_kind, a.authority_instance
        FROM public.external_boot_authorities AS a
        WHERE a.activation_id = newest.id
          AND a.system_id = p_system_id
          AND a.run_id = newest.run_id
          AND a.plan_identity = newest.plan_identity
          AND a.state IN ('current', 'retired')
        ORDER BY a.generation DESC
        LIMIT 1
    ) AS authority ON true
    WHERE newest.state <> 'torn_down'
    UNION ALL
    SELECT newest.id, newest.run_id, newest.plan_identity,
           job.payload #>> '{external_boot_authority_v1,provider_kind}',
           job.payload #>> '{external_boot_authority_v1,authority_instance}'
    FROM newest
    JOIN public.jobs AS job
      ON job.kind = 'boot'
     AND job.payload ->> 'run_id' = newest.run_id::text
     AND job.payload #>> '{external_boot_authority_v1,activation_id}' = newest.id::text
     AND job.payload #>> '{external_boot_authority_v1,run_id}' = newest.run_id::text
     AND job.payload #>> '{external_boot_authority_v1,system_id}' = p_system_id::text
     AND job.payload #>> '{external_boot_authority_v1,plan_identity}' = newest.plan_identity
     AND job.payload #>> '{external_boot_authority_v1,purpose}' = 'activate'
    WHERE newest.state = 'preparing'
      AND NOT EXISTS (
          SELECT 1 FROM public.external_boot_authorities AS a WHERE a.activation_id = newest.id
      )
$$;
```

3. Append `("0167", "0167_unrouted_preparing_teardown_route.sql")` to each ledger tuple list and
   `"0167"` to `test_migrate.py`'s version list. Run both green commands.
4. `just lint`, `just type`; commit `fix(external-boot): route a never-authorized preparing teardown`.

## Task 2 — kept refusals

File: `tests/integration/test_external_boot_unrouted_teardown.py` (same module as Task 1).

Interfaces: consumes `_boot`, `_teardown` from Task 1.

Verification:
- `Mode: focused-test` — contract: ambiguous or authorized activation still refuses. Test
  `test_teardown_still_refuses_an_ambiguous_or_authorized_activation[second_activate_job|allocating|superseded]`.
  These pass before and after 0167, so the red observation is a controlled fault: drop the
  `NOT EXISTS` clause (superseded/allocating turn `queued`) and add `LIMIT 1` to the second branch
  (second_activate_job turns `queued`); revert both. Green: the Task 1 command.

Steps:
1. Add the arm: after `_boot` and `cancel_job`, either insert a copy of the activate job
   (`INSERT INTO jobs (id, kind, payload, state, max_attempts, authorizing, dedup_key) SELECT
   %s, kind, payload, 'canceled', max_attempts, authorizing, %s FROM jobs WHERE id = %s`) or an
   authority row via `_seed_authority(conn, job_id, state)`: one `worker_incarnations` row
   (`'docker', '{}'::jsonb, b"1" * 32, 4`) and one `external_boot_authorities` row selected from
   the job, activation and System (purpose and operation `activate`, `state` given,
   `superseded_at = now()` only for `superseded`). Assert `status == "error"` and
   `data["reason"] == "external_boot_teardown_authority_unresolved"`.
2. Run green; run the controlled faults; revert; commit `test(external-boot): keep the
   unresolved-route refusals for #3017`.

## Requirement map

Spec Success 1 → Task 1; Success 2 → Task 2; Success 3 → existing
`test_teardown_of_a_preparing_activation_skips_preparation[pending-pending_system_teardown-0]`
(run in Task 1's focused set); ADR amendment → design phase, `just records`.
