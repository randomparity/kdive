# 0515 — A flock-held fetch lease pins an uploaded rootfs base while its download is in flight

## Status

Accepted (2026-07-30)

## Context

#1558 offered two ways to stop a reclaim deleting a staged uploaded-rootfs base out from under a
live download. ADR-0495 implemented **option 1**: `_reclaim_one_checksum` probes for a
`<token>.*.partial` whose `flock` a live writer holds, and defers the whole checksum before any
unlink. That satisfied #1558's written acceptance criterion, and ADR-0495's own Consequences named
three residual windows it did not close.

#1702 carries **option 2**, for the substantive one — ADR-0495's *window 2*: a fetch that has
resolved its `artifacts` row but has not yet created its partial. At gate time there is no partial
to probe, so option 1 is blind to it, and the window is not short. `fetch_uploaded_rootfs` resolves
the row and *then* waits on the per-(investigation, checksum) session advisory lock, which can be
held for a sibling's entire multi-GiB download before this fetcher opens anything.

Nothing else closes it either, and the reason is worth stating precisely, because it is what
ADR-0495 deferred on. The ADR-0441 §6 pin classifier answers "does a System pin this base?" from the
System's state column plus overlay-file presence, and both terminal states a doomed provision
reaches defeat it:

- `_ROOTFS_REFERENCERS_SQL` (`rootfs_reclaim.py`) selects `WHERE investigation_id = %s AND state
  <> %s` with `torn_down` bound, so a `torn_down` System is never enumerated and cannot pin at all.
- `ROOTFS_BASE_PRE_OVERLAY_SYSTEM_STATES` (`domain/capacity/state.py`) is `{PROVISIONING,
  REPROVISIONING, RESTORING}`. `FAILED` is absent, so a `failed` System pins only through the
  overlay-file stat — which returns false, because a System that died mid-fetch never got an
  overlay.

`PROVISIONING -> TORN_DOWN` and `PROVISIONING -> FAILED` are both legal, the download runs detached
under `asyncio.to_thread` and cannot be cancelled, and nothing serializes the two: the fetch takes
only its own session lock, never the `INVESTIGATION` lock the reclaim holds.

ADR-0495 recorded why it did not simply widen the classifier: `torn_down` and `failed` carry no
"was provisioning" evidence, so option 2 needs new durable state. That is the gap this record
closes, and the shape of the state is the whole decision.

## Decision

### 1. The evidence is a `flock`-held lease file, not a column

A fetcher creates `<token>.<uuid>.fetching` in the investigation's staging directory and holds an
exclusive `flock` on it for the whole fetch. The reclaim's per-checksum gate probes it with exactly
the predicate ADR-0495 already applies to the partial (`live_writer_holds_staging_file`), so a
`torn_down` or `failed` System's base is pinned for as long as — and only as long as — a fetcher is
alive.

The **bound** is why the evidence is a file rather than a `systems` column, and it is not a
stylistic preference. A column set by a fetcher that is then `SIGKILL`ed is never cleared. Nothing
in this system reverses it: `failed` is terminal with no transition out of it, `torn_down` is the
achieved post-state, and `remove_overlay` runs only from teardown. A base pinned by a dead fetcher
would therefore be pinned forever — a SENSITIVE leak of up to the 50 GiB canonical cap, per
investigation, with no collector. That is precisely the regression
`test_failed_referencer_with_overlay_gone_drains` (AC-8) exists to catch, and shipping it under the
banner of fixing a race would have been a strictly worse bug than the one being fixed.

A `flock` has no such failure mode. The kernel drops it when the holding descriptor closes,
**including on `SIGKILL`**, so the pin cannot outlive the process that took it. This is the third
time this subsystem has reached the same conclusion (ADR-0446 for the fetch-side sweep, ADR-0452 for
the drain tail, ADR-0495 for the row-driven gate); ADR-0515 applies it to the one window those three
left open rather than introducing a fourth mechanism.

### 2. The lease brackets the partial, and both are probed

The lease is taken **before** `_resolve_object` and released when `fetch_uploaded_rootfs` returns or
raises. It therefore strictly contains the partial's window rather than overlapping it, and it
covers the session-lock wait that made window 2 long.

