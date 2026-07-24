# ADR 0442 — Investigation-rootfs reclaim runs as a worker job, not in the reconciler

- **Status:** Accepted
- **Date:** 2026-07-24
- **Supersedes:** [ADR-0441](0441-investigation-scoped-uploaded-rootfs.md) §6's *execution model* — the
  per-pass `os.stat` co-location probe ("directory existence is the co-location signal"), the
  reconciler-side staged-base unlink and staging-dir sweep, and the object-before-file reclaim
  order. ADR-0441 §6's **policy** is retained unchanged: the two worklists (close+grace via the
  dedicated `rootfs_cleanup_pending_at` marker, and the mandatory TTL backstop), the two-condition
  overlay-absence liveness gate with its referencer enumeration, the "never drop the `artifacts`
  row while the SENSITIVE object or file survives" ordering contract, and the 404/`ENOENT`-tolerant
  fault contract. Every other ADR-0441 decision (§1–§5, §7, §8) is untouched.
- **Depends on:** [ADR-0441](0441-investigation-scoped-uploaded-rootfs.md) (the reclaim policy this
  re-homes), [ADR-0018](0018-job-queue-worker-execution.md) (the durable `jobs` queue and its
  `dedup_key` admission), [ADR-0273](0273-observe-rotating-console-parts.md) (`console_rotate`, the
  precedent for a reconciler sweep that enqueues a worker job instead of doing the work itself),
  [ADR-0016](0016-repository-layer-locks-idempotency.md) (the `INVESTIGATION` advisory-lock scope).
- **Spec:** [`../specs/2026-07-24-rootfs-reclaim-worker-job-1522-design.md`](../specs/2026-07-24-rootfs-reclaim-worker-job-1522-design.md)

## Context

ADR-0441 put the whole investigation-rootfs reclaim — S3 object delete, staged-base `unlink`,
`artifacts` row delete, staging-dir `rmdir` — inside two reconciler sweeps. #1522's live proof on a
KVM/libvirt host showed that this cannot work in the deployment shape ADR-0441 §8 scopes the
feature to.

`scripts/live-stack/up.sh` runs the **worker as root** (it needs root for install-staging and VM
ops) and the **server and reconciler as the invoking user**. The root worker creates
`/var/lib/kdive/rootfs-uploads/<inv>/` as `root:root 0755` and stages the base as `qemu:qemu 0644`.
The unprivileged reconciler can `os.stat` that directory but cannot `unlink` inside it. ADR-0441
§6's fail-closed gate is `rootfs_dir_accessible()` — `os.stat` + `S_ISDIR` — so it probes
**stat-ability, not writability** and admits a pass that cannot finish:

1. `store.delete(object_key)` succeeds — the S3 object is gone.
2. `_unlink_staged_base()` raises `PermissionError` → the checksum defers.
3. The `artifacts` row delete is never reached; `rootfs_cleanup_pending_at` never clears.

The staged multi-GiB SENSITIVE base and its row are retained **forever**, the sweep re-fails every
30 s, and because both sweeps route through the same helper, the TTL backstop fails identically —
there is no reclaim path at all in this deployment.

The stat-vs-write mismatch is the proximate cause, but the deeper problem is that the probe is
trying to answer a question it cannot answer. The #1502 `/challenge` iteration-5 finding already
established that **directory existence is not co-location**: an operator who pre-creates the
staging root (say, by adding it to `KDIVE_HOST_RUNTIME_DIRS` alongside `/var/lib/kdive/rootfs`)
makes a *non*-co-located reconciler read "accessible", every overlay then stat as absent, and the
sweep mass-deletes SENSITIVE bases still backing live guests on the real host. ADR-0441 §6 kept
that hazard at bay only by forbidding the reconciler to `mkdir` — a prohibition no deployment tool
enforces. Strengthening the probe (`os.access(W_OK|X_OK)`, or a create/unlink probe) fixes #1522's
half-reclaim but leaves both the co-location question and the leak: the base still is not
reclaimed, only more loudly not reclaimed.

A grep of the reconciler establishes the relevant invariant: the investigation-rootfs sweep is the
**only** site where the reconciler mutates the host filesystem. Every other repair is DB-only,
S3-only, or libvirt-API-only. ADR-0441 introduced the exception; #1522 is the bill for it.

## Decision

### 1. The reconciler enqueues; the worker reclaims

