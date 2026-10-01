# Unrouted preparing-activation teardown (#3017) — plan

Goal: `systems.teardown` routes a `preparing` activation that has no authority row by its
activate job's marker. Architecture: one resolver body replaced in migration 0167; no Python
change; tests in the existing integration file. Spec:
[design](../specs/2026-09-30-unrouted-preparing-teardown-3017-design.md).

Expected implementation size: 220–300 changed lines (M) — one ~50-line migration, ~200 lines of
integration tests (route, end-to-end, refusal arms and helpers), seven ledger lines plus four
widened tail windows.

## Global Constraints

- Python 3.14, `uv`; psycopg 3. No new dependency.
- Migrations are forward-only (ADR-0015); number `0167` is assigned; no other number.
- ADR-0620 is append-only; its 2026-09-30 #3017 amendment is already on the branch.
- Gates: `just lint`, `just type`, `just test-verbose <paths>`, `just records`, pre-push
  `just ci > <file> 2>&1 < /dev/null`.

## Task 0 — commit the reachability proof

File: `tests/integration/test_external_boot_unrouted_teardown.py` (exists, untracked; asserts
`external_boot_teardown_authority_unresolved` after `boot_run` + `cancel_job`). Verification:
`Mode: focused-test` — green on main's schema: `just test-verbose
tests/integration/test_external_boot_unrouted_teardown.py`. Commit it unchanged:
`test(external-boot): prove the unrouted preparing teardown refusal`.

## Task 1 — route by the activate marker

Files: create `src/kdive/db/schema/0167_unrouted_preparing_teardown_route.sql`; modify
`tests/integration/test_external_boot_unrouted_teardown.py`, `tests/db/test_migrate.py`,
`tests/db/test_migration_0091_system_object_sweep_cursors.py`,
`tests/db/test_migration_0102_build_gc_cursors.py`, `tests/db/test_migration_0115_capture_reap_state.py`.

Interfaces: consumes the 0147 resolver (columns `activation_id, run_id, plan_identity,
provider_kind, authority_instance`), `boot_run(pool, ctx, run_id, *, resolver)`,
`cancel_job(pool, ctx, job_id)`, `teardown_system(pool, ctx, system_id, *, resolver)`, and from
`tests/integration/external_boot_support.py`: `seed_public_external_boot`,
`configure_external_boot`, `fetch_one`, `PreparingProvider`. Provides to Tasks 2–3:
`_boot(pool, resolver) -> tuple[str, str]` — `seed_public_external_boot`, then `boot_run` with
`runs_support.ctx()`, assert `queued`, return `(system_id, activate_job_id)`; and
`_teardown(pool, system_id, resolver)` — `teardown_system` with `runs_support.ctx(Role.ADMIN)`.

Verification:
- `Mode: focused-test` — route arm `test_teardown_routes_by_the_activate_marker[canceled|queued]`.
  Red before the migration: `status == "error"`. Green: `just test-verbose
  tests/integration/test_external_boot_unrouted_teardown.py`.
- `Mode: focused-test` — migration ledger. Red: the tail-window tests fail on `0167`. Green:
  `just test-verbose tests/db/test_migrate.py tests/db/test_migration_0091_system_object_sweep_cursors.py
  tests/db/test_migration_0102_build_gc_cursors.py tests/db/test_migration_0115_capture_reap_state.py`.

Steps:
1. Replace the reachability test with the route arm: parametrize `activate_job` over `canceled`
   (`cancel_job` on the activate job) and `queued` (no call); `_teardown`; assert `queued`, the
   teardown marker's `provider_kind`, `authority_instance`, `activation_id` equal the activate
   marker's (`jobs.payload -> 'external_boot_authority_v1'`), purpose and operation `teardown`,
   reservation `pending`. Run; expect the red above.
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

3. Ledgers: append `("0167", "0167_unrouted_preparing_teardown_route.sql")` to the tuple lists
   (test_migrate.py:368 and each `test_migration_0*` file) and widen their slices by one
   (`[-65:]`→`[-66:]`, `[-61:]`→`[-62:]`, `[-48:]`→`[-49:]`); append `"0167"` to the three
   version lists in test_migrate.py (after lines 292, 1118, 1558). Run both green commands.
4. `just lint`, `just type`; commit `fix(external-boot): route a never-authorized preparing teardown`.

## Task 2 — the teardown completes and the allocation releases

File: the same integration module. Interfaces: `_boot`, `_teardown`; `Worker(pool, registry, *,
worker_id, incarnation_credential, secret_registry)`; `build_operations(ExternalBootHandlerPorts(
resolver=, incarnation_credential=CREDENTIAL, secret_registry=SecretRegistry(),
acknowledger=RecordingAcknowledger(authority_role_dsns("kdive_provider_authority")),
teardown_executor=RecordingTeardownExecutor(conn)))` registered for `TEARDOWN` through
`route_marked(operations, must_not_run)`, as `tests/integration/test_external_boot_job_lifecycle.py`
`_registry` does; `register_incarnation`; `release_allocation(pool, ctx, allocation_id)`.

Verification: `Mode: focused-test` — `test_routed_teardown_completes_and_releases` (activate job
canceled; a queued one would be claimed first by `created_at`). Red before 0167: the teardown
response is `error`. Green: the Task 1 command.

Steps:
1. After the Task 1 route, `worker.run_once(<teardown job's dispatch_lane>)`; assert the job
   `succeeded`, activation and System `torn_down`, zero `external_boot_reservations` and zero
   `external_boot_reservation_releases` rows for the activation, the only authority row is the
   teardown one, one recorded teardown call, `PreparingProvider.phases` empty; then
   `release_allocation` returns `released`. The ports also need `artifact_store=INERT_OBJECT_STORE`.
2. Green; commit `test(external-boot): drive the routed preparing teardown to release`.

## Task 3 — kept refusals

File: the same module. Verification: `Mode: focused-test` —
`test_teardown_still_refuses_an_ambiguous_or_authorized_activation[second_activate_job|allocating|superseded]`.
Green before and after 0167, so bite by controlled fault: drop the `NOT EXISTS` clause
(allocating and superseded turn `queued`), then add `LIMIT 1` to the second branch
(second_activate_job turns `queued`); restore with `git checkout -- <migration>` after committing.

Steps:
1. After `_boot` and `cancel_job`: either copy the activate job (`INSERT INTO jobs (id, kind,
   payload, state, max_attempts, authorizing, dedup_key) SELECT %s, kind, payload, 'canceled',
   max_attempts, authorizing, %s FROM jobs WHERE id = %s`) or `_seed_authority(conn, job_id,
   state)`: one `worker_incarnations` row (`'docker', '{}'::jsonb, b"1" * 32, 4`) and one
   `external_boot_authorities` row selected from the job, activation and System (purpose and
   operation `activate`, generation 1, the given `state`, `superseded_at = now()` only for
   `superseded`). Assert `status == "error"`, reason `external_boot_teardown_authority_unresolved`.
2. Green; faults red then restored; commit `test(external-boot): keep the unresolved-route refusals`.

## Requirement map

Criterion 1 → Task 0; Success 1 → Task 1; Success 2 → Task 2; Success 3 → Task 3; criterion 5
(ADR amendment) → design phase, `just records`.
