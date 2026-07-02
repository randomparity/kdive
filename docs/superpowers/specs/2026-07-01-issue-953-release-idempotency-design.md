# Idempotent `allocations.release` on an already-released grant (#953)

- **Issue:** [#953](https://github.com/randomparity/kdive/issues/953) — `allocations.release`
  after `systems.teardown` returns spurious `stale_handle`.
- **ADR:** [ADR-0293](../../adr/0293-idempotent-release-on-released.md)
- **Status:** Draft
- **Scope:** `src/kdive/services/allocation/release.py` + tests. No schema, RBAC, config, or
  new-tool change.

## Problem

The documented 9-step agent workflow ends at step 9: *"Release — `allocations.release` when
done."* But `systems.teardown` does not itself release the backing allocation. The
reconciler's `reap_orphaned_active_allocations` (`reconciler/repairs/allocations.py`) reclaims
the now-orphaned `active` allocation after a 2-minute grace
(`DEFAULT_ORPHANED_ACTIVE_GRACE`). An agent that reaches step 9 after that sweep has run
finds the grant already `released`.

`_release_locked` (`release.py:240-245`) treats every terminal state — `released`, `expired`,
`failed` — identically: it returns `ReleaseOutcome(released=False,
category=STALE_HANDLE, current_status=<state>)`. The MCP layer surfaces this as an error
envelope. So the caller who did exactly what the workflow told them to do gets an error and
must reconcile it with `allocations.get`.

## Decision

Split the terminal-state branch in the caller-facing release path (`_release_locked`, used by
`release_with_backstops` for both project release and platform break-glass release):

- Already **`released`** → return `ReleaseOutcome(released=True)` **idempotently**: no state
  transition, no audit row, no ledger touch. The grant is already terminal and already
  reconciled; releasing it again is a no-op success. This is the outcome the caller asked
  for, whether they or the reaper drove the transition.
- Still-terminal **`expired`** / **`failed`** → keep `STALE_HANDLE` with `current_status`.
  These are terminal outcomes the caller did **not** ask for: the lease lapsed, or
  provisioning failed. Reporting them as `ok` would hide that the agent's mental model
  ("I still hold this") is wrong. The agent should learn the real state via `allocations.get`.

The idempotent branch performs **no** ledger reconciliation and **no** `stamp_active_ended`,
so the ADR-0040 §4 "exactly one reconciliation per allocation" invariant is preserved: the
credit was written once, by whoever first drove the terminal transition (project release,
break-glass, expiry sweep, or the orphaned-active reaper).

## Boundaries — what does NOT change

- **The reaper path (`reclaim_under_lock`) is untouched.** Its terminal-state `stale_handle`
  return is internal to the reaper, which isolates each candidate and logs the outcome; the
  reaper never targets an already-`released` row (its candidate query selects only `active`
  rows with no live System). Making it idempotent would be a change with no caller.
- **No new state transitions.** `released` stays terminal; we do not re-enter `releasing`.
- **No schema, migration, RBAC, config, error-category, or tool-surface change.** The MCP
  wrapper docstring for `allocations.release` gains one clarifying sentence that a completed
  teardown may leave the allocation auto-released, so a step-9 release can be a no-op `ok`
  (the secondary suggestion in the issue), keeping the agent-facing contract honest.

## Success criteria (falsifiable)

1. `allocations.release` on a grant already in state `released` returns status `released`
   (`ok`), not an error, and writes **no** additional audit row and **no** additional ledger
   row beyond those the terminal transition already produced.
2. `allocations.release` on a grant in state `expired` still returns
   `error_category=stale_handle` with `data.current_status="expired"`.
3. `allocations.release` on a grant in state `failed` still returns
   `error_category=stale_handle` with `data.current_status="failed"`.
4. The `granted`, `active`, `requested`, absent, cross-project, malformed, and
   illegal-transition-backstop behaviors are unchanged.
5. The break-glass release path (`breakglass_release_allocation` →
   `release_with_backstops`) inherits the same idempotent `released` outcome, because it
   shares `_release_locked`.

## Edge / error cases

- **Concurrent double release** (two callers, or caller + reaper): the second observer reads
  `released` under the ALLOCATION lock and returns idempotent `ok` — no second credit, no
  second transition. The first observer did the real work.
- **`expired`/`failed` unchanged**: covered by criteria 2 and 3.
- **`requested` cancel** (ADR-0069) is a distinct earlier branch (direct to `released`, no
  credit) and is not affected.
