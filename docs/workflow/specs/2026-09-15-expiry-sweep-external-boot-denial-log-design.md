# Expiry sweep says when the external-boot guard refuses a release — design

Issue #2519, parent #2501. Decisions in force and unchanged:
[ADR-0036](../../adr/0036-reservation-lease-semantics.md), [ADR-0040](../../adr/0040-admission-lifecycle-concurrency.md),
[ADR-0109](../../adr/0109-reap-leaked-active-allocation.md), and [ADR-0596](../../adr/0596-allocation-release-waits-for-external-boot-cleanup.md).

## Problem

`_expire_one` (`src/kdive/reconciler/repairs/allocations.py`) handles
`allocation_release.ExternalBootDenied` with a bare `return False`, identical in effect to the
two legitimate no-op returns above it — allocation already terminal, lease no longer elapsed.
`sweep_expired_allocations` therefore reports the same `reclaimed` count for "nothing to do"
and for "this one cannot be reclaimed until an operator clears an activation", while its
sibling `except Exception` handler logs every other per-candidate failure. A refused allocation
is skipped on every pass with nothing in the logs naming it.

## Scope

One `except` branch gains a log call: it binds the exception and emits one `_log.warning`
before its existing `return False`. The allocation stays non-terminal and nothing else in the
function changes. Files changed: `src/kdive/reconciler/repairs/allocations.py` and
`tests/services/external_boot/test_allocation_release.py`, which already holds the refusal's
behavioural test.

`src/kdive/services/allocation/release.py` is in the permitted surface and is **not** changed.
Issue #2519's second half asks whether refusing release is ADR-0596's intent once the lease has
elapsed. It is: ADR-0596 admits `allocation_release` only when no activation restricts a System
the Allocation owns, because the authority is held only while its Allocation is live, and
terminating the Allocation first strands the release, cleanup and teardown path that would
clear the activation. `expired` is as terminal as `released`, so the guard is correct here. The
hold is not permanent either: `repair_external_boot_lane` (same reconcile pass) enqueues those
successors for a stranded activation, and `get_restricting_for_system`
(`src/kdive/db/external_boot_activations.py`) stops matching once `cleanup_complete` is set, so
the next sweep expires the allocation. No ADR is written and no guard code changes.

Out of scope, with owners: live-stack test fixtures releasing without `try/finally` (#2520);
redesigning the reap architecture (none — the four records above stand); metrics or alerting
beyond a log line (out of batch).

### Lock ordering

The branch sits inside `conn.transaction()` → `advisory_xact_lock(conn, LockScope.PROJECT,
project)` → `advisory_xact_lock(conn, LockScope.ALLOCATION, allocation_id)`. One statement is
added before an unchanged `return False`, so both locks are taken in the same order at the same
point and released at the same `return` as today, and no lock, transaction, `await`, or
database call is added or moved.

## Success

1. A sweep pass over an allocation whose System carries a restricting external-boot activation
   emits exactly one `WARNING` from `kdive.reconciler.repairs.allocations` naming the
   allocation id and the denial, and the allocation stays non-terminal with `reclaimed` at 0.
2. No other outcome emits that record: the two no-op returns above the branch are unedited, and
   a clean reclaim keeps only its existing `INFO` line.
3. The `PROJECT → ALLOCATION` acquisition order and the scope over which both are held are
   identical to the pre-change function.
4. `guard_external_boot_release` is unchanged, the review conclusion above is recorded, and
   `just ci` and `just records` are green.

## Failure model

**Actors and deployments.** The reconciler process only: `_expire_one` is module-private with
one caller, `sweep_expired_allocations`, wired into `reconcile_once`
(`src/kdive/reconciler/loop.py`); no agent, MCP tool, or untrusted caller reaches it. Designed
for every deployment running the reconciler, on the root handler its other lines already use.

**Invariants and assets at stake.**

- The `PROJECT → ALLOCATION` acquisition order, which serializes the expiry sweep, the
  orphaned-active reaper and the release service against each other, and the window inside that
  transaction during which the other two block.
- The allocation's non-terminal state, which keeps the external-boot cleanup path authorized
  (ADR-0596).

**Accepted failure classes.**

- One record per refused candidate per pass, so a permanently stuck allocation logs once every
  `DEFAULT_INTERVAL` (30s). Accepted: the rate the sibling `except Exception` handler and
  `repair_external_boot_lane` already log at, for a condition that needs an operator.
- The record is emitted while both advisory locks are held. Accepted and bounded: a synchronous
  format-and-write to the process's stream handler (`src/kdive/log.py`) with no database access
  and no `await`, alongside several database round trips already inside the same lock window;
  the OTLP arm of the bridge is a `BatchLogRecordProcessor`, so it queues rather than blocking.
- `logging` raising into the locked region. Not reachable: the format string and its arguments
  are fixed at the call site, `UUID` and the denial's bounded-scalar `details` cannot raise on
  conversion, and the stdlib routes handler errors through `handleError`, not the caller.

**Covered elsewhere.** Whether a stranded activation converges — ADR-0596 and
`repair_external_boot_lane`. Live-stack fixture releases — #2520. A richer signal — out of batch.

## Validation

- **A refused expiry is logged** — `Mode: focused-test`, extending
  `tests/services/external_boot/test_allocation_release.py::test_expiry_retains_an_allocation_needed_for_external_boot_cleanup`.
  Red: no `WARNING` from the repair module. Green: one record carrying the allocation id.
- **A clean reclaim is not logged as a refusal** — `Mode: focused-test`, new case in the same
  file seeding a cleaned activation so the guard admits. Red if the call sits outside the
  `except` branch.
- **Lock ordering and hold scope are unchanged** — `Mode: task-test-not-applicable`. The two
  `advisory_xact_lock` calls and their `async with` header are not edited, so the revisions are
  byte-identical there and no observation distinguishes them;
  `tests/adversarial/test_lease_expiry_renew_race.py` holds the ordering contract.