The two sweeps stop touching the filesystem and the object store. Each becomes a **DB-only
worklist scan that enqueues a `reclaim_investigation_rootfs` job**, and the worker performs the
entire reclaim: liveness gate, staged-base unlink, object delete, `artifacts` row delete,
staging-dir sweep, and the `rootfs_cleanup_pending_at` clear.

This is the `console_rotate` shape (ADR-0273): a reconciler sweep that decides *what* needs doing
from the database and hands the host-touching half to the worker. `console_rotate` exists precisely
because the reconciler may not be co-located with the files; ADR-0441 §6 cited that precedent but
then adopted the opposite half of it, keeping the file work in the reconciler and tolerating
non-co-location by *degrading*. Degrading is right for console rotation (a missed rotation is
re-attempted next pass and loses nothing); it is wrong for reclaim (a permanently deferred reclaim
is a permanent SENSITIVE-data leak, which is the outcome ADR-0441 §6 exists to prevent).

Routing the filesystem work to the worker restores the reconciler's DB/S3-only invariant and puts
the unlink in the **same process that created the file**. That is what dissolves #1522: the root
worker owns the staging tree by construction, so no cross-user permission gap can exist.

### 2. Co-location becomes structural, and the probe is deleted

`rootfs_dir_accessible()` is removed outright. It is not replaced by a stronger probe, because
after decision 1 there is nothing left to probe: the worker that claims a local-libvirt job **is**
the libvirt host by definition.

This is not a new assumption — it is the *same* assumption the code that creates the staged base
already makes. The provision handler stages `rootfs-uploads/<inv>/<token>.qcow2` on whatever worker
claims the `provision` job, and backs the guest's overlay on that local path. A local-libvirt
deployment whose workers were not the guest host would already be unable to provision at all. So
reclaim-on-the-worker is *no weaker* than the staging it reverses, and it replaces a probe that
could be fooled with a structural guarantee that cannot.

The consequence is that the sweep no longer defers when the reconciler cannot see the host
filesystem — a split reconciler/libvirt-host topology, which ADR-0441 §6 documented as "simply
defers rootfs reclaim", now reclaims correctly. That deployment shape gains a working reclaim it
never had.

The `ReconcileConfig.rootfs_dir` / `rootfs_uploads_dir` fields become unreachable and are removed
rather than left as dead configuration; the worker reads the `ROOTFS_DIR` / `UPLOADS_DIR` constants
from `providers/shared/runtime_paths`, the same constants the staging path writes through.

### 3. The gate is re-evaluated by the worker, under the `INVESTIGATION` lock

ADR-0441 §6's two-condition gate is retained verbatim in behavior — for every System bound to the
investigation whose `provisioning_profile` references **this** checksum, both (a) its per-System
overlay file is absent on the host and (b) it is not in a pre-overlay/re-materializing state
(`ROOTFS_BASE_PRE_OVERLAY_SYSTEM_STATES`) — but it now runs on the worker, immediately before the
unlink, rather than on the reconciler one enqueue earlier. The reconciler does **not** pre-evaluate
it: a gate decision made a pass ago is stale by the time the job runs, and duplicating the
overlay probe on a process that may not be co-located is the mistake this ADR removes.

The worker evaluates the gate and performs the reclaim for one checksum inside a single transaction
holding the **`INVESTIGATION` advisory lock**. This closes a race ADR-0441 left open. `provision`
admission takes that same lock and holds it transaction-scoped until the new System row commits
(ADR-0441 §2/§7), so a bind either commits before the gate reads and is seen as a `defined`
referencer (condition (b) pins the base), or waits behind the reclaim and finds the object gone and
re-uploadable. Previously the gate read and the unlink were unsynchronized against that bind, and a
System could be inserted between them, opening its backing file onto a base the sweep was already
unlinking. Holding a network-call-bearing transaction under the `INVESTIGATION` lock follows the
existing upload reaper (`cleanup/uploads.py:reap_one_owner`), which does the same with
`store.list_prefix` + `store.delete`.

The gate is evaluated per checksum, and a checksum that is pinned is **not an error**: the handler
skips it, leaves its row, and returns having reclaimed fewer objects. Pinning is the expected
steady state while an investigation's Systems are alive.

### 4. Unlink the staged base first, then delete the object, then the row

ADR-0441 §6's order was object → file → row. It becomes **file → object → row**.

The row-last invariant — the reason ADR-0441 pinned an order at all — is unchanged and still
holds: the `artifacts` row is the worklist anchor and the only download handle, so it is deleted
only after both the SENSITIVE local file and the SENSITIVE object are gone. Neither can be orphaned
beyond a reaper.