The partial is still probed. That is not defence in depth for its own sake: across a rolling worker
upgrade, a fetcher started before this change is mid-download holding a partial and **no** lease, so
dropping the older evidence would reclaim its base out from under it and reintroduce #1565 for the
length of the deploy.

### 3. What clears the lease, on every exit path

| exit | what clears it |
|---|---|
| normal return (including the reuse fast path) | the `finally` closes the descriptor and unlinks the file |
| `CategorizedError` / `OSError` raised inside the fetch | the same `finally` |
| worker killed (`SIGTERM` sets an asyncio stop Event; `SIGKILL` runs nothing) | the kernel drops the `flock` at process exit, so the pin ends immediately; the leftover file is collected by the two sweeps below |

The leftover file **must** be collected somewhere, or it keeps the investigation's staging directory
non-empty forever and fails every `rmdir` with `ENOTEMPTY` — the per-investigation leak
`_unlink_completion_markers` was added for one file-kind over. So both existing collectors take it
on the same `flock`-gated terms they take a partial: `_unlink_orphan_staging` on the next fetch of
that base, and `sweep_investigation_staging_dir`'s drain tail. A held lease defers the drain exactly
as a held partial does, which keeps ADR-0452 §4's rule intact: the drain marker is retained only for
causes that are *provably* transient.

### 4. AC-8's property is preserved by leaving the classifier alone

The issue title proposes widening the pin classifier. This record deliberately does not, and
`domain/capacity/state.py` is unchanged. `pinned_rootfs_tokens`, `rootfs_base_reclaimable`, and
`ROOTFS_BASE_PRE_OVERLAY_SYSTEM_STATES` all keep their existing semantics, so:

- **`test_failed_referencer_with_overlay_gone_drains` (AC-8) is unchanged and still passes.** A
  `failed` System with no fetch in flight drains exactly as it did. There is no user-visible change
  to when a failed System's base is reclaimed.
- **`test_torn_down_referencer_is_excluded` is unchanged and still passes.** A `torn_down` System
  with an overlay present is still not a referencer.

Both were reconciled by *not* moving the new evidence into the classifier, which is what keeps their
property — a terminal System can never pin its base forever — true by construction rather than by
argument. One test is added beside them,
`test_the_state_classifier_is_not_widened_by_the_fetch_lease`, which holds a live lease for a failed
System's own token and asserts `rootfs_base_reclaimable` still says "reclaimable": the separation of
duties is now pinned, so a later "simplification" that adds `FAILED` to the pre-overlay set reddens
next to AC-8 instead of quietly replacing it.

### 5. No schema change

There is none, and none is wanted. The evidence has to be released by something that runs when a
worker is `SIGKILL`ed, and no Postgres construct available here does that soundly: a durable column
leaks forever (§1), and a session advisory lock fails the *other* way — ADR-0446 established that it
belongs to a connection idle for the whole download and can be reaped out from under a live writer,
which would drop the pin early and reopen the race. Adding a table or column to satisfy a shape the
design does not need would be new durable state with a worse safety story than the file.

The reclaim already depends on being co-located with the fetcher's filesystem — ADR-0442 records
that co-location as structural, and ADR-0495's existing probe `scandir`s that same staging directory
— so the lease is readable wherever the gate it feeds already runs. This adds no new assumption.

## Consequences

The substantive residual ADR-0495 recorded is closed: a reclaim can no longer delete a staged base
for a checksum whose download has resolved its `artifacts` row but not yet created its partial.

Two residuals remain, both unchanged and both narrower than what they replace:

1. **The probe-to-unlink instant.** Sub-syscall, and not addressable by any longer wait.
2. **A staging file the probe cannot evaluate** (`EACCES`, `ENOLCK`, `EOPNOTSUPP`). The reclaim
   proceeds as it did before ADR-0495, and the `WARNING` is the operator's signal. Deferring here
   would strand the checksum permanently and silently, per ADR-0452 §5 — the lease inherits that
   rule rather than relitigating it.

