# ADR 0494 — The staging drain is keyed on each file's token, and has a lane of its own

- **Status:** Accepted
- **Date:** 2026-07-29
- **Completes:** [ADR-0452](0452-flock-guarded-reclaim-staging-sweep.md), whose Consequences record
  this as the case decision 6 does not reach and name #1559.
- **Amends:** [ADR-0442](0442-rootfs-reclaim-worker-job.md) §7 — the drain tail no longer returns
  early on a surviving rootfs row, and its staging sweep no longer derives its licence from the row
  count.
- **Amends:** [ADR-0441](0441-investigation-scoped-uploaded-rootfs.md) §6 — the TTL backstop is
  joined by a third worklist that is not a pure `artifacts` query.
- **Spec:** [`../specs/2026-07-29-token-keyed-staging-drain-1559-design.md`](../specs/2026-07-29-token-keyed-staging-drain-1559-design.md)

## Context

Every path that reclaims a staged uploaded-rootfs base is driven by an `artifacts` row.
`_reclaim_one_checksum` derives the token from `object_key` and unlinks
`<uploads>/<inv>/<token>.qcow2`. A base sitting there with **no** row is reclaimed by whichever
sweep happens to walk that directory — and ADR-0452 decision 6 added exactly one such walk, in the
drain tail of the `reclaim_investigation_rootfs` handler, licensed by a precondition it reads under
the `INVESTIGATION` lock: *no rootfs row remains for this investigation, therefore every file here
is unowned.*

That precondition is sound where it holds. Three reachable states do not satisfy it, and in all
three a SENSITIVE qcow2 of up to the 50 GiB canonical cap is reclaimed by nothing at all.

**(a) A never-closed investigation whose rootfs rows have all drained.** Neither reconciler lane
selects it. `_CLOSE_DRIVEN_INV_SQL` requires a non-NULL `rootfs_cleanup_pending_at`, which only
`investigations.close` sets. `_TTL_ROOTFS_OBJECTS_SQL` is a pure `artifacts` join, so zero rows
select zero work. No job is ever enqueued, the drain tail never runs, and the leak is permanent
until a human closes the investigation. This is the dominant case: the TTL backstop exists *because*
investigations are not reliably closed.

**(b) A base published after the pass's own enumeration.** `sweep_investigation_staging_dir` globs,
then `rmdir`s. A file landing between the two gives `ENOTEMPTY` with no held partial, so the caller
clears the drain marker — and `_remove_drained_dir`'s own warning says so out loud: *"its drain
marker is cleared regardless, so nothing will revisit it."* The window is narrow, because the
publishing fetcher holds an `flock` on its partial across `_durable_replace` and the partial glob
runs first, so the ordinary interleaving reports a held partial and defers. It is not closed.

**(c) A checksum whose reclaim faults permanently.** `_finish_drained_investigation` returns before
the sweep while any rootfs row remains. A `PermissionError`-class unlink fault, or an object store
that is dead or refusing, keeps the row indefinitely — and the close-driven lane keeps re-issuing
the job every backoff, each one turning back at the same early return. An orphan base in that same
directory is starved for as long as the fault lasts. The steady state during the grace window has
the same shape without any fault at all: a *pinned* base keeps its row, so its investigation's
staging directory is not swept either.

## Decision

1. **The staging sweep is keyed on each file's own token, not on the investigation's row count.**
   `sweep_investigation_staging_dir` takes a `protected_tokens` set, read by the caller under the
   `INVESTIGATION` lock, and collects a `<token>.qcow2` or `<token>.ready` only when its token is
   outside that set. `protected_tokens` is the union of two things: the tokens a surviving rootfs
   `artifacts` row owns (derived from `object_key` by the same `_rootfs_token_from_key` the
   row-driven reclaim uses), and the tokens a live System pins.

   Token-keying is what makes the collection independent of all three states above. It is also what
   makes running the sweep alongside surviving rows *safe*: the pre-existing marker glob was
   unconditional, and under a surviving row an unconditional glob strips the [ADR-0451](0451-staged-rootfs-completion-marker.md)
   completion marker off a perfectly good base — which the reuse gate then rejects, forcing a silent
   multi-GiB re-download on the next provision.

