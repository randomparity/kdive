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
   before the first unlink.** Liveness is asked of the kernel, exactly as ADR-0446 and ADR-0452
   already ask it — not re-derived from a System state column a third time. The pin gate stays first
   because it is the cheaper question (rows plus one `stat`) and a pinned base is not reclaimed
   either way.

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

4. **The gate is read-only and fail-closed, and `staging_partials` grows a second entry point
   rather than having its collector predicate re-read.** `unlink_partial_if_unheld` is a *collector*:
   its `bool` merges "locked it, so it is a crash orphan" with "could not be evaluated", which is
   correct there because both call for the same inaction on the only thing it touches — the file. A
   caller that reads that `False` as "no live writer" and then deletes a staged base, an object-store
   object and an `artifacts` row is doing #1558's data loss with extra steps: an `EACCES` partial
   under the uid asymmetry ADR-0442 documents in this same subsystem, or an `EMFILE` under descriptor
   exhaustion (likeliest exactly when many stagings are in flight), is a file the reclaim **cannot
   show is dead**.

   So the `flock` mechanics move into a shared `_probed` returning a five-valued `_Liveness`, and the
   module exposes two mappings over it. `live_writer_may_hold_partial` treats `UNEVALUABLE` as
   "may be held" — the same fail-closed rule `_overlay_pins_base` already applies to a failed `stat`
   one gate earlier in the same function — and it never unlinks. Sharing the probe rather than the
   predicate is what keeps the call sites from drifting on what the kernel's answers *mean* while
   letting them differ on what to *do*, which they genuinely must.

   A crash orphan needs no collecting here: `UNHELD` does not defer, so leaving the file costs
   nothing, and `sweep_investigation_staging_dir` remains its collector exactly as before.

5. **`UNLOCKABLE` proceeds, and is the one answer the gate does not fail closed on.** On
   `ENOLCK`/`EOPNOTSUPP` the writer's own `_flocked_partial` degraded and staged unguarded, so no
   answer is available for any file on that filesystem, ever. Deferring there is not caution but a
   permanent refusal to reclaim any uploaded base on that host — the never-terminating shape
   ADR-0452 §5 rejects — so the reclaim degrades to its pre-ADR-0495 behaviour. Nor may the gate
   *unlink* there, which is what re-reading the collector's `unlink_when_unlockable=True` would have
   done: it would destroy the only copy of a writer that may well be live, on exactly the hosts where
   nothing can prove otherwise. The drain tail's own policy for that case is unchanged.

6. **The directory walk is `os.scandir`, not `Path.glob`, and a walk fault defers too.** `Path.glob`
   swallows the `OSError` and yields nothing, so an unreadable staging directory is indistinguishable
   from an empty one. That is a real fail-open path rather than a theoretical one: unlinking a known
   name needs write and execute on the directory, **not** read, so at mode `0o333` the glob would
   report no candidates while `_unlink_staged_base` behind it succeeded. `ENOENT` on the directory
   stays the achieved post-state for an investigation that never staged anything.

7. **`rootfs_cleanup_pending_at` is not set on an open investigation.** Rejected here for the third
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
- **A permanently unevaluable partial permanently retains its checksum's row, base and object.** The
  cost of decision 4's fail-closed rule, and it is a real permanence: an `EACCES` partial under a uid
  asymmetry does not heal on its own. It is deliberately not the never-clearing *marker* ADR-0452 §5
  rejects — no durable state is pinned, the row stays inspectable, and the drain tail still clears
  `rootfs_cleanup_pending_at` on that condition exactly as ADR-0452 decided. It also adds no new
  permanence class: a base whose `unlink` faults permanently already retains its row forever under
  ADR-0442 §4's fault contract, and the same `EACCES` that blocks the probe blocks that `unlink`. The
  `WARNING` from the shared probe is the operator signal, and the alternative — deleting the last
  copies of a SENSITIVE base this process cannot show is dead — is strictly worse.
- **On a filesystem that cannot `flock`, the fix is a no-op and the pre-existing race is unchanged.**
  Decision 5's cost, stated rather than derived. `ENOLCK`/`EOPNOTSUPP` (NFS with a dead lock manager,
  some FUSE and 9p backends) leaves the reclaim exactly as it behaves on `main` today. That is the
  floor, not a regression, and the alternatives are both worse: deferring forever leaks every base on
  such a host, and unlinking the unguarded partial destroys a possibly-live writer's only copy.
- **The gate never unlinks, so it adds no filesystem mutation to the `INVESTIGATION`-lock critical
  section** beyond the `scandir` below. Collecting a partial stays `sweep_investigation_staging_dir`'s
  job on every path, which is also why the drain tail's `unlink_when_unlockable=True` and its ADR-0452
  §4 marker rule are untouched by this ADR.
- **Cost is one `scandir` of a per-investigation directory per due checksum, on the
  `INVESTIGATION`-lock critical section** — the same section, and the same order of magnitude, as the
  three globs ADR-0494 already put in the drain tail. The probe runs only after the pin gate has
  passed, so a pinned base (the steady state for the whole grace window) does not pay for it.
- **A deferral is logged at `WARNING` from two frames.** The shared `_probed` reports the *file*
  observation and `_reclaim_one_checksum` reports the *decision* and what it retained. The job
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
  record for the third time — see decision 7.
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
  not reclaimed, so the `scandir` would be spent on the steady state and thrown away.
- **Reuse `unlink_partial_if_unheld` and read its `bool`.** Rejected — it was the first shape of this
  change and it is wrong twice over, which is what decisions 4 and 5 record. Its `False` merges
  "proven crash orphan" with "could not be evaluated", so the gate would delete three copies of a base
  it cannot show is dead; and no value of `unlink_when_unlockable` is right for a gate, since `True`
  destroys a possibly-live writer's unguarded partial while `False` would have to mean defer-forever.
  Sharing the underlying probe instead keeps the anti-drift property that predicate was reached for.
- **Have the gate collect the crash orphans it walks past.** Rejected as an unjustified side effect.
  The rationale for it does not hold: an `UNHELD` candidate does not defer either way, so nothing is
  gained by removing it here, and `sweep_investigation_staging_dir` already collects it in the same
  job. A gate that mutates the filesystem to answer a question is also a gate that cannot be run
  speculatively.