Two conditions leave a fetch **unleased**, both degrading to exactly the pre-ADR-0515 behaviour with
a `WARNING` rather than failing the provision, on `_flocked_partial`'s own `ENOLCK` precedent: a
staging directory the fetcher cannot create or write, and a filesystem that cannot `flock` at all
(where the reclaim could not read the lease either). A third is reported and not retried — a
concurrent sweep taking the lease in the create-then-lock gap, which is ADR-0446 §3's two-syscall
window; the pin is advisory and the partial's own `flock` still guards the transfer, so a retry loop
against a sweeper would buy an ADR-0452 §5 non-termination risk for nothing.

Each fetch now costs three extra syscalls (`open`, `flock`, `unlink`) on a path that already stats
the base and is about to move multiple GiB. A reuse-fast-path hit pays them too, and still returns
without a download.

This does **not** pin a base across the gap between `fetch_uploaded_rootfs` returning and the
caller creating its overlay. That gap predates this change and is unaltered by it.

`live_writer_holds_partial` is renamed `live_writer_holds_staging_file` and
`_unlink_orphan_partials` to `_unlink_orphan_staging`, because both now answer for two file kinds;
`_live_writer_holds_a_partial` becomes `_live_fetch_in_flight` for the same reason.

## Considered & rejected

**Add `FAILED` to `ROOTFS_BASE_PRE_OVERLAY_SYSTEM_STATES`** — the edit the issue title suggests, and
a one-line change. Rejected: `failed` is terminal with nothing that transitions out of it and
nothing that removes such a System's overlay, so the pin would never release and the base would leak
until an operator intervened. It also would not work, because it answers the wrong question — a
terminal state says nothing about whether a download is in flight, which is why ADR-0495 deferred
option 2 rather than taking this shortcut. AC-8 is the standing guard against it.

**Include `torn_down` in `_ROOTFS_REFERENCERS_SQL`** — same defect, arrived at from the other side,
and worse: `torn_down` is the achieved post-state of every System that ever existed, so every
investigation would accumulate permanent pins.

**A `systems.rootfs_fetch_started_at` column (or a per-(investigation, checksum) lease table) with a
TTL** — the shape a migration would have taken. Rejected on two counts. The TTL has to exceed the
longest legitimate download of a base up to the 50 GiB cap, so the pin outlives a crashed fetcher by
that whole margin; and choosing the margin trades a leak window against reopening the race, with no
value that does neither. A heartbeat to avoid the TTL puts a liveness protocol on the provision hot
path to answer a question one `flock` already answers exactly.

**A session `pg_advisory_lock` as the pin** — appealing, since the fetch already takes one.
Rejected: ADR-0446 established that a session lock belongs to a Postgres *connection* which sends
nothing for the whole download and can be reaped by an idle-connection timeout or a terminated
backend. It would release the pin while the writer is still writing, which is the failure this
record exists to prevent.

**Reuse the partial as the lease by creating it earlier** — no new file kind. Rejected: the partial
is the download target, so creating it before the object is resolved means creating it before the
fetch is known to be legitimate, and an early-`O_EXCL`d partial would have to be reconciled with
`stage_uploaded_rootfs`'s own create-and-`flock`. A zero-byte lease with its own suffix keeps the
two lifetimes separate and lets the collectors treat them identically.

**Drop the partial probe now that the lease supersedes it** — one mechanism rather than two.
Rejected for the rolling-upgrade reason in §2; the two are one predicate over two candidate sources,
not two mechanisms.

## References

- Issue #1702 (this record), #1558 (option 2 as originally stated), #1565, #1544, #1522
- ADR-0495 — reclaim defers a live-held checksum (option 1; its Consequences name window 2)
- ADR-0446 — the `flock` gate on the fetch-side orphan sweep, and why a session lock is not liveness
- ADR-0452 — the same gate on the drain tail; §5 on what may and may not pin a drain
- ADR-0441 §5/§6 — staging concurrency and the pin classifier
- ADR-0442 — reclaim ordering, and the worker/staging co-location this relies on
- ADR-0494 — token-keyed staging collection
