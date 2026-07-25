# ADR 0452 — The reclaim-side staging sweep is `flock`-gated too

- **Status:** Accepted
- **Date:** 2026-07-25
- **Completes:** [ADR-0446](0446-flock-guarded-orphan-partial-sweep.md), whose Consequences record
  this sweep as an open residual and name #1544. The primitive ADR-0446 introduced is reused, not
  re-derived.
- **Amends:** [ADR-0442](0442-rootfs-reclaim-worker-job.md) §7 — the drain tail's *unconditional*
  `rootfs_cleanup_pending_at` clear gains exactly one exception.
- **Amends:** [ADR-0441](0441-investigation-scoped-uploaded-rootfs.md) §5 — the reclaim-side
  backstop sweep no longer derives its safety from "no rootfs row remains".
- **Spec:** [`../specs/2026-07-25-flock-guarded-reclaim-staging-sweep-1544-design.md`](../specs/2026-07-25-flock-guarded-reclaim-staging-sweep-1544-design.md)

## Context

ADR-0446 fixed **one** of the two places that unlink a `<token>.*.partial`. The other,
`sweep_investigation_staging_dir` in `jobs/handlers/artifacts/rootfs_reclaim.py`, glob-unlinked
unconditionally — and over a *wider* glob than the fetch-side sweep, since it takes every token in
the investigation directory rather than one base's.

Its safety was **derived** rather than held, and ADR-0446's own Consequences record the derivation
failing. The claim was that the sweep runs only once no committed rootfs row remains for the
investigation, so no live fetcher for that base can exist. But the row count reaches zero only
because `rootfs_base_reclaimable` classified the base as unpinned, and that gate reads the System
row's **state column** plus overlay-file presence: `_ROOTFS_REFERENCERS_SQL` filters `torn_down` out
entirely, `ROOTFS_BASE_PRE_OVERLAY_SYSTEM_STATES` is `{defined, provisioning, reprovisioning,
restoring}` so `failed` pins nothing either, and a *provisioning* System has no overlay file yet.
`PROVISIONING -> TORN_DOWN` and `PROVISIONING -> FAILED` are both legal transitions.

Meanwhile the download runs detached under `asyncio.to_thread`, which cannot be cancelled and keeps
writing whatever any other actor does to the row, and nothing serializes the two: the fetch takes
only the per-(investigation, checksum) session lock, never the `INVESTIGATION` advisory lock the
reclaim holds. So a concurrent teardown drops the pin, the reclaim deletes the last rootfs row, and
the very next statement sweeps a **live** partial — #1524's defect on the path #1524 did not touch.
The fetcher writes on into an unlinked inode and fails at `os.replace`, with the blocks charged to
`df` yet invisible to every path-matching tool.

