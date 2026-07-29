# ADR 0495 — The row-driven reclaim defers a checksum whose partial a live writer holds

- **Status:** Accepted
- **Date:** 2026-07-29
- **Amends:** [ADR-0494](0494-token-keyed-staging-drain.md) — its Consequences record #1565 as not
  fixed and its case as untouched. This ADR fixes the rows-still-present half it named, and not by
  the mechanism that entry anticipated (widening the drain tail's partial glob).
- **Amends:** [ADR-0442](0442-rootfs-reclaim-worker-job.md) §3/§4 — `_reclaim_one_checksum` has a
  second gate ahead of the staged-base unlink. The base -> object -> row order is unchanged.
- **Completes:** [ADR-0452](0452-flock-guarded-reclaim-staging-sweep.md), whose `flock` gate stopped
  the *sweep* from destroying a live partial and left the *reclaim* running ahead of it untouched.

## Context

The TTL backstop (ADR-0441 §6) reclaims committed uploaded-rootfs bases of a **never-closed**
investigation. Its worklist, `reconciler.cleanup.gc._TTL_ROOTFS_OBJECTS_SQL`, is a pure `artifacts`
query, and the handler deletes exactly the rows it selected — so once a pass runs, nothing that pass
skipped can be re-selected by it.

ADR-0452 §4 gave the *close-driven* lane a retry for a skipped live-held partial: the drain tail
retains `rootfs_cleanup_pending_at`, so the close-driven sweep re-issues the reclaim past its
backoff. That mechanism is that lane's alone. `sweep_expired_investigation_rootfs_reclaim` has no
marker to retain — a TTL job runs against an `open`/`active` investigation whose marker is already
NULL — and the fetch-side opportunistic sweep needs another fetch of the same (investigation, token),
which `_resolve_object` rejects once the row is gone. So on the TTL lane a skipped partial waited for
a human to close the investigation, which for a never-closed one is unbounded: the accumulation the
TTL backstop exists to prevent (#1565).

ADR-0494 gave the *drained* half of that asymmetry a trigger, `sweep_unowned_investigation_rootfs_staging`,
and its own Consequences record that this half is untouched. That lane's `_UNOWNED_STAGING_INV_SQL`
fires only when **zero** rootfs rows remain (`AND NOT EXISTS (SELECT 1 FROM artifacts …)`), and
`sweep_investigation_staging_dir` returns at `if not drained` before its `flock`-gated partial
collector ever runs. So while any rootfs row survives — not yet past retention, or **permanently
pinned**, e.g. a `failed` System whose overlay nothing removes — the collector does not run at all,
on any pass.

Underneath that bookkeeping gap is a data-loss one, and it is the same root cause. The ADR-0441 §6
pin gate classifies a base from the referencing System's **state column** plus overlay presence:
`_ROOTFS_REFERENCERS_SQL` excludes `torn_down` outright, `failed` is outside
`ROOTFS_BASE_PRE_OVERLAY_SYSTEM_STATES`, and a `provisioning` System has no overlay file yet.
`PROVISIONING -> TORN_DOWN` and `PROVISIONING -> FAILED` are both legal, the download runs detached
under `asyncio.to_thread` and cannot be cancelled, and nothing serializes the two — the fetch takes
only its per-(investigation, checksum) session lock, never the `INVESTIGATION` lock the reclaim
holds. So `_reclaim_one_checksum` unlinks the staged base, deletes the object-store object, and
deletes the row while that download is still streaming. On the ranged-GET path the rest of the
download becomes 404s that `_staging_fault` renders as an `INFRASTRUCTURE_FAILURE` "failed to stage",
pointing an operator at the object store over a purely local reclaim race.

## Decision

1. **`_reclaim_one_checksum` probes `<token>.*.partial` for a live writer, after the pin gate and
   before the first unlink.** `_live_writer_holds_a_partial` runs the existing
   `providers.shared.staging_partials.unlink_partial_if_unheld` over the candidates and reports
   whether any is held. Liveness is asked of the kernel, exactly as ADR-0446 and ADR-0452 already
   ask it — not re-derived from a System state column a third time. The pin gate stays first because
   it is the cheaper question (rows plus one `stat`) and a pinned base is not reclaimed either way.

2. **A held partial defers the whole checksum: base, object and row are all retained, and the job
   still succeeds.** The retained **row** is the entire retry mechanism. `_TTL_ROOTFS_OBJECTS_SQL`
   selects on `artifacts`, so an investigation whose checksum deferred is re-selected by the very
   next TTL pass with no new marker, no new column, no new lane, and no dependency on
   `systems.created_at`. The deferral returns `None`, the same "nothing to do, and not an error"
   value the pin gate returns, so it does not dead-letter the per-investigation reclaim slot — the
   loop continues to the remaining checksums and the handler reports the drained count.

3. **The probe is scoped to the checksum being reclaimed, not to the directory.** The staging tree is
   per investigation and content-addressed within it, so a sibling System downloading a *different*
   base is a routine concurrent state. Deferring on any `*.partial` would stall this checksum for the
   length of an unrelated multi-GiB download — ADR-0442 §7's starvation, keyed on an unrelated
   filename. The glob is the same `<dest.stem>.*.partial` the fetch-side sweep uses against its own
   `dest`.

4. **An unheld candidate of this token is unlinked in passing, through the shared gate.** A dead
   fetcher's partial must not defer the checksum, or the base, the object and the row leak for as
   long as that file sits there — #1565's unbounded leak re-created inside its own fix. Reusing
   `unlink_partial_if_unheld` rather than writing a second read-only probe is what keeps the three
   call sites from drifting on what "held" means, which is the whole reason that module exists.
   `unlink_when_unlockable=True` matches the drain tail rather than the fetch side: this is a
   reclaim-side collector, and on a host that cannot `flock` at all the writer staged unguarded, so
   skipping protects nothing.

5. **`rootfs_cleanup_pending_at` is not set on an open investigation.** Rejected here for the third
   time, on ADR-0452's and ADR-0494's recorded reasoning: the column is durable,
   record-model-visible state (`domain/lifecycle/records.py`) whose meaning is "this investigation
   was closed and its rootfs is being reclaimed", and an open investigation carrying it reads as
   closed to every consumer.

## Consequences

- A live-held partial skipped by a TTL-driven reclaim is collected without the investigation having
  to close — #1565's acceptance criterion — and the reclaim no longer deletes the base, the object
  and the row out from under a live download.
- **This is substantively #1558's option 1, and it is disclosed rather than claimed.** #1558
  ("Pin-dropping System transitions let the rootfs reclaim delete a base under a live download")
  proposes exactly this: *"Run the same `flock` probe over `<token>.*.partial` inside
  `_reclaim_one_checksum` before the base unlink and defer the whole checksum (return `None`, keep
  the row) when a live writer holds one."* Decisions 1–4 implement that, and #1558's written
  acceptance criterion holds against this change. #1558 is **not** closed by this PR and is not in
  this change's scope; a human should judge whether it is now subsumed. What is left of it is its
  option 2 — the classifier itself, which still cannot tell that a `torn_down` or `failed` System was
  mid-provision — plus the residual window in the next point.
- **The race is narrowed, not closed, and the residual is a real interleaving.** The probe is
  point-in-time under the `INVESTIGATION` lock, which the fetch never takes. A fetch that has already
  resolved its `artifacts` row but has not yet created its partial is invisible to the probe, and
  that gap is not instantaneous: the fetch waits on its per-(investigation, checksum) session
  advisory lock in between, potentially behind a sibling's multi-GiB download. Such a fetcher then
  reaches a deleted object and fails its provision with the same misleading staging fault. Closing
  that needs option 2, or an ordering that makes the fetch take the `INVESTIGATION` lock — a change
  to the bind path's contention profile, out of scope here.
- **The secondary residual ADR-0494 introduced is left in place and not fixed here.**
  `_UNOWNED_STAGING_INV_SQL` gates on `s.created_at < now() - retention` per `systems` row, so a
  freshly-created System reusing a long-staged, already-past-retention checksum (content-addressed
  reuse, ADR-0441) can hold a live partial before its own `systems` row ages into that lane's window.
  It is bounded by `investigation_rootfs_retention` (30 days by default) rather than unbounded, and
  it only affects the *drained* half — the half where no `artifacts` row exists, so decision 2's
  retained-row retry has nothing to retain. Fixing it means re-deriving that lane's age gate from
  something other than the `systems` row, which is a change to ADR-0494's decision 5 and its own
  disclosed steady-state cost. Named as follow-on work rather than folded in.
- **A staging directory the worker cannot enumerate reads as "no live writer".** `Path.glob` yields
  nothing for it instead of raising, the same behaviour ADR-0452 §7 and ADR-0494 both record for the
  drain tail's globs. It is not a fail-open hazard: `_unlink_staged_base` runs next, needs write on
  that same directory, and defers the checksum on any `OSError` but `ENOENT`, so nothing is deleted
  either way. Named rather than guarded, because the state needs a staging directory that is
  writable but not readable (mode `0o333`) and nothing creates one.
- **Cost is one `readdir` of a per-investigation directory per due checksum, on the
  `INVESTIGATION`-lock critical section** — the same section, and the same order of magnitude, as the
  three globs ADR-0494 already put in the drain tail. The probe runs only after the pin gate has
  passed, so a pinned base (the steady state for the whole grace window) does not pay for it.
- **A deferral is logged at `WARNING` from two frames.** `unlink_partial_if_unheld` reports the
  *file* observation and `_reclaim_one_checksum` reports the *decision* and what it retained. The job
  succeeds either way, so without the second line a deferral is indistinguishable from an
  already-drained row. On the TTL lane the pair can repeat once per
  `ROOTFS_RECLAIM_RETRY_BACKOFF` (5 minutes) for the length of a download, which is bounded by the
  download rather than by the 30-day grace window — unlike the pinned-base case ADR-0494 deliberately
  silenced.
- **An `abandoned` investigation is still reached by no lane.** Unchanged from ADR-0494's
  Consequences, and unreachable today because nothing transitions an investigation to that state.
- No schema, no migration, no config setting, no new dependency, no MCP tool, no RBAC surface change.
  Not an AI surface. No new reconciler `repair_kind` and no new job kind — the retry is an existing
  lane re-selecting an existing row.

## Considered & rejected

- **Give the TTL lane a marker of its own** (#1565's option 1). Rejected. It is durable schema for
  state the `artifacts` row already carries: the row survives the deferral, and that lane's worklist
  is a query over exactly those rows, so the marker's only reader would be a sweep that already has
  the answer. It also splits one deferral rule into two mechanisms that must be kept in step.
- **Set `rootfs_cleanup_pending_at` on the open investigation** (#1565's option 2). Rejected, on
  record for the third time — see decision 5.
- **Widen the drain tail's partial glob to run while rows survive**, which ADR-0494 decision 2 named
  as "#1565's question". Rejected as an answer to it. That tail unlinks files; it cannot retain a
  row, so widening it cannot produce a retry — it would only move a live sibling's partial into
  unlink range, which is the hazard ADR-0442 §7's row test exists to prevent. The question was worth
  asking and the answer is that the fix belongs in the row-driven path.
- **Widen the pin classifier so a System that requested a base pins it until its download cannot be
  in flight** (#1558's option 2). Not rejected — out of scope. `torn_down` and `failed` carry no
  "was provisioning" evidence, so it needs new durable state, and this change is the cheap half that
  needs none. It remains the only thing that closes the residual window above.
- **Hold the `INVESTIGATION` lock across the fetch.** Rejected. It serializes every uploaded-rootfs
  download against System bind for the length of a multi-GiB transfer, on a lock the bind path takes
  transaction-scoped. The `flock` exists precisely so liveness needs no shared lock.
- **Probe before the pin gate.** Rejected as strictly more work for the same answer: a pinned base is
  not reclaimed, so the `readdir` would be spent on the steady state and thrown away.