What changes is which half survives a mid-reclaim fault, and #1522 is the demonstration that the
old choice was the wrong one. Under object-first, a failure at the unlink leaves the *local* file:
a multi-GiB SENSITIVE base, still resolvable and still bootable (`fetch_uploaded_rootfs` checks
`dest.is_file()` before any store call), whose object is gone — unrecoverable and, as #1522 shows,
possibly unreclaimable. Under file-first, a failure at the object delete leaves the *object*: no
SENSITIVE bytes on the guest host, and the object is re-downloadable, so a re-provision that races
the reclaim re-stages the base rather than booting a base whose backing object no longer exists.
Both intermediate states converge on retry — the unlink is `ENOENT`-tolerant and the delete is
404-tolerant, so the next attempt finds the completed step already done — but only one of them is
safe to be stuck in. The file is the copy worth losing first.

### 5. Fault contract: pinned is success, a real fault fails the job

ADR-0441 §6's "404/`ENOENT` is success, any real fault defers the whole checksum before the row
delete" is retained. What changes is how a real fault is **surfaced**. In the reconciler it was a
`WARN` line, which is what let #1522 run for 5+ passes as nothing but log noise.

Now: a checksum whose gate pins it is skipped and the job **succeeds** (having reclaimed the rest).
A checksum that hits a real unlink or store fault is skipped, the remaining checksums are still
attempted, and the job then **fails** — surfacing in the `jobs` table and in the worker's failure
metrics. A reclaim that cannot make progress is a SENSITIVE-data-retention problem and must be
loud, not a log line.

Note the reach of that signal precisely: `jobs.list`'s `investigation_id` filter joins `runs` on
`payload->>'run_id'`, and a reclaim job carries no `run_id`, so the job is listable **by kind**
(and by state), not by the investigation it reclaims. Widening that filter is a `jobs.list` change
outside this fix; until then, `kind=reclaim_investigation_rootfs` plus the per-fault `WARN` naming
the object key is the operator path.

### 6. One reclaim job per investigation, recycled — not one per pass

The sweeps run every ~30 s. Enqueuing a fresh job per pass would grow the `jobs` table without
bound (≈2 880 rows/day for one stuck investigation) and would stack duplicate reclaims.

Each enqueue therefore uses a **stable `dedup_key`** — `rootfs-reclaim:<investigation_id>` — so the
sweeps hold at most **one** reclaim job row per investigation. Admission is gated in the sweep
rather than delegated to `queue.enqueue`'s `recycle_terminal`, because two properties of that
recycle are wrong at sweep cadence:

- **Ordering.** `dequeue` claims by `ORDER BY created_at`, and the recycle resets state in place
  without touching `created_at`. A job recycled every pass would keep its original timestamp
  forever and therefore sort ahead of every job enqueued after it — a permanently faulting
  background reclaim would head-of-line-block provisioning and every other interactive job. The
  sweep instead deletes the settled row and inserts a fresh one, re-dating the reclaim to the pass
  that decided it is due. Nothing references `jobs.id`, so the delete is free of fallout.
- **Retry rate.** A recycle every pass means a faulting reclaim re-runs twice a minute, each
  attempt holding the `INVESTIGATION` lock across object-store deletes. A settled job therefore
  keeps its slot until `ROOTFS_RECLAIM_RETRY_BACKOFF` (5 minutes) has elapsed. Reclaim is
  grace/TTL-governed in days, so a few minutes of backoff costs nothing and converges just as
  fast. `max_attempts` is `1` for the same reason: an in-job retry of a permission wall or a dead
  store buys nothing the next sweep does not — the sweep *is* the retry loop.

A `queued`/`running` job is left untouched, which is the in-flight dedup. A `canceled` job is
treated as settled like any other terminal state, so an operator `jobs.cancel` on this kind stops
the current attempt but does **not** stop reclaim: the next sweep past the backoff re-issues it.
That is deliberate — the slot is reconciler-owned, and a `canceled` row wedged in a stable
per-investigation slot would silently disable reclaim for that investigation forever, which is the
failure mode this ADR exists to remove. But it means cancel on this kind is **advisory**, worth
roughly one backoff interval, and nothing should be built on it as a stop. The kind is absent from
`CONTRIBUTOR_CANCELABLE_JOB_KINDS`, so only an operator can cancel it at all.

Preserving the failure record trades against the same backoff. A `failed` reclaim row survives for
at least one backoff interval before the next sweep replaces it, so it is inspectable through
`jobs.list` for minutes rather than seconds — but it is not a permanent audit trail, and the
per-fault `WARN` naming the object key remains the durable record.

