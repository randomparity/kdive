# Lease expiry holds a System with external-boot history (#3599)

Issue #3599. Decision record: the 2026-11-15 amendment to
[ADR-0816](../../adr/0816-stranded-activation-settlement.md).

## Problem

`_expire_one` (`src/kdive/reconciler/repairs/allocations.py`) checks the external-boot admission
guard and `settled_system_awaiting_teardown`. A completed activation (`recovered`, `abandoned` or
`torn_down` with `cleanup_complete`) passes the guard, and an unsettled one passes the settled
check, so the allocation moves to `expired` while its System is not `torn_down`.
`repair_orphaned_systems` then enqueues an ordinary teardown, and `teardown_handler` refuses it
(`external_boot_teardown_not_supported`) for any unsettled latest activation. The System is never
torn down by the reconciler, and the operator's `systems.teardown` routes to the authority, which
needs the allocation the expiry ended (#2966). `allocations.release` holds the same allocation
through `_require_system_teardown`.

## Decision

Expiry applies the release fence. The ADR amendment records why the full fence, not the narrowed
one: the narrowed fence joined with the existing settled fence is the full fence.

- `src/kdive/services/allocation/release.py`: rename `_require_system_teardown` to
  `require_system_teardown` (public; same signature, SQL, message and details). Its two release
  callers use the new name.
- `_expire_one`: inside the existing `try`, call `require_system_teardown(conn, allocation_id,
  project=project)` after `guard_external_boot_release`. The existing `except ExternalBootDenied`
  logs the skip and returns `False`. Its warning text changes from "the external-boot admission
  guard refused the release" to "the external-boot release fence refused the release", because it
  now covers both checks; the reason and System come from `denied.details` as today.
- Remove the `settled_system_awaiting_teardown` block from `_expire_one` and the function from
  `src/kdive/services/external_boot/settlement.py`; it has no other caller. Its real-database test
  under the `kdive_reconciler` role is replaced by one calling `require_system_teardown` under
  that role.

Lock order is unchanged: PROJECT, ALLOCATION, then the System locks the guard takes; the fence is
one read inside that transaction.

## Success

1. An expired-lease allocation whose System has a completed, unsettled activation
   (`recovered` or `abandoned`, each with `cleanup_complete`) and is not `torn_down` stays in its
   state after `sweep_expired_allocations`, which returns 0 and logs one warning naming the
   allocation, the System and `external_boot_system_teardown_required`.
2. Once that System is `torn_down`, the next sweep expires the allocation and logs no warning.
3. A settled System that is not `torn_down` stays held (the #3551 behaviour), now through the
   same fence.
4. A restricting activation is still refused by the admission guard with `DENIAL_REASON`.
5. `require_system_teardown` runs under the `kdive_reconciler` role.

## Failure model

1. **Actors and deployments** — the reconciler process (`kdive_reconciler` role) on every
   deployment; operators running `systems.teardown` / `ops.force_teardown`.
2. **Invariants and assets at stake** — an allocation is not ended while a System it owns may
   still hold an authority domain; the expiry and release fences agree.
3. **Accepted failure classes** — a held allocation keeps its host-cap slot until the System is
   torn down: bounded, logged every pass, and already true of release. A `failed` System with
   history is held too (ADR amendment).
4. **Covered elsewhere** — ordinary teardown accepting completed unsettled histories (operator
   non-goal); settled-System resolver routing (#3598); stranded boot job (#3596).

## Validation

- Success 1, 2: `focused-test`, new parametrized
  `test_expiry_holds_a_completed_unsettled_history_until_teardown` in
  `tests/services/external_boot/test_allocation_release.py` (real Postgres). Red on main: the
  sweep returns 1.
- Success 2 also: `test_expiry_without_a_restricting_activation_logs_no_denial` now seeds the
  System `torn_down`, since its current setup is the defect.
- Success 3: existing `test_expiry_holds_an_allocation_whose_settled_system_still_runs`, unchanged.
- Success 4: existing `test_expiry_retains_an_allocation_needed_for_external_boot_cleanup`.
- Success 5: `focused-test`, `test_the_reconciler_reads_the_release_fence` in
  `tests/db/test_migration_0194_stranded_activation_settlement.py`.