The condition is already conceded in-tree in three places: ADR-0446's Consequences, this function's
docstring, and `_require_still_linked`'s `_DOWNLOAD_WINDOW` message, which names "the
investigation-reclaim backstop sweep took it (#1544)" as one of its two causes.

## Decision

1. **`_unlink_if_unheld` moves to `providers/shared/staging_partials.py` as
   `unlink_partial_if_unheld`**, and both sweeps call it. It is provider-private today and
   `src/kdive/jobs/` imports nothing from `providers/local_libvirt/` — a job handler reaching into a
   provider's lifecycle package inverts the direction the layout encodes, while
   `providers.shared.runtime_paths` is already imported by this very handler. Behaviour is otherwise
   unchanged: `os.open(O_RDONLY|O_NONBLOCK)`, non-blocking `flock(LOCK_EX|LOCK_NB)`, `unlink`, with
   a `WARNING` on each of the skip branches and every fault handled **per candidate** so one
   unsweepable file cannot truncate a pass. A second implementation was rejected outright: two
   copies of a two-syscall liveness test drift, and the ADR-0446 derivation lives in that docstring.

2. **A filesystem that cannot `flock` at all is a caller decision, `unlink_when_unlockable`, with no
   default.** ADR-0446 §4 put `ENOLCK` in the same "cannot evaluate" branch as `EACCES` and
   `EMFILE`, and §5 justified that for the fetch side by "collection falls to the
   investigation-reclaim sweep" — which was true only because that sweep unlinked unconditionally.
   Sharing the helper unchanged would therefore have made the reclaim backstop skip **every**
   candidate on precisely the hosts where the fetch-side gate had already degraded, and clear the
   drain marker on the way out: the last collector for a SENSITIVE multi-GiB orphan, retired
   silently, as a side effect of adding a guard. That is a different question from "this one
   candidate resists evaluation" and it is answered per caller.

   `False` on the fetch side, keeping ADR-0446 §4 exactly. `True` on the reclaim side, where it is
   not a new risk but the **pre-ADR-0446 behaviour of that same sweep**, now confined to the case
   where the kernel refuses to answer — the writer on such a host staged unguarded too, so the lock
   protocol carries no information in either direction and skipping protects nothing. It also
   restores the truth of `_flocked_partial`'s own operator-facing degrade `WARNING`, which already
   tells an operator that "collection falls to the investigation-reclaim sweep". The argument has no
   default because inheriting an answer to it is how the gap was created.

3. **`sweep_investigation_staging_dir` calls it per candidate** instead of
   `partial.unlink(missing_ok=True)`. Its safety stops depending on row-state classification, which
   is the same move ADR-0446 made on the fetch side. The glob and the unlink-before-`rmdir` ordering
   are untouched.

4. **The held-branch `WARNING` reports the observation, not a fetch-side inference.** Its text
   asserted that "this fetcher acquired the rootfs fetch lock while a sibling was still
   downloading — its Postgres session was lost mid-transfer, and this download is redundant", which
   is simply false on the reclaim path, where no fetch lock exists. It now states that a live writer
   holds the partial, that it is left in place, and that it is collectable once its holder's
   descriptor closes. That is `_release_fetch_lock`'s own principle in this same subsystem: report
   the observed state rather than the inferred cause, so a conditional is not written down as an
   invariant. Both derivations remain in the docstring and in the ADRs.

5. **The drain marker is retained for a live-held skip, and only for that.**
   `sweep_investigation_staging_dir` returns whether it skipped a partial because a live writer holds
   its `flock`; `_finish_drained_investigation` then skips the
   `rootfs_cleanup_pending_at = NULL` update in that one case, so the close-driven reconciler sweep
   re-issues the reclaim job past its 5-minute backoff and the next pass converges. `rmdir` keeps its
   `suppress(OSError)` and its position: `ENOTEMPTY` becomes the achieved post-state of a live-held
   skip rather than something a reader discovers.

   Both neighbouring choices are wrong, in opposite directions. **Clearing unconditionally** would
   leave the skipped partial with no collector at all — the marker is the only thing that re-enqueues
   a reclaim job for a closed investigation, and the fetch-side opportunistic sweep only fires on the
   *next fetch of that base*, which never comes once the investigation is closed. If the live holder
   is then killed mid-download, its multi-GiB SENSITIVE partial leaks permanently, which is exactly
   what the backstop exists to prevent. **Retaining on every non-drain** would be worse in the other
   direction: an unopenable or unlinkable partial (`EACCES` under the uid asymmetry ADR-0442
   documents in this same subsystem, `EROFS`, `EIO`) is permanent until an operator acts, so
   retaining on it resurrects the never-clearing marker and the re-fail-every-pass loop ADR-0442's
   Context is written about. A held `flock` is the one outcome that is *provably* transient: the
   kernel releases it when the holding descriptor closes, including on process exit, normal or
   `SIGKILL`.

## Consequences

- A live fetcher's partial survives the reclaim sweep. Both partial-unlinking paths in the tree are
  now gated on the same kernel-answered liveness question, and neither derives its safety from a
  Postgres lock or from a System's state column.
- Crash-orphan collection keeps its reach and its latency: an unheld orphan is unlinked in the same
  pass, and a `SIGKILL`ed holder's orphan is collectable the instant its descriptor closes, with no
  timeout to age out. Reach narrows in the same one way ADR-0446 §4 already accepted — a partial this
  process cannot `open` is no longer unlinked blind — and here that narrowing has no further
  backstop behind it, which is why each such skip is a `WARNING`.
- Two syscalls per candidate are added to a pass that runs once per drained investigation. Immaterial.
- One investigation in ten thousand keeps its `rootfs_cleanup_pending_at` set for one extra reclaim
  cycle (5-minute backoff) per live-held partial. The reconciler sweep is DB-only and the re-issued
  job's worklist is empty, so the retry costs one job row and one `INVESTIGATION` lock acquisition.
- **The guard is best-effort, and the residue is `_flocked_partial`'s documented degrade, not this
  gate.** On a filesystem that cannot lock at all — `ENOLCK` on an NFS mount whose lock manager is
  down, `EOPNOTSUPP` on some FUSE and 9p backends — the fetcher stages *unguarded* with a `WARNING`
  (ADR-0446 §5) and this sweep unlinks its partial under decision 2. The outcome there is exactly
  the pre-ADR-0446 one, which is the point of degrading rather than failing an entire lane, and
  `_require_still_linked`'s `_DOWNLOAD_WINDOW` message names it as the usual cause of a vanished
  partial. Claiming the live partial is now safe unconditionally would be the same kind of derived
  invariant this ADR exists to delete. `_DOWNLOAD_WINDOW` also stops *asserting* that cause: a lock
  dropped by lock-manager recovery, or anything outside kdive removing the file, leaves the same
  state, and asserting one cause in an error message is the defect decision 4 removes one file over.
- **`ENOLCK` from kernel lock-record exhaustion is transient rather than a property of the
  filesystem, and decision 2 does not distinguish the two.** A reclaim sweep that hits it would
  unlink a live partial. That is the pre-ADR-0446 behaviour for a condition that is already rare and
  already accompanied by the attributable `_DOWNLOAD_WINDOW` fault, and the alternative — skipping —
  is the leak decision 2 exists to prevent. Named rather than left for a reader to derive.
- **The pin-dropping classification is untouched, and the object delete still races the download.**
  `rootfs_base_reclaimable` still lets `PROVISIONING -> TORN_DOWN` and `PROVISIONING -> FAILED` drop
  the pin, so `_reclaim_one_checksum` still deletes the base, the object and the row while a
  detached download streams. This gate preserves the partial's *bytes*; it does not preserve the
  *fetch*, which on the gzip path then reads a deleted object through its ranged GETs and fails with
  a store-shaped `INFRASTRUCTURE_FAILURE`. The drain tail's `WARNING` names that condition — which is
  diagnosis, not prevention — and **#1558** carries the fix (run the same `flock` probe inside
  `_reclaim_one_checksum` and defer the checksum). It is not a substitute for this change: a sweep
  whose correctness rests on a state column is the defect class both ADRs are removing.