The two worklists cannot collide on that shared key: the close-driven sweep selects investigations
with `rootfs_cleanup_pending_at` set (set only at close), and the TTL sweep pins
`i.state IN ('open','active')`. A closed investigation is in neither `open` nor `active`, so the
two selections are disjoint by construction.

### 7. Drain bookkeeping and the marker move to the worker

ADR-0441 §6's single-pass `drained` flag — "clear `rootfs_cleanup_pending_at` when every rootfs
object of this investigation drained in *this* pass" — does not survive the split, because the pass
that decides and the process that acts are now different. It is replaced by a **state query, not a
loop variable**: after attempting every checksum in its payload, the handler re-reads whether
**any** `owner_kind='investigations'`/`retention_class='rootfs'` row remains for the investigation,
under the same `INVESTIGATION` lock. Only when none remains does it sweep the staging directory
(glob-unlink stale `*.partial`, then `rmdir` the now-empty per-investigation dir) and clear
`rootfs_cleanup_pending_at`.

Reading the real post-state rather than tracking a per-pass flag is what makes this correct across
a worker that dies mid-reclaim, a job whose payload is a stale due-set, and a concurrent finalize
that commits a new row: each is just a different answer to "are there rows left?".

Recovery from a dead worker is worth stating exactly, because it is **not** the sweep's own doing.
A worker that dies mid-reclaim leaves its job `running` with a lapsed lease, and the sweep's
admission gate treats `running` as in flight — so the slot stays held. The existing
`repair_abandoned_jobs` reconciler repair is what frees it: it dead-letters a running job whose
lease has lapsed and whose `attempt >= max_attempts`, which `max_attempts=1` makes true on the very
first claim. Only once that repair has marked the job `failed` does the reclaim sweep see a settled
slot and, past the backoff, re-issue. Worst-case latency to resume is therefore lease expiry plus
one reconcile pass plus one backoff interval. The re-issued job resumes from the real state, since
every completed step is an `ENOENT`/404 no-op.

The condition also subsumes ADR-0441's TTL-side `_investigation_has_rootfs_objects` guard on the
staging-dir sweep (a remaining row means a live fetch may be writing a `*.partial` that must not be
clobbered), so both sweeps now share one drain rule instead of two.

For the same reason the close-driven sweep enqueues a job even when a past-grace investigation has
**no** rootfs rows, carrying an empty worklist. That job does nothing but run the drain tail — which
is exactly the point: it is the only path that reaps a crash-orphaned SENSITIVE `*.partial` no row
owns and clears the marker. Short-circuiting the empty case in the reconciler would either strand
that orphan or put the filesystem write back in the reconciler that decision 1 removes, and it
would split one drain rule into two.

The marker clear is unconditional on the drain rather than on which sweep enqueued the job. A TTL
job only ever runs against an `open`/`active` investigation, whose marker is NULL, so the clear is
a no-op there; the handler needs no discriminator and carries none.

### 8. Payload carries the due row set

The payload is `{investigation_id, artifact_ids}`. The reconciler's two sweeps differ only in
*which* rows are due — all of the investigation's rootfs rows for the close-driven sweep, only
those past `KDIVE_INVESTIGATION_ROOTFS_RETENTION_DAYS` for the TTL backstop — and passing the
selected ids keeps that retention policy in the reconciler, where the configuration lives, rather
than duplicating grace/TTL settings into the worker. The handler re-reads each row by id under the
lock and skips ones already gone, so a stale due-set is self-correcting.

Migration **0078** widens the `jobs_kind_check` constraint to admit
`reclaim_investigation_rootfs`, following `0053_console_rotate_job_kind.sql` and
`0072_capture_traffic_job_kind.sql` (drop-and-recreate, keeping the constraint name stable for the
SQL↔enum tie `test_migrate.py` asserts).

## Consequences

- Investigation-rootfs reclaim works on the host-process local-libvirt deployment
  `scripts/live-stack/up.sh` ships — the deployment ADR-0441 §8 scopes the feature to, and the one
  where it was completely non-functional. Both sweeps are fixed by the same change.
- The reconciler is DB/S3-only again. The one exception ADR-0441 introduced is gone, and the
  invariant is worth stating because it is what makes the reconciler safe to run anywhere.
- A stuck reclaim becomes a `failed` job with a category and a failure context, listable by
  `kind` (not by `investigation_id` — see decision 5), rather than only a `WARN` line. The row is
  replaced by the next re-issue past the backoff, so it is a bounded-window signal, not a permanent
  audit trail; the per-fault log line naming the object key remains the durable record.
