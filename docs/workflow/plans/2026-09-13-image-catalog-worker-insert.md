# Image-build worker image-catalog INSERT

Add the smallest migration and test evidence needed for the existing image-build publish handler
to insert catalog rows as `kdive_worker`. PostgreSQL migrations define the authority, the ADR-0653
inventory verifies its effective state, and an integration-style handler test exercises the role.

Tech stack: PostgreSQL migrations, Python 3.14, pytest, psycopg.

## Global Constraints

- Scope is migration 0154 and image-build worker-role evidence only.
- The grant adds only INSERT; existing SELECT and UPDATE authority remains unchanged, and no
  DELETE, schema, or role-membership authority is added.
- ADR-0653 remains the governing decision; no new architectural decision is introduced.

Expected implementation size: 28–42 changed lines (M) — one SQL grant, two JSON declarations,
one worker-role test adaptation, seven migration-tail assertions across four modules, and one
privilege-matrix entry.

## File map

- `src/kdive/db/schema/0154_worker_image_catalog_insert.sql`: grant the single required table
  privilege.
- `tests/jobs/handlers/worker_role_inventory.json`: declare the direct write for ADR-0653's guard.
- `tests/jobs/worker_write_baseline.json`: replace the known leak with migration evidence.
- `tests/jobs/test_image_build_handler.py`: invoke the existing success flow as `kdive_worker`.
- `tests/db/test_migrate.py`, `tests/db/test_migration_0102_build_gc_cursors.py`,
  `tests/db/test_migration_0091_system_object_sweep_cursors.py`, and
  `tests/db/test_migration_0115_capture_reap_state.py`: advance each discovered migration tail.
- `tests/db/test_worker_fence_authority.py`: add `image_catalog` only to
  `_WORKER_MUTATIONS["INSERT"]`.

## Task 1: declare and prove the privilege

### Interfaces

Consumes `kdive_worker`, `public.image_catalog`, and the existing `image_build_handler` signature.
Provides the effective INSERT authority consumed by the catalog guard and worker-role proof.

### Verification

- Contract: effective direct INSERT grant. Mode: focused-test. Red observation: without migration
  0154, `test_declared_worker_writes_are_covered_by_migrated_catalog` reports the image-build
  declaration missing INSERT coverage. Green command: `just test-verbose
  tests/jobs/test_worker_write_grants.py::test_declared_worker_writes_are_covered_by_migrated_catalog`.
- Contract: image-build publish succeeds as worker. Mode: focused-test. Red observation: without
  migration 0154, the handler's catalog insert raises PostgreSQL insufficient privilege. Green
  command: `just test-verbose tests/jobs/test_image_build_handler.py::test_worker_role_handler_builds_validates_publishes_registered`.
- Contract: migration history has the expected 0154 tail. Mode: focused-test. Red observation:
  adding migration 0154 without advancing seven hard-coded 0153 expectations across four modules
  makes their equality assertions fail. Green command: `just test-verbose tests/db/test_migrate.py tests/db/test_migration_0102_build_gc_cursors.py tests/db/test_migration_0091_system_object_sweep_cursors.py tests/db/test_migration_0115_capture_reap_state.py`.
- Contract: worker authority adds only INSERT on image_catalog. Mode: focused-test. Red observation:
  before the matrix entry is added, the migrated worker privilege matrix expects INSERT to be
  absent; the matrix retains existing SELECT and UPDATE and rejects DELETE, REFERENCES, TRIGGER,
  and TRUNCATE. Green command: `just test-verbose tests/db/test_worker_fence_authority.py::test_runtime_roles_receive_data_access_without_crossing_fence_authority`.
- Contract: baseline evidence remains structurally valid. Mode: focused-test. Red observation: a
  malformed migration evidence path, line, text, verdict, or confirmed-leaks list fails the
  baseline validator. Green command: `just test-verbose tests/jobs/test_worker_write_baseline.py::test_committed_baseline_is_complete_and_valid`.

### Steps

1. Add `GRANT INSERT ON TABLE public.image_catalog TO kdive_worker;` in migration 0154.
2. Add the sorted direct INSERT declaration to the worker-write inventory.
3. Update the baseline record's migration line/text, verdict, and confirmed-leak list.
4. Stage a job with the migration owner, reconnect through `authority_role_dsns("kdive_worker")`,
   and assert the existing publish result remains registered and stored.
5. Advance the seven hard-coded migration-history expectations across four modules with
   `("0154", "0154_worker_image_catalog_insert.sql")`.
6. Add `image_catalog` only to `_WORKER_MUTATIONS["INSERT"]`, preserving every other role and
   operation set.
7. Run all focused checks above, then `just lint` and `just type`.

### Acceptance criteria

Only the intended INSERT privilege is added; existing SELECT and UPDATE authority is retained;
migration history, the migrated guard, baseline structure, full worker privilege matrix, and the
actual worker-role publish path all pass.

### Rollback

Reverting the migration and matching declarations restores the former authority boundary.
