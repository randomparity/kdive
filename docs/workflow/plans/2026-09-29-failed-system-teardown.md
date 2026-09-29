# Failed-System teardown implementation plan (#2908)

**Goal:** a teardown job for a `failed` System reclaims provider leftovers and succeeds without a
state transition. **Architecture:** one guard change in `teardown_handler`; spec
[`2026-09-29-failed-system-teardown-design.md`](../specs/2026-09-29-failed-system-teardown-design.md).
**Expected implementation size:** 80–140 changed lines (handler 3–6, tests 70–110, ADR
amendment 10–20).

## File map

- `src/kdive/jobs/handlers/systems.py` — `teardown_handler` state guard.
- `tests/adversarial/test_provider_state_races.py` — two DB-backed tests.
- `docs/adr/0435-reclaim-failed-provision-artifacts.md` — dated amendment.

## Task 1 — failing tests

Add to `tests/adversarial/test_provider_state_races.py`, reusing `_pool`, `_seed_system`,
`_enqueue`, `_system_state`, `_TrackingProvisioner`, `provider_resolver`, `INERT_OBJECT_STORE`:

1. `test_teardown_queued_behind_failed_provision_reclaims_and_stays_failed`: seed `provisioning`;
   enqueue provision and teardown; a provisioner whose `provision` adds its domain to `live` then
   raises `CategorizedError(PROVISIONING_FAILURE)`, and a recording snapshotter; run
   `provision_handler` (expect the terminal raise), then `teardown_handler`. Assert the return is
   the System id, state `failed`, `live == set()`, the snapshotter's `delete_all` saw the domain,
   and no `audit_log` row with a `tearing_down` transition for the System.
2. `test_failed_system_provider_teardown_fault_stays_retryable`: seed `failed` with its domain in
   `live`; a provisioner whose first `teardown` raises `CategorizedError(INFRASTRUCTURE_FAILURE)`.
   First run raises that error (not terminal, `retryable_category` true), state `failed`, domain
   still live; second run returns the id, state `failed`, `live == set()`.

Run `just test-verbose tests/adversarial/test_provider_state_races.py` and observe both red with
`IllegalTransition`.

## Task 2 — handler guard

In `teardown_handler`, change the guard to skip the `tearing_down` move when the System is
`tearing_down` or in `TERMINAL_SYSTEM_STATES` (import from `kdive.domain.lifecycle.rules`). Update
the docstring to say a terminal System is reclaimed without a transition. Re-run the focused tests
(green), then `just lint` and `just type`.

## Task 3 — ADR-0435 amendment

Append `### Amendment (2026-09-29): teardown of a failed System reclaims without a transition
(#2908)` stating the contract, that `failed -> torn_down` stays illegal, that obligation
discharge stays with ADR-0652, and that it replaces the same premise repeated in ADR-0441.
Run `just lint`.

## Verification

`just test-verbose tests/adversarial/test_provider_state_races.py tests/jobs/handlers/`, then
`just lint`, `just type`; the pre-push hook runs `just ci`.