- A reclaim now costs one job round-trip of latency after the sweep observes it is due. Reclaim is
  a grace/TTL-governed background activity measured in days, so a sub-minute delay is immaterial.
- The `INVESTIGATION` advisory lock is held across an object-store delete per checksum, and
  `close_investigation`, `runs.create`, and System bind all contend on it. This is not confined to
  quiet investigations: the TTL backstop reclaims **live** `open`/`active` ones by design. The
  delete is therefore given an explicit wall-clock budget (`_STORE_DELETE_TIMEOUT_S`), so a
  degraded store bounds the lock hold instead of stalling a bind for the client's whole retry
  budget; a timeout is treated as a real fault (defer the checksum, keep the row), and the request
  landing late afterwards is harmless because the retry is 404-tolerant. The upload reaper
  (`cleanup/uploads.py`) sets the precedent for holding this lock across store calls, though its
  `investigations` arm is gated to terminal owners, so it does not by itself justify the live case.
- A split reconciler/libvirt-host topology gains working local-libvirt reclaim, which ADR-0441 §6
  explicitly deferred.
- A multi-worker-host local-libvirt deployment would route a reclaim job to a host that may not
  hold the staged base. That host's overlay probes read "absent", so the gate would not pin, and it
  would `ENOENT` its way to deleting the object and row while another host kept the file. Stated
  plainly: in that topology the new model **fails open** where the deleted probe failed closed.
  That is an accepted consequence of the trade, not an oversight — the probe's fail-closed behavior
  was itself unreliable (it read directory existence, which #1502's `/challenge` iteration 5 showed
  is not co-location, and #1522 showed is not writability), and the same deployment already cannot
  provision correctly, because `provision` stages the base on whichever worker claims it. Reclaim
  is therefore exactly as host-assuming as the staging it reverses, and no more. Local-libvirt
  remains single-host per ADR-0441 §8; the remote lane (#1433/ADR-0440) is per-System-lease and
  unaffected. Pinning the reclaim to the staging host (a dedicated dispatch lane, or recording the
  staging worker on the row at fetch time) is the shape a future multi-host local lane would need,
  and is deferred with that lane rather than built speculatively now.
- `ReconcileConfig` loses two fields and the two repair metrics are renamed from `*_gc_count` to
  `*_reclaims_enqueued`, because they now count enqueues rather than reclaimed bases. Naming them
  for what they count keeps the reconcile report honest.

## Considered & rejected

- **Strengthen the probe to test writability** (`os.access(W_OK|X_OK)`, or a create/unlink probe).
  The smallest change, and it does convert #1522's half-reclaim into a clean defer. But it leaves
  the base unreclaimed — a declared leak instead of a silent one — and leaves the reconciler
  mutating the host filesystem, so the co-location question and the pre-created-empty-dir hazard
  both survive intact.
- **Make the staging tree group-writable**, with the worker creating it under a shared group and
  the deployment placing both daemons in that group. Keeps ADR-0441 §6 intact and is a small code
  change, but it introduces a shared-group contract every deployment (compose, helm, Ansible,
  bare host-process) must honor and that nothing enforces — a new operational invariant whose
  violation reproduces #1522 exactly, only harder to diagnose. It also still leaves the reconciler
  writing to the guest host's filesystem.
- **Pre-create the staging root in the deployment** (add it to `KDIVE_HOST_RUNTIME_DIRS` the way
  `/var/lib/kdive/rootfs` already is). Cheapest operationally, and **unsafe**: it is precisely the
  pre-created-empty-dir case the #1502 `/challenge` iteration-5 fix guarded against — a
  non-co-located reconciler reads the empty dir as "co-located", overlay-absence then reads
  "nothing pins these bases", and the sweep mass-deletes live SENSITIVE bases.
- **Keep the object-before-file order.** Rejected in decision 4: it is the order that made #1522's
  failure unrecoverable rather than merely deferred.
- **Have the handler re-derive the due set from grace/TTL configuration** instead of carrying
  `artifact_ids`. Would require the worker to resolve the reconciler's retention settings, putting
  the same policy in two processes that can disagree, for no gain — the reconciler already ran the
  query to decide the job was needed.
- **A fresh `dedup_key` per pass** (the `console_rotate` pattern, `<key>:<uuid4>`). Correct for
  rotation, which is continuous and short-lived, but here it would accumulate a job row every 30 s
  for every investigation whose reclaim is pinned — and pinning is the normal state for the whole
  grace window.
