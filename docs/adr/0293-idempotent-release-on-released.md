# ADR-0293: idempotent `allocations.release` on an already-released grant (#953)

- Status: Accepted
- Date: 2026-07-01

## Context

The agent-facing 9-step workflow (ADR-0284's `agent-index`) ends with step 9: *"Release —
`allocations.release` when done."* But `systems.teardown` does not itself release the backing
allocation. When a lifecycle Run's single System reaches a terminal state, the allocation is
left `active` with no live System; the reconciler's orphaned-active reaper
(`reap_orphaned_active_allocations`, `reconciler/repairs/allocations.py`, ADR-0109/#371)
reclaims it after a 2-minute grace (`DEFAULT_ORPHANED_ACTIVE_GRACE`). An agent that follows the
workflow and calls `allocations.release` at step 9 — after that sweep has run — finds the grant
already `released`.

`_release_locked` (`services/allocation/release.py`) treated all three terminal states —
`released`, `expired`, `failed` — identically: it returned `ReleaseOutcome(released=False,
category=STALE_HANDLE, current_status=<state>)`, which the MCP layer surfaces as an error
envelope (ADR-0040 §4 named the `stale_handle`-on-terminal behavior). So a caller who did
exactly what the documented workflow prescribes gets a spurious error and has to reconcile it
with `allocations.get`. The black-box review filed this as #953 (Finding 5); the review
mis-attributed the auto-release to a direct teardown cascade — the real path is the reaper.

`release` is a "drive this to done" intent, not a "perform this transition" command. An
already-`released` grant *is* done. Idempotence is the natural contract, and the codebase
already applies it elsewhere (a `requested` cancel goes straight to `released` with no credit;
the reaper isolates and no-ops terminal candidates).

## Decision

**Split the terminal-state branch in the caller-facing release path** (`_release_locked`, used
by `release_with_backstops` for both project release and platform break-glass release):

- Already **`released`** → return `ReleaseOutcome(released=True)` **idempotently**. No state
  transition (`released` stays terminal, we never re-enter `releasing`), no audit row, and no
  ledger touch. The grant is already terminal *and already reconciled*; releasing it again is a
  no-op success. This is the outcome the caller asked for regardless of who drove the
  transition (the caller, the break-glass path, or the reaper).
- Still-terminal **`expired`** / **`failed`** → keep `STALE_HANDLE` with `current_status`.
  These are terminal outcomes the caller did **not** request: the lease lapsed, or provisioning
  failed. Returning `ok` would hide that the agent's mental model ("I still hold this") is
  wrong; the agent should learn the real state via `allocations.get`.

The idempotent branch performs **no** `accounting.reconcile` and **no**
`accounting.stamp_active_ended`, so ADR-0040 §4's "exactly one reconciliation per allocation"
invariant is preserved: the single `reconciled` credit was already written by whoever first
drove the terminal transition. Two concurrent releasers (or a releaser racing the reaper)
serialize on the ALLOCATION advisory lock; the second reads `released` and returns idempotent
`ok` — never a second credit.

**Agent-surface effect.** The `allocations.release` wrapper docstring gains one clarifying
sentence: a completed `systems.teardown` may leave the allocation auto-released, so a step-9
release can return `ok` as a no-op. This is the issue's secondary suggestion, keeping the
agent-facing contract honest about the interaction.

**Scope.** `services/allocation/release.py` plus the `allocations.release` wrapper docstring and
tests. No schema, migration, RBAC, error-category, config, or new-tool change.

## Consequences

- The documented step-9 release is no longer a spurious error after a teardown + reaper cycle;
  the happy path is honest. An agent that follows the workflow gets `ok`.
- Break-glass release (`breakglass_release_allocation`) inherits the idempotent `released`
  outcome for free, since it shares `_release_locked`. The platform accountability row still
  commits first, so an idempotent no-op release is still audited at the platform layer.
- `expired`/`failed` still surface as `stale_handle`, so a lapsed lease or a failed provision is
  never silently reported as a clean release.
- ADR-0040 §4 is not weakened: the single-reconciliation invariant holds because the idempotent
  branch writes no credit. This ADR narrows *when* `release` returns `stale_handle` (only for
  `expired`/`failed`), not how reconciliation happens.
- The reaper path (`reclaim_under_lock`) is untouched: its terminal-state `stale_handle` return
  is internal and its candidate query never selects an already-`released` row, so there is no
  caller to make idempotent.

## Considered & rejected

- **Documentation only** (the issue's other suggestion — note in `agent-index` that teardown
  auto-releases, so step 9 may be a no-op). Leaves the spurious error in place; every agent
  must still special-case `stale_handle` on the happy path. Rejected as the sole fix; the
  clarifying docstring sentence is kept as an addendum to the code fix.
- **Make all terminal states return `ok`** (`expired`/`failed` too). Hides a lapsed lease or a
  failed provision behind a clean-release envelope, destroying the signal that the agent's
  handle is stale for a reason it did not choose. Rejected; only `released` is the caller's own
  intent.
- **Re-run the release transition on an already-`released` grant** (idempotent re-transition).
  Would need a `released -> releasing` edge (illegal) or a self-edge, and risks a second ledger
  credit — the ADR-0007 §2 budget-minting hazard ADR-0040 §4 guards against. Rejected; the
  no-op success writes nothing.
- **Change the reaper (`reclaim_under_lock`) to idempotent too.** It never targets a `released`
  row and its `stale_handle` return is internal logging, so the change would have no caller.
  Rejected as dead scope.
- **A new `ErrorCategory` (or a distinct success sub-status) for "already released".** The
  existing `released` success status already models it; adding surface for a no-op violates the
  stable-taxonomy invariant. Rejected.
