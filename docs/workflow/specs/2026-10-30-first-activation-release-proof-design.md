# Release after teardown reads the first activation (#3423)

## Problem

`torn_down_after_activation` (`src/kdive/services/systems/authority_owned.py`) admits allocation
release for a `torn-down` authority owner when any `external_boot_activations` row exists for the
System. A first-activation claim blocked by a requested teardown can leave a row that was never
claimed. Once that row is cleaned, a never-activated owner reads as activated and its allocation
is released without the consumed absence receipt ADR-0754 requires. The authoritative fact is
`authority_system_ownership.first_activation_id` (migration 0149), which the server role cannot
read directly.

## Design

Migration `0183_authority_first_activation_proof.sql` adds one function on the 0171 template:

```sql
public.authority_system_torn_down_after_activation(p_system_id uuid) RETURNS boolean
-- plpgsql, SECURITY DEFINER, STABLE, SET search_path = ''
-- raises 42501 unless session_user is a member of kdive_server or kdive_reconciler
-- true exactly when the ownership row exists, state = 'torn-down' and
-- first_activation_id IS NOT NULL; false otherwise, including an unknown System
```

Privileges: `REVOKE ALL` from `PUBLIC`, `kdive_server`, `kdive_worker`, `kdive_reconciler`,
`kdive_lifecycle_witness`, `kdive_provider_authority`; `GRANT EXECUTE` to `kdive_server` and
`kdive_reconciler` (the reconciler's lease-expiry sweep and orphan reaper share the admission).

`torn_down_after_activation` becomes a single `SELECT` of that function, like
`preactivation_absence_is_proven`; its binding read and the activation-row query are deleted.
The caller (`services/external_boot/admission.py`) is unchanged. State and first activation come
from one row in one statement, so no window separates them.

ADR-0754 gains an appended amendment: release after teardown is admitted on the recorded first
activation; the activation-row limit stated by the 2026-10-28 amendment no longer applies. The
merged sections stay unchanged (the record is append-only).

## Failure model

1. Actors and deployments: the MCP server (`kdive_server`) handling `allocations.release`; the
   reconciler (`kdive_reconciler`) releasing allocations; any deployment running migrations.
2. Invariants and assets: a never-activated torn-down System's allocation is released only on
   its consumed absence receipt; an activated torn-down System's allocation stays releasable
   (#3382); raw authority tables stay unreadable to the server; no other operation leaves the
   fence.
3. Accepted failure classes: none.
4. Covered elsewhere: the receipt proof (0171, ADR-0754); the activation policy for live rows
   (`admission.py` `_ADMITTED`); the ownership state CHECK that pairs `activated` with a first
   activation (0149).

## Success

- A `torn-down` owner with no first activation is denied `allocation_release` with
  `authority_preactivation_mutation_fenced` when its only activation row is cleaned, and when it
  is live.
- A `torn-down` owner with a first activation is admitted for `allocation_release`; the three
  other operations tested today stay fenced.
- The function returns false for `ready`, `teardown-requested`, `repair-required`, and an unknown
  System; `kdive_worker` and `kdive_provider_authority` cannot execute it.
- The four migration-window tests and the three version lists in `tests/db/test_migrate.py` carry
  `0183`.

## Validation

- `focused-test`: release admission. `tests/db/test_release_after_activated_teardown.py`, the
  unclaimed-row test parametrized over a live and a cleaned row, asserting the fence reason. Red
  before the change: the cleaned row is admitted, the live row is denied with the activation
  reason. Green: `uv run pytest tests/db/test_release_after_activated_teardown.py`.
- `focused-test`: function grants and results, in the same file. Red: the function is undefined.
- `focused-test`: migration registration, in `tests/db/test_migrate.py` and the three
  `tests/db/test_migration_0*` window tests. Red: the list lacks `0183`.
- `task-test-not-applicable`: the ADR amendment is prose with no executable consumer;
  `just records` checks its append-only shape.