- **A doomed fetcher can now publish a base nothing owns, and that shape is new.** Before this
  change the sweep destroyed the live partial, so the fetcher died at `_require_still_linked` and
  published nothing. Now it runs to completion and `os.replace`s onto `<token>.qcow2` whose
  `artifacts` row the reclaim already deleted — a staged base with no row, in a closed
  investigation, which no sweep reclaims. That trade is deliberate: the previous behaviour's "no
  leak" was an invisible-inode leak plus a failed provision, and a path-addressable file an operator
  can find and an orphan-base sweep can later collect is the better of the two. **#1559** carries it,
  with the operator-side detection recipe in the meantime, because collecting orphan *bases* is a
  different worklist from collecting orphan partials.
- No schema, no migration, no config setting, no new dependency, no MCP/RBAC surface. Not an AI
  surface. `#1539` adds a sidecar completion marker to this same directory and this pass keeps the
  shape it needs: a second, non-`flock`ed glob added to the same function.

## Considered & rejected

- **Import `_unlink_if_unheld` from `providers.local_libvirt` into the job handler.** Rejected. It
  is the smallest diff and the wrong direction: `src/kdive/jobs/` has no other import from that
  package, and a private helper reached across a package boundary is a coupling nobody will find
  when the provider is refactored.
- **A second liveness helper local to `rootfs_reclaim.py`.** Rejected. Two implementations of the
  same two-syscall test drift, and the one that drifts is the one without ADR-0446's derivation
  attached to it.
- **An mtime window on the reclaim side.** Rejected for ADR-0446's reasons, which are unchanged
  here: a tunable that is wrong in both directions under exactly the conditions that produce the
  bug, and wrong on facts outside the process's control (clock skew, coarse mtime granularity).
- **Take the `INVESTIGATION` advisory lock in the fetch so the reclaim cannot interleave.**
  Rejected. It would hold a transaction-scoped lock across a multi-GiB download, blocking every bind
  and close in the investigation for minutes, and the fetch runs on an autocommit connection
  precisely so no transaction stays open (`idle_in_transaction_session_timeout`). Serializing a
  download behind an investigation-wide lock trades a rare race for a routine stall.
- **Clear the marker unconditionally and simply document the surviving directory.** Rejected. It is
  the smaller change and it silently removes the last collector for the very partial the new gate
  just decided to protect.
- **Share the helper unchanged and let the reclaim sweep skip on a lock-less filesystem, recording
  the leak as a residual.** Rejected. It is the smallest diff and it is a *regression*: on such a
  host the sweep that unlinked unconditionally before this change would collect nothing at all,
  clear the drain marker, and leave a SENSITIVE multi-GiB orphan with no collector — a strictly
  worse outcome than the behaviour being replaced, shipped under a guard whose purpose is safety.
