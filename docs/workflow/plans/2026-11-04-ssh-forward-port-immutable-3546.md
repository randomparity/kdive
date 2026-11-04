# Pin the authority System SSH forward port: implementation plan (#3546)

Goal: a non-NULL `authority_system_ownership.ssh_forward_port` cannot change.
Architecture: one forward-only migration replaces the existing trigger function; no caller changes.
Tech stack: PostgreSQL plpgsql, pytest with the disposable Postgres fixtures.

Expected implementation size: 60–90 changed lines (S) — one migration, one test file, one list entry in each of seven migration list tests.

## Global Constraints

Migration number 0191 exactly. Do not edit 0149 or 0189 (`just schema-guard`). No other column
gains a rule. Spec: `docs/workflow/specs/2026-11-04-ssh-forward-port-immutable-3546-design.md`.

## File map

- Create `src/kdive/db/schema/0191_authority_system_ssh_forward_immutable.sql`: redefines
  `public.reject_authority_system_binding_update()`.
- Create `tests/db/test_migration_0191_authority_system_ssh_forward_immutable.py`.
- Modify the list tests: `tests/db/test_migrate.py` (three version lists at the `"0189",` entries,
  and the `migrations[-86:]` window, to `-87`), `tests/db/test_migration_0091_system_object_sweep_cursors.py`
  and `tests/db/test_migration_0102_build_gc_cursors.py` (`-82` to `-83`),
  `tests/db/test_migration_0115_capture_reap_state.py` (`-69` to `-70`). Each gains the row
  `("0191", "0191_authority_system_ssh_forward_immutable.sql")` or the bare `"0191"`.

## Task 1: the immutability clause

Interfaces: consumes `_seed_direct_authority_case(conn, operation=..., ownership_state=...)` from
`tests/db/authority_system_support.py`, whose result carries `"system_id"`, and the `migrated_url`
fixture. Provides the exception text `authority System SSH forward port is immutable`.

Verification:

- Mode: focused-test. Contract: value-to-other and value-to-NULL raise; same-value, state-only and
  NULL-to-value pass. Case: the new test file. Expected red: the two raise cases report `DID NOT
  RAISE` and the registration test fails. Green:
  `uv run python -m pytest tests/db/test_migration_0191_authority_system_ssh_forward_immutable.py tests/db/test_migration_0189_authority_system_ssh_forward.py -q`
- Mode: focused-test. Contract: the migration list. Green:
  `uv run python -m pytest tests/db/test_migrate.py tests/db/test_migration_0091_system_object_sweep_cursors.py tests/db/test_migration_0102_build_gc_cursors.py tests/db/test_migration_0115_capture_reap_state.py -q`

Steps:

1. Write the test file with this body for each case, using a `_set_port(conn, system_id, port)`
   helper around `UPDATE authority_system_ownership SET ssh_forward_port=%s WHERE system_id=%s`:

   ```python
   @pytest.mark.parametrize("rewrite", [41235, None])
   def test_0191_a_set_port_cannot_change(migrated_url: str, rewrite: int | None) -> None:
       with psycopg.connect(migrated_url) as conn:
           case = _seed_direct_authority_case(
               conn, operation="provision", ownership_state="provisioning"
           )
           _set_port(conn, case["system_id"], 41234)
           with pytest.raises(psycopg.errors.RaiseException, match="port is immutable"):
               _set_port(conn, case["system_id"], rewrite)
   ```

   Pass cases: rewrite 41234 over 41234; `SET state='ready'` with a port set; NULL to 41234.
2. Run the first green command. Expect the stated red.
3. Write the migration: the header comment, then `CREATE OR REPLACE FUNCTION
   public.reject_authority_system_binding_update()` with the 0149 lines 130-153 body and the
   clause from the spec after the first-activation clause, before `NEW.updated_at`.
4. Update the list tests in the file map. Run both green commands; expect pass.
5. `just lint`, `just type`, `just schema-guard`, `just migration-order-check`. Commit.
6. Controlled fault: after the commit, delete the clause from the migration, run the new test file,
   observe the two raise cases fail, restore with `git checkout -- src/kdive/db/schema/0191_authority_system_ssh_forward_immutable.sql`.

Acceptance: the spec Validation list passes; `git diff origin/main --stat` shows no change to 0149
or 0189. Rollback: forward-only; a defect is fixed by a later migration.
