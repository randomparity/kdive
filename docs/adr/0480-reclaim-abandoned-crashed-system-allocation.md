# ADR 0480 — Reclaim an `active` allocation on an abandoned `crashed` System

- **Status:** Accepted
- **Date:** 2026-07-28
- **Amends:** [ADR-0109](0109-reap-leaked-active-allocation.md). That ADR's predicate is
  unchanged for terminal and absent Systems; only its treatment of `crashed` as
  *unconditionally* live is revised here. See §1.
- **Depends on:** [ADR-0021](0021-reconciler-loop-drift-repair.md) (the reconciler as the convergence
  point for cross-object drift), [ADR-0036](0036-reservation-lease-semantics.md) (the 4h
  `lease_expiry` that is the only thing reclaiming these slots today),
  [ADR-0069](0069-reservation-pending-queue-scheduler.md) (the `OCCUPYING` host-cap predicate a
  stranded `active` allocation wedges), [ADR-0325](0325-force-crash-crashing-state.md) (the
  `crashing` marker, which stays unconditionally live).
- **Issue:** [#1628](https://github.com/randomparity/kdive/issues/1628). Found while proving
  [#1610](https://github.com/randomparity/kdive/issues/1610).

## Context

A run that aborts **between** crashing its System and releasing its allocation strands the slot.
The allocation stays `active`, the System sits in `crashed`, the overlay stays in the pool, and
the slot counts against `concurrent_allocation_cap` until the 4-hour lease elapses.

Observed twice on `rock10-big` (`concurrent_allocation_cap = 2`) while proving #1610: an
allocation still `active` 90 minutes after its run died, which — combined with the four-method
capstone's System A — reached the cap and denied `alloc-B`. The denial is correct enforcement;
the leak is the defect. Worse, the failure names *capacity*, so it reads as a host-sizing problem
rather than as stale state. Recovery today is a manual `allocations.release` per stranded id.

Every existing reaper misses it:

- The `→expired` lease sweep (ADR-0036) keys only on `lease_expiry < now()`, which is 4 hours out
  — not a practical run timescale.
- `reap_orphaned_active_allocations` (ADR-0109) requires the allocation to have **no live
  System**, and `_LIVE_SYSTEM_STATES` includes `crashed`. So the repair built for exactly this
  leak skips exactly this case.
- `repair_orphaned_systems` is the reverse case (terminal allocation + live System → teardown);
  `_ORPHANED_SYSTEM_TERMINAL_STATES` is `(torn_down, failed)`.
- `DumpVolumeReaper` reaps dump volumes, not overlays or allocations.

ADR-0109's exclusion of `crashed` is deliberate and load-bearing, not an oversight: *"an
allocation held for an in-progress crash investigation (the central kdive workflow) is
preserved."* `force_crash → capture_vmcore → analyze → teardown` legitimately parks an allocation
on a `crashed` System for as long as the agent keeps working. **Simply deleting `CRASHED` from
`_LIVE_SYSTEM_STATES` would reap live investigations after the 2-minute grace** — a regression
strictly worse than the bug it fixes.

So the real problem is not "is `crashed` live?" but "**can the reconciler tell an abandoned
`crashed` System from an in-progress one?**" State alone cannot: both sit in `crashed`. The
distinguishing evidence has to be activity over time.

## Decision

### 1. `crashed` becomes the one *conditionally* live System state

`_LIVE_SYSTEM_STATES` is unchanged as a set — it stays the documented complement of admission's
`_NON_TERMINAL_SYSTEM`, and a `crashed` System still occupies a quota slot everywhere else.
The liveness *predicate* gains one exception: a `systems` row in `_LIVE_SYSTEM_STATES` keeps its
allocation live **unless** it is `crashed` and its crash investigation has gone idle.

`crashing` is explicitly **not** included. It is mid-`force_crash`, transient by construction,
and `repair_stalled_crashing_systems` already resolves a stuck one to `crashed` in the same pass
— at which point this repair's clock starts. Adding a second idle rule for a state that already
has a dedicated resolver would give a stalled crash two competing recoveries.

### 2. Idle means all three activity signals are silent

A `crashed` System is abandoned only when **every** one of these is true:

1. **`systems.updated_at < now() - crashed_idle_grace`.** The `crashed` state is stamped by the
   `force_crash` finalize, so this clock starts at the crash instant.
2. **No job naming the System** (`payload->>'system_id'`) is active (`queued`/`running`) **or**
   has `updated_at` inside the window. `capture_vmcore`, `power`, and `teardown` all carry it.
   The recency arm — not just the active arm — is what covers the capstone's own shape: a
   capture that just *succeeded* while the agent chooses the next method leaves no active job,
   no session, and a System row stamped at the crash.
3. **No DebugSession on any of the System's Runs** is non-terminal (`attach`/`live`) **or** has
   `updated_at` inside the window. A drgn/gdb session is the analysis half of the workflow, and
   an agent can hold one open reading guest memory for a long time without writing another row.

Any single signal firing keeps the allocation live. The three are cheap, already indexed by the
same access patterns sibling repairs use, and — critically — they are the signals a *live* agent
necessarily produces.

### 3. The job signal counts only jobs the reconciler did not author

`sweep_console_rotation` enqueues a fresh `console_rotate` job for **every** live local-libvirt
System — `crashed` included — on **every** pass, forever, each with a unique `dedup_key`. Signal
2 taken naively is therefore permanently true for any `crashed` local System, and this entire
repair would be unreachable on the default provider while still passing a unit test that seeds no
such job. That is the exact shape of a fix that ships green and never fires.

The job clause therefore requires
`j.authorizing->>'principal' IS DISTINCT FROM 'system:reconciler'`. Keying on the authorizing
principal rather than an excluded-kind list means a future reconciler-issued job kind is excluded
automatically, while a future agent-issued kind is counted automatically — the drift-safe
direction on both sides. `IS DISTINCT FROM` (not `<>`) so a row with no recorded principal counts
as activity, which is the preserving direction.

An in-flight reconciler-authored **teardown** likewise does not preserve the slot. Teardown never
releases the allocation — that is the original ADR-0109 leak — so waiting on one only delays the
reclaim it exists to enable.

### 4. One predicate, two call sites

The liveness SQL is built once (`_live_system_exists_sql`) and used for both the unlocked
candidate scan (correlated on `a.id`) and the under-lock re-check (`_has_live_system`, a bound
placeholder). The read-then-act gap ADR-0109 closed for System re-creation now closes for
investigation *resumption* too: a capture job enqueued or a session attached between the
candidate read and the `PROJECT → ALLOCATION` lock is seen by the re-check and the allocation is
skipped. Duplicating the predicate — the obvious alternative — would let the two drift, and the
half that drifts is the one that decides whether a live investigation dies.

The expression parameter is typed `LiteralString`, so no runtime value can reach the interpolated
position.

### 5. A distinct, much longer grace: `crashed_idle_grace`, default 30 minutes

`DEFAULT_ORPHANED_ACTIVE_GRACE` (2 min) guards a read-then-act race measured in seconds and stays
as it is. The crashed-idle window is a different quantity — it has to outlast an agent's think
time between two capture methods on the same crashed guest — and is deliberately an order of
magnitude larger, while staying an order of magnitude *below* the 4-hour lease that is the status
quo. It is a `ReconcileConfig` field (`crashed_idle_grace`), mirroring
`debug_session_stale_after`, so an operator can widen it on a host where investigations idle for
long stretches or narrow it on a tight-cap host where a stranded slot denies real work. No new
environment setting, no schema, no migration: `systems.state`/`updated_at`,
`allocations.updated_at`, `jobs`, and `debug_sessions` all already carry what the predicate needs.

## Alternatives considered

- **Delete `CRASHED` from `_LIVE_SYSTEM_STATES` (the issue's option 1, read literally).**
  Rejected: it reaps live crash investigations after 2 minutes. This is the failure mode the
  whole design is arranged around, and it is pinned by
  `test_active_with_recently_crashed_system_preserved`.
- **Shorten `lease_expiry` for allocations on a terminal System (the issue's option 2).**
  Rejected: `lease_expiry` is the reservation contract (ADR-0036), visible to the agent and
  reported in the allocation envelope. Silently rewriting it from a background sweep makes a
  published deadline untrue, and the resulting `expired` state also mislabels the outcome — the
  work was abandoned, so `released` is the honest transition, which is precisely what ADR-0109
  already established.
- **Name the stranded allocation ids in the `allocation_denied` detail (the issue's option 3).**
  Not adopted here. It is a user-facing envelope decision on a different plane
  (`services/allocation/admission/core.py`) and is worth doing on its own merits, but it improves
  the *diagnosis* of a leak this ADR removes. Filed as follow-up rather than bundled.
- **Use `runs.state` as the liveness signal.** Rejected: nothing heartbeats a Run, so an aborted
  run leaves its row in `running` indefinitely. `running` is therefore evidence of *abandonment*
  as often as of activity — the one signal that cannot distinguish the two cases.
- **Reuse `has_active_capture_job` alone as the signal.** Rejected as too narrow: it sees only
  `capture_vmcore` in `queued`/`running`, which misses the agent between captures, the analysis
  phase entirely, and every other system-scoped job.
- **Excluded-kind list (`kind <> 'console_rotate'`) instead of the principal test.** Rejected:
  its drift failure mode is that a future reconciler-issued continuous job kind silently disables
  the reaper again — the same defect, reintroduced with CI green.

## Consequences

- A slot stranded by a run that died after crashing its System is reclaimed within one reconcile
  interval past `crashed_idle_grace`, instead of after 4 hours. On a `cap=2` host with zero
  headroom this is the difference between the four-method capstone running and failing at
  `alloc-B` with a message that names capacity rather than stale state.
- **An investigation that goes fully silent for longer than the grace loses its guest.** Once the
  allocation is `released`, `repair_orphaned_systems` enqueues a teardown for the `crashed`
  System, destroying the domain and its overlay. Already-captured vmcores are unaffected — they
  live in the object store, referenced by `artifacts` rows. This is the accepted cost, and
  `crashed_idle_grace` is the operator's brake on it. State it when documenting the knob: an
  agent that will pause an investigation for hours should raise it.
- The reaper's audit trail, count field (`reaped_active_allocations`), lock order, release
  mechanic, and per-candidate isolation are all unchanged. No new transition, no new ledger
  semantics, no migration.
- `has_active_capture_job` keeps its narrow `capture_vmcore` meaning for `reap_orphaned_dump_volumes`;
  only the shared `_ACTIVE_JOB_STATES` constant is renamed now that two clauses read it.
- The activity predicate is a heuristic, and it is a heuristic about *agent behavior*. If a future
  workflow keeps an allocation legitimately parked on a `crashed` System while writing no rows
  at all, it will need a fourth signal or a wider grace — not a return to unconditional liveness,
  which is what stranded the slot in the first place.
