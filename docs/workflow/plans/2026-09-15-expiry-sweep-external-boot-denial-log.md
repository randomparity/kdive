# Expiry sweep says when the external-boot guard refuses a release

## Goal and architecture

Make a refused `→expired` reclaim visible. `sweep_expired_allocations` calls `_expire_one` per
candidate allocation; `_expire_one` holds `conn.transaction()` → `advisory_xact_lock(PROJECT)`
→ `advisory_xact_lock(ALLOCATION)`, re-checks the allocation, runs
`guard_external_boot_release`, and on `ExternalBootDenied` returns `False` silently. One log
call goes in that `except` branch. Design:
[`../specs/2026-09-15-expiry-sweep-external-boot-denial-log-design.md`](../specs/2026-09-15-expiry-sweep-external-boot-denial-log-design.md).

## Tech stack and constraints

Python 3.14, psycopg, pytest, `just`. Global constraints, transcribed from the spec:

- The `PROJECT → ALLOCATION` acquisition order and the scope over which both locks are held are
  identical to the pre-change function. Add no lock, transaction, `await`, or database call
  inside that region, and move none.
- `src/kdive/services/allocation/release.py` is not changed: ADR-0596's refusal is the intended
  outcome on this path. Add no ADR, migration, public response shape, metric, or config key.

Expected implementation size: 15–35 changed lines (S) — one bound `except` clause with a log
call in one file, one extended test, and one new test.

## Task — Log the denial and prove both arms

Files: `src/kdive/reconciler/repairs/allocations.py`,
`tests/services/external_boot/test_allocation_release.py`.

Interfaces: consume `allocation_release.ExternalBootDenied` (`ExternalBootDenied(message, *,
details: dict[str, object], next_actions: list[str], project: str)`, re-exported through
`kdive.services.allocation.release` from `kdive.services.external_boot`), the module-level
`_log = logging.getLogger(__name__)` in `allocations.py`, `sweep_expired_allocations(conn:
AsyncConnection) -> int`, and the `seeded_activation` fixture (`SeedActivation`,
`tests/services/external_boot/conftest.py`). Produce no new name: `_expire_one`'s signature is
unchanged.

Verification:

- Mode: focused-test. Contract: a sweep pass over an allocation whose System carries a
  restricting activation emits one `WARNING` from `kdive.reconciler.repairs.allocations`
  naming the allocation id, and still reclaims 0. Expected red: `caplog.records` is empty, so
  the assertion fails on an empty list. Green:
  `just test-verbose tests/services/external_boot/test_allocation_release.py::test_expiry_retains_an_allocation_needed_for_external_boot_cleanup`
  exits 0.
- Mode: focused-test. Contract: an allocation whose activation is fully cleaned is reclaimed
  and emits no `WARNING` from the repair module; the clean pass keeps its `INFO` line. Expected
  red: a non-empty warning list if the log call is written outside the `except` branch. Green:
  `just test-verbose tests/services/external_boot/test_allocation_release.py::test_expiry_without_a_restricting_activation_logs_no_denial`
  exits 0.
- Mode: task-test-not-applicable. Changed surface: the `async with` header of `_expire_one`
  holding `conn.transaction()` and the two `advisory_xact_lock` calls. Reason: those three
  lines are not edited here, so the two revisions are byte-identical there and no executable or
  structural observation distinguishes them; `tests/adversarial/test_lease_expiry_renew_race.py`
  holds the ordering contract itself.

Steps:

1. In `tests/services/external_boot/test_allocation_release.py`, add
   `import logging` and `import pytest`, and import
   `from kdive.reconciler.repairs import allocations as allocation_repairs`.
2. Give `test_expiry_retains_an_allocation_needed_for_external_boot_cleanup` a
   `caplog: pytest.LogCaptureFixture` parameter. Wrap its
   `assert await sweep_expired_allocations(conn) == 0` in
   `with caplog.at_level(logging.WARNING, logger=allocation_repairs.__name__):`, then assert
   that exactly one captured record has `levelno == logging.WARNING` and that
   `str(allocation_id)` appears in its `getMessage()`.
3. Run the first focused command and confirm it fails on the empty record list.
4. In `src/kdive/reconciler/repairs/allocations.py`, change
   `except allocation_release.ExternalBootDenied:` to
   `except allocation_release.ExternalBootDenied as denied:` and, before the existing
   `return False` and at that branch's existing indentation, add this call:

   ```python
   _log.warning(
       "reconciler: allocation %s not expired — the external-boot release guard refused "
       "it (ADR-0596); retry follows external-boot cleanup: %s %s",
       allocation_id,
       denied,
       denied.details,
   )
   ```

   Keep `return False` on the line after it. Change nothing else in the function.
5. Run the first focused command and confirm it passes.
6. Add `test_expiry_without_a_restricting_activation_logs_no_denial`, modelled on the existing
   test but seeding `cleanup_complete=True` so `get_restricting_for_system` matches nothing and
   the guard admits. Set the allocation `active` with an elapsed lease the same way, run the
   sweep under the same `caplog.at_level(...)` context, and assert it returns 1, the allocation
   reads `expired`, and no captured record has `levelno == logging.WARNING`. The fixture seeds
   no `budgets` row, so `accounting.reconcile` is its documented unmetered no-op.
7. Run the second focused command and confirm it passes, then `just lint`, `just type`,
   `just test-changed`, then `git fetch origin main` and `just records`, then `just ci`. Each
   exits 0.

Acceptance: a refused expiry is attributable in the logs and a clean reclaim is not; the diff
leaves `_expire_one`'s `async with` header and `release.py` untouched. Rollback is a revert.