2. **The partial glob keeps its `flock` gate, gains no token test, and keeps running only on a
   full drain.** A `<token>.<uuid>.partial` is not a published base, so ownership says nothing about
   it and ADR-0452 decision 2's kernel-answered liveness question remains the whole answer — the two
   gates are not re-derived from each other. Its *reach* is also left exactly where ADR-0442 §7 put
   it: a surviving rootfs row means a fetch of that base can legitimately be in flight, so the
   partial glob is skipped while any row remains. Widening it is #1565's question — and
   [ADR-0495](0495-reclaim-defers-a-live-held-checksum.md) answers that question "no": a tail that
   unlinks files cannot retain the row a retry needs, so the probe went into `_reclaim_one_checksum`
   instead and this decision is unchanged.

3. **A live System's pin is read as a set; a pinned-but-unowned base is left in place, reported,
   and does *not* retain the drain marker.** `pinned_rootfs_tokens` enumerates the referencing
   Systems once and returns every token pinned by ADR-0441 §6's conditions (a) and (b);
   `rootfs_base_reclaimable` is re-expressed against it, so the row-driven gate and the
   filesystem-keyed sweep cannot drift on what "pinned" means.

   Leaving the base closes ADR-0452's own recorded residue — an overlay created *after* the base's
   row was reclaimed, which the zero-row precondition could not see because that precondition reads
   rows and an overlay is a file.

   Retaining the marker on it looks like the symmetric choice and is wrong.
   `_ROOTFS_REFERENCERS_SQL` excludes only `torn_down`; `failed` is terminal with **no** transition
   out of it (`domain/capacity/state.py`), and nothing removes a `failed` System's overlay —
   `remove_overlay` runs from teardown, `_reclaim_materialized_on_failure` only undoes an overlay
   its own call created, and `repair_leaked_domains` skips any System whose row is not `torn_down`.
   So a pin can be **permanent**, and pinning the marker on it resurrects the never-clearing marker
   and the re-fail-every-pass loop ADR-0442 was written about — the outcome ADR-0452 §5 rejects. The
   marker clears with a `WARNING` naming the condition, exactly as an unremovable directory does.
   The base is still never unlinked, which is the part that matters; what the marker would buy is
   only *who* revisits it, and for an `open`/`active` investigation that is decision 5's lane, which
   is not marker-keyed.

   Both this and the held-partial deferral are decided from what the **walk observed**, never from
   `protected_tokens` being non-empty. A non-empty set with an empty directory is the ordinary
   steady state — the row-driven reclaim unlinks each base as its own row drains — so deriving
   "a base was left behind" from the set would fire the survivor `WARNING` on every ordinary drain
   and defer a drain that plainly completed. That is the inference-as-invariant defect ADR-0452 §4
   removes one function over.

4. **A failed `rmdir` re-runs the collection once, then gives up.** State (b) is a file that no glob
   this pass ever saw. One bounded re-pass converts a permanent leak into one extra `readdir`. It is
   deliberately not a loop: an unbounded retry is the never-terminating drain ADR-0452 decision 5
   rejected, and #1558 removes the race that produces the window at all. Both `rmdir`s stay gated on
   the investigation being row-drained, so a live fetcher's directory is never removed under it.

5. **A third reconciler lane, `sweep_unowned_investigation_rootfs_staging`, keyed on `systems`.**
   Its worklist is the `open`/`active` investigations that have a System older than the rootfs
   retention whose `provisioning_profile` names an `upload` rootfs, and that have **no** rootfs
   `artifacts` row left. It issues the existing `reclaim_investigation_rootfs` job with an **empty**
   `artifact_ids`, which falls straight through the reclaim loop to the drain tail — the same
   empty-worklist path `sweep_investigation_rootfs_reclaim` already relies on for a marker past
   grace with no rows left. No new job kind, no new payload, no migration.

   It is gated on its **own** `ROOTFS_STAGING_DRAIN_BACKOFF` (6 hours) rather than the neighbouring
   lanes' `ROOTFS_RECLAIM_RETRY_BACKOFF` (5 minutes), for the reason in the Consequences below: this
   lane's worklist is a steady state rather than a condition that clears.

   The `systems` row is the *causal* record for a staged base: one is only ever staged for a System
   whose profile names an `upload` rootfs, and Systems are retired in place (`torn_down`) rather
   than deleted, so the trigger outlives every row the base itself had. The `NOT EXISTS` keeps this
   worklist disjoint from the TTL lane's and the `open`/`active` predicate keeps it disjoint from
   the close-driven one, so the three never contend for the shared per-investigation dedup key.

## Consequences

- A staged base with no owning row is collected in all three states, and the per-investigation
  staging directory drains to nothing — #1559's acceptance criterion.
