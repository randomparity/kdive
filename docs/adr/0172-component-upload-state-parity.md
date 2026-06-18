# ADR 0172 — Remove the schema-only `failed` component-upload state

- **Status:** Accepted
- **Date:** 2026-06-18
- **Deciders:** kdive maintainers

## Context

`component_uploads` records a provider-component upload intent and its lifecycle
(`src/kdive/db/provider_component_records.py`, ADR-0065). The lifecycle is a
service-local state machine defined by the `ComponentUploadState` enum, which has
exactly two values: `pending` (an intent exists, awaiting the object) and `finalized`
(the object validated and a `provider_components` row was created).

The SQL `CHECK` on `component_uploads.state` admits a third value the enum does not
define:

```sql
state text NOT NULL CONSTRAINT component_uploads_state_check
    CHECK (state IN ('pending', 'finalized', 'failed')),
```

No code path writes `failed`. `create_component_upload_intent` writes `pending`;
`finalize_component_upload` writes `finalized`. The only writer of `failed` anywhere
in the tree is a test (`tests/db/test_provider_component_records.py`) that manually
`UPDATE`s the column to prove finalization rejects a non-pending row — which means
`failed` exists only at the database and test level, owned by no service code.

Two parity gaps let the phantom value persist undetected:

- `component_uploads_state_check` is absent from
  `tests/db/test_migrate.py::CHECK_ENUMS`, so nothing tied the constraint to the enum.
- `CHECK_ENUMS` only verifies every Python enum value appears in SQL (enum ⊆ SQL). It
  structurally cannot catch an SQL-only extra such as `failed` (a value in SQL but
  absent from the enum). This is the same gap ADR-0171 closed for `run_steps.state`.

## Decision

1. **Remove `failed` from the SQL CHECK.** Forward-only migration
   `0044_component_upload_state_check.sql` (ADR-0015) drops the existing constraint
   and recreates it admitting exactly the enum values:
   `CHECK (state IN ('pending', 'finalized'))`. Removing a value from an existing
   constraint requires the drop+recreate; a plain `ADD CONSTRAINT` cannot narrow it.
   Validation of existing rows cannot fail because no writer ever produced `failed`.

2. **Pin the constraint to the enum bidirectionally.** Register
   `component_uploads_state_check` in `test_migrate.py::CHECK_ENUMS` (the enum ⊆ SQL
   guard every lifecycle table uses) and add a dedicated test asserting the CHECK's
   admitted set *equals* `ComponentUploadState`'s value set — closing the SQL-only-extra
   direction `CHECK_ENUMS` cannot check.

3. **Stop writing a value the service cannot produce in tests.** The test that wrote
   `failed` to prove a non-pending upload rejects finalization is replaced: the
   already-finalized row is the real non-pending lifecycle state, and the idempotent
   replay path already covers it. Finalization still rejects an expired upload with an
   actionable `configuration_error` (an existing, unchanged test).

## Consequences

- `component_uploads.state` can no longer hold a value the enum does not define; the
  column and `ComponentUploadState` are in exact sync, enforced in both directions.
- Adding or removing a `ComponentUploadState` value now requires updating the
  migration; the two parity tests fail otherwise.
- No change to the response envelope, the worker handlers, or any tool surface. The
  removed value was never produced or read by service code, so removing it changes no
  runtime behavior — `finalize_component_upload` already rejects any non-`pending`
  state through its single `state != PENDING` branch.

## Considered & rejected

- **Implement a real `failed` lifecycle state.** This would add a Python `FAILED`
  enum value and a transition path that writes it (e.g. on a checksum mismatch at
  finalization). No caller needs a persisted terminal `failed` row: a mismatch already
  raises `configuration_error` and leaves the row `pending` until its TTL reaps it, and
  no consumer queries for `failed` uploads. Adding state no workflow reads is
  speculative surface; the issue prefers removal unless a concrete failure workflow
  needs it, and none does.
- **Add the CHECK to `CHECK_ENUMS` without removing `failed`.** `CHECK_ENUMS` only
  asserts enum ⊆ SQL, so it would pass with `failed` still in SQL and never surface the
  drift. The bidirectional test is what catches the extra; with it, leaving `failed`
  in SQL would fail the build. Removing the value is the fix the test then guards.
- **`DROP CONSTRAINT ... ` then `ADD ... NOT VALID`.** The only writers of
  `component_uploads.state` produce `pending`/`finalized`, so a validating recreate
  cannot fail on real data, matching every other CHECK migration in the tree. `NOT
  VALID` would add ceremony for a validation that cannot fail.
