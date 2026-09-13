# Image-build worker image-catalog INSERT

Add the smallest migration and test evidence needed for the existing image-build publish handler
to insert catalog rows as `kdive_worker`. PostgreSQL migrations define the authority, the ADR-0653
inventory verifies its effective state, and an integration-style handler test exercises the role.

Tech stack: PostgreSQL migrations, Python 3.14, pytest, psycopg.

## Global Constraints

- Scope is migration 0154 and image-build worker-role evidence only.
- The grant is table-specific and operation-specific: no UPDATE, DELETE, SELECT, schema, or role
  membership changes are added.
- ADR-0653 remains the governing decision; no new architectural decision is introduced.

Expected implementation size: 16–28 changed lines (M) — one SQL grant, two JSON declarations,
and one worker-role test adaptation.

## File map

- `src/kdive/db/schema/0154_worker_image_catalog_insert.sql`: grant the single required table
  privilege.
- `tests/jobs/handlers/worker_role_inventory.json`: declare the direct write for ADR-0653's guard.
- `tests/jobs/worker_write_baseline.json`: replace the known leak with migration evidence.
- `tests/jobs/test_image_build_handler.py`: invoke the existing success flow as `kdive_worker`.

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

### Steps

1. Add `GRANT INSERT ON TABLE public.image_catalog TO kdive_worker;` in migration 0154.
2. Add the sorted direct INSERT declaration to the worker-write inventory.
3. Update the baseline record's migration line/text, verdict, and confirmed-leak list.
4. Stage a job with the migration owner, reconnect through `authority_role_dsns("kdive_worker")`,
   and assert the existing publish result remains registered and stored.
5. Run the focused tests, then `just lint` and `just type`.

### Acceptance criteria

Only the intended INSERT privilege is granted; the migrated guard, baseline structure, and actual
worker-role publish path all pass.

### Rollback

Reverting the migration and matching declarations restores the former authority boundary.