- **The reconciler still holds no filesystem, and the fix does not give it one.** The new lane is
  DB-only and hands an empty worklist to the worker, which is the whole point of ADR-0442: on a
  host-process local-libvirt deployment the worker runs as root and the reconciler as the invoking
  user, so a reconciler-side `unlink` fails after the object is gone (#1522).
- **The new lane's worklist is a permanent steady state, not a condition that clears, and that is
  its real cost.** `systems` rows are retired in place and never leave the match, so once an
  `open`/`active` investigation's rootfs rows have TTL-drained, it is selected on *every* pass for
  the rest of its life — whether or not its staging directory holds anything, which the reconciler
  cannot see (it holds no filesystem, ADR-0442). This is **not** the shape the TTL lane has: that
  lane is anchored on an `artifacts` row, so its churn ends when the row drains. Each pass costs a
  `jobs` delete-and-insert, a worker dequeue/lease/complete cycle, an `INVESTIGATION` advisory-lock
  transaction (the lock System bind contends on), one `pinned_rootfs_tokens` enumeration with an
  `os.stat` per referencing System, and one `readdir`. At the shared 5-minute backoff that is ~288
  passes a day per such investigation, growing linearly with the number of long-lived
  investigations and never decreasing.

  Two things bound it. The lane takes its own 6-hour `ROOTFS_STAGING_DRAIN_BACKOFF`, which cuts the
  permanent rate ~72x while still converging far faster than the TTL policy in days that governs
  the bytes it reclaims. And the settle path's `UPDATE investigations SET rootfs_cleanup_pending_at
  = NULL` is now predicated on the column being non-NULL, so it stops writing a dead tuple per pass
  on a table every System bind reads — that write was unconditional and is a no-op on both
  marker-less lanes. Making the lane itself conditional would need durable per-investigation "the
  directory is empty" state whose only reader is this sweep — a schema change to save a `readdir`.
- **The trigger is a superset of the leak, not a detector of it, and it is bounded the one way that
  matters.** An investigation that never staged an uploaded base is excluded by the profile
  predicate, so the worklist does not grow with the whole `systems` table. What it does not exclude
  is an investigation whose staging directory has already drained, per the previous point.
- **Reach depends on `systems` rows outliving the base.** They do today — teardown moves a System to
  `torn_down` and no path deletes the row — but if a System hard-delete is ever added, this lane
  goes blind for exactly the investigations it exists to serve. Stated rather than derived.
- **The lane inherits the TTL retention (30 days by default) as its own age gate.** A base orphaned
  in a never-closed investigation therefore survives up to that long, which is a large improvement
  on "forever" and not an immediate collection. A shorter window was rejected: the age gate is what
  keeps the lane off a System that is staging its base right now, between the `mkdir` and the row
  resolution, and it matches the policy the artifacts-keyed TTL lane already applies to the same
  bytes.
- **A pinned-but-unowned base survives with its marker cleared, and for a *closed* investigation
  nothing revisits it.** Decision 3's cost, stated rather than derived. The base is never unlinked,
  so no running guest loses its backing file; what is lost is the follow-up, and only for a closed
  investigation, whose Systems `investigations.close` has already coupled. For an `open`/`active`
  one, decision 5's lane is the follow-up and is not marker-keyed. The `WARNING` names the condition
  so an operator has the one signal there is. Retaining instead is rejected in decision 3.
- **The drain tail now enumerates the staging directory on every pass rather than only on a
  complete drain.** Three `glob`s and a `rmdir` attempt per reclaim job, against a directory holding
  a handful of entries. It also runs while holding the `INVESTIGATION` lock, as it did before, so
  the added work is on the same critical section — immaterial at this cardinality, and named because
  the lock is shared with System bind.
- **`_finish_drained_investigation`'s deferral WARNING is suppressed while a rootfs row survives.**
  A pinned base is the expected steady state for the whole grace window, and warning on it every
  pass would bury the line that reports a genuine unexplained survivor.
- #1565 is **not** fixed here and its case is untouched: a partial a live writer holds on the TTL
  lane still has no retry, because that lane runs against an investigation whose marker is already
  NULL and this ADR's new lane requires zero rootfs rows, which a pinned base does not satisfy. The
  new lane does give the *drained* half of that asymmetry a trigger, which narrows #1565's scope to
  the rows-still-present case.

  **Amended by [ADR-0495](0495-reclaim-defers-a-live-held-checksum.md).** That rows-still-present case
  is now fixed, and not by decision 2's "widening the partial glob" — the deferral went into
  `_reclaim_one_checksum`, where retaining the `artifacts` row makes the existing TTL lane the retry.
  Decision 2 stands as written: this tail's partial glob is still skipped while any row survives.
  What this ADR *does* leave behind, and ADR-0495 does not fix, is decision 5's `s.created_at` age
  gate: a freshly-created System reusing a long-staged, already-past-retention checksum can hold a
  live partial before its own `systems` row ages into this lane's window, so the drained half stays
  unretried for up to `investigation_rootfs_retention`.
- **An `abandoned` investigation is reached by no lane, including this one.** All three require
  either `open`/`active` or a marker `investigations.close` sets, and `_close_locked` refuses to
  close an `abandoned` investigation. It is unreachable today — no writer transitions an
  investigation to `abandoned` — but the state is in the enum and the transition table, so adding
  such a writer reopens #1559 for it. Named here rather than guarded against a state nothing sets.
- **The lane has no supporting index.** There is none on `artifacts (owner_kind, retention_class,
  owner_id)`, and `#>>` is not indexable without an expression index, so the lane sequentially scans
  `systems` with a per-row jsonb extraction and anti-joins `artifacts`. Both tables are small at
  this deployment's cardinality and the anti-join hashes into one `artifacts` scan, but the cost is
  permanent per the steady-state consequence above rather than decaying.
- No schema, no migration, no config setting, no new dependency, no MCP tool, no RBAC surface
  change. Not an AI surface. One new reconciler `repair_kind`
  (`unowned_investigation_rootfs_staging_drains_enqueued`), which joins `ALL_REPAIR_KINDS` and the
  `ReconcileReport` fields.

## Considered & rejected

- **Set `rootfs_cleanup_pending_at` on an open investigation to reuse the close-driven lane.**
  Rejected, and already rejected on record by ADR-0452's Consequences. It is a one-line fix and it
  corrupts the record model: the column is durable, record-model-visible state
  (`domain/lifecycle/records.py`) whose meaning is "this investigation was closed and its rootfs is
  being reclaimed". An open investigation carrying it would read as closed to every consumer.
- **A new `sweep_orphaned_rootfs_bases` job kind that walks the whole uploads tree.** Rejected. It
  is the shape #1559's option 2 sketches and it is the cleanest *filesystem*-keyed worklist, but
  `jobs.kind` is a Postgres enum, so a new kind costs a migration; the job would carry no real
  project, so it lands in no tenant's queue view; and on a multi-worker deployment one global job
  reaches one host's uploads tree. Reusing the existing per-investigation job with an empty worklist
  gets the same reach with none of that.
- **Widen `_TTL_ROOTFS_OBJECTS_SQL` to a `LEFT JOIN` instead of adding a lane.** Rejected. The two
  worklists share the per-investigation dedup slot, so folding them means one query that must stay
  disjoint from itself; keeping them separate makes the `NOT EXISTS` disjointness a property a test
  can state directly.
- **Keep the zero-row precondition and add only the new lane.** Rejected. It fixes (a) alone.
  State (c) is reached by the close-driven lane already and turned back by the early return, and
  state (b) needs the re-pass, so the precondition has to go regardless.
- **Run the `rmdir` unconditionally so the directory always drains.** Rejected. `rmdir` on an empty
  directory races a fetcher that has just `mkdir`ed and not yet created its partial, which fails
  that provision with a staging fault. Gating it on the investigation being row-drained is what
  keeps that *narrow*: a fetch resolves an `artifacts` row before it stages, and
  `complete_rootfs_upload` takes the `INVESTIGATION` lock, so a newly-finalized upload cannot race
  a drain that read zero rows under that lock. It is **not** unreachable, and the ADR does not claim
  it is: the fetch resolves its row on a separate autocommit connection and never takes the
  `INVESTIGATION` lock, so on ADR-0452 §6's own doomed-fetcher path the row is resolved, then
  deleted by `_reclaim_one_checksum`, and the fetcher reaches its `mkdir` with the investigation now
  row-drained. Unchanged from before this ADR except that decision 4 makes two `rmdir` attempts per
  pass rather than one; #1558 removes the doomed-fetcher path that produces it.
- **Retain the drain marker on every non-drain, so state (b) is revisited.** Rejected for ADR-0452
  decision 5's reason, unchanged: an unremovable directory or an unopenable partial is permanent
  until an operator acts, and pinning the marker on it resurrects the never-clearing marker and the
  re-fail-every-pass loop ADR-0442 was written about. The bounded re-pass gets the reachable part of
  the benefit with a terminating rule.
- **Checksum-verify each staged base against its row instead of comparing tokens.** Rejected. It is
  O(filesize) against a base of tens of GiB, under the `INVESTIGATION` lock, to answer a question
  the content-addressed filename already answers — the same reasoning ADR-0451 applies to the reuse
  gate.
