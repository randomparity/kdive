# Plan: release after teardown reads the first activation (#3423)

Goal: admit allocation release for a torn-down authority owner exactly when its ownership row
records a first activation.
Architecture: a restricted SECURITY DEFINER SQL function (migration 0183) exposes the boolean;
`torn_down_after_activation` calls it; the admission caller is unchanged.
Spec: `docs/workflow/specs/2026-10-30-first-activation-release-proof-design.md`.
Tech stack: Python 3, psycopg 3 async, PostgreSQL migrations under `src/kdive/db/schema/`,
pytest with testcontainers Postgres.

Expected implementation size: 70–110 changed lines (S) — one migration, one function body, one
test file change plus seven one-line list additions and an ADR paragraph.

## Global Constraints

- Migration number `0183` is campaign-assigned; never renumber.
- Migrations are forward-only (ADR-0015); merged ADR sections are append-only (`just records`).
- Guardrails: `just lint`, `just type`, `just test-changed`, `git fetch origin main && just records`.
- Doc wording: no "critical/robust/comprehensive/elegant".

## File map

| File | Change | Owns |
|---|---|---|
| `src/kdive/db/schema/0183_authority_first_activation_proof.sql` | create | the function and its grants |
| `src/kdive/services/systems/authority_owned.py` | modify | `torn_down_after_activation` calls the function |
| `tests/db/test_release_after_activated_teardown.py` | modify | regression + function tests |
| `tests/db/test_migrate.py`, `tests/db/test_migration_0091_system_object_sweep_cursors.py`, `tests/db/test_migration_0102_build_gc_cursors.py`, `tests/db/test_migration_0115_capture_reap_state.py` | modify | migration windows |
| `docs/adr/0754-receipt-bound-authority-system-discharge.md` | append | amendment |

No ownership transition: `authority_owned.py` already owns the server-side authority reads.

## Task 1: first-activation proof

Interfaces: consumes `_torn_down_owner`, `_admit`, `_scalar` (existing in the test file) and
`authority_role_dsns` / `migrated_url` fixtures (`tests/db/conftest.py`). Produces
`authority_system_torn_down_after_activation(uuid) -> boolean` and keeps
`torn_down_after_activation(conn: AsyncConnection, system_id: UUID) -> bool`.

Verification:
- Mode: focused-test — release admission. `test_an_unclaimed_activation_row_never_admits_release`
  parametrized `live`/`cleaned`; expect red (`cleaned` does not raise; `live` is denied
  with the activation policy's `DENIAL_REASON`). Green:
  `uv run pytest tests/db/test_release_after_activated_teardown.py`.
- Mode: focused-test — function results and grants. `test_first_activation_proof_*`; red:
  `UndefinedFunction`. Same green command.
- Mode: focused-test — migration windows; red: list mismatch. Green:
  `uv run pytest tests/db/test_migrate.py tests/db/test_migration_0091_system_object_sweep_cursors.py tests/db/test_migration_0102_build_gc_cursors.py tests/db/test_migration_0115_capture_reap_state.py`.
- Mode: task-test-not-applicable — ADR amendment prose; no executable consumer. `just records`.

Steps:

1. In the test file, replace `orphan_row: bool = False` with `orphan_row: str | None = None`
   (`"live"` sets the row `preparing`; `"cleaned"` keeps the abandoned, cleaned row), and delete
   the row only when `not activated and orphan_row is None`. Replace the unclaimed-row test with
   one parametrized over `"live"` and `"cleaned"` asserting
   `AUTHORITY_PREACTIVATION_DENIAL_REASON`. Add:
   - `_PROOF = "SELECT authority_system_torn_down_after_activation(%s)"`;
   - a test that, as `kdive_server`, the proof is `True` for an activated torn-down owner and
     `False` for: never-activated torn-down, cleaned orphan row, states `ready`,
     `teardown-requested`, `repair-required`, and `uuid4()`;
   - a test parametrized over roles: `kdive_server`, `kdive_reconciler` execute;
     `kdive_worker`, `kdive_provider_authority` raise `psycopg.errors.InsufficientPrivilege`.
2. Add `("0183", "0183_authority_first_activation_proof.sql")` after the `0182` row in each of
   the four window lists and widen `migrations[-K:]` to `K+1`; add `"0183"` after `"0182"` in
   the three version lists in `tests/db/test_migrate.py`.
3. Run the focused commands; record red.
4. Create the migration:

   ```sql
   -- 0183_authority_first_activation_proof.sql — #3423, ADR-0754 (2026-10-30).
   -- Forward-only (ADR-0015). Allocation release after a torn-down authority System is admitted
   -- on its recorded first activation, not on any activation row: an unclaimed row left by a
   -- claim blocked by teardown is not a first activation.
   CREATE FUNCTION public.authority_system_torn_down_after_activation(p_system_id uuid)
   RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' STABLE AS $$
   BEGIN
       IF NOT (pg_has_role(session_user, 'kdive_server', 'member')
               OR pg_has_role(session_user, 'kdive_reconciler', 'member')) THEN
           RAISE EXCEPTION 'control authority is required' USING ERRCODE = '42501';
       END IF;
       RETURN EXISTS (
           SELECT 1 FROM public.authority_system_ownership
           WHERE system_id = p_system_id
             AND state = 'torn-down' AND first_activation_id IS NOT NULL
       );
   END
   $$;

   REVOKE ALL ON FUNCTION public.authority_system_torn_down_after_activation(uuid)
   FROM PUBLIC, kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness,
        kdive_provider_authority;
   GRANT EXECUTE ON FUNCTION public.authority_system_torn_down_after_activation(uuid)
   TO kdive_server, kdive_reconciler;
   ```

5. Replace the body of `torn_down_after_activation`:

   ```python
   async def torn_down_after_activation(conn: AsyncConnection, system_id: UUID) -> bool:
       """Whether a torn-down authority System recorded its first activation (ADR-0754).

       Reads the ownership row's first activation, not activation rows: a claim blocked by a
       requested teardown can leave a row that was never a first activation (#3423).
       """
       cursor = await conn.execute(
           "SELECT authority_system_torn_down_after_activation(%s)", (system_id,)
       )
       row = await cursor.fetchone()
       return row is not None and row[0] is True
   ```

6. Run the focused commands; expect green. Commit `fix(services): ...`.
7. Controlled fault: in the migration drop `AND first_activation_id IS NOT NULL`, rerun step 1's
   file, expect the `cleaned` case red; `git checkout --` the migration (after the commit).
8. Append to ADR-0754 a `## Amendment (2026-10-30): release reads the first activation` section;
   run `git fetch origin main && just records`, `just lint`, `just type`, `just test-changed`.
   Commit `docs(adr): ...`.

Rollback: forward-only; a revert PR adds a migration dropping the function.
