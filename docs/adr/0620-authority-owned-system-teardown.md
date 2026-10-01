# 0620 Authority-owned System teardown

## Status

Accepted (2026-09-06)

> **Amended by [ADR-0711](0711-bound-acknowledged-retry-grant-per-budget.md) (#2960):** the
> reconciler also ends a `boot` job past the acknowledged-retry grant bound, and retires or
> supersedes its live authority.

## Context

External-boot host domains, overlays, and recovery data can be private to the
authority host. Ordinary worker provisioning resolves its own libvirt URI and
cannot safely tear down that state. A completed external release stops
restricting admission but does not transfer host ownership.

## Decision

Systems with durable external-boot history use an authority-marked TEARDOWN
job selected from the newest activation's durable provider route. The local
authority performs host destruction inside its authenticated commit lane. The
worker commits only authority journal facts; it never receives a provider
mutation capability.

`torn_down` is an external-boot activation state for physical System teardown.
It is not `abandoned`. Cleanup and capacity accounting remain separately proven:
ready reservations credit once after cleanup, pending reservations never credit,
and quarantine retains ownership until authority disposition completes. A clean
release keeps its original release and cleanup evidence.

### Amendment (2026-09-28): an unresolved teardown owns the System (#2884)

Only a teardown may allocate authority for a System while any of these holds: a teardown
authority is `allocating` or `current`; the journal head is a teardown operation at `admitted`,
`mutation-started`, `provider-returned` or `observed`; the head carries a suspended teardown.
Any other purpose gets `superseded` and writes no authority row (migration 0161). Before this,
only a `current` teardown authority fenced allocation. An activate could supersede a teardown
takeover in flight and recover the teardown under an activate authority, with no teardown
commit or credit.

A public `systems.teardown` re-runs a `failed` authority teardown job when the marker it would
enqueue is identical. It resets that job to a fresh queued attempt under the System lock. Live,
`succeeded` and `canceled` jobs, and a marker mismatch, still replay. Authority rows are looked
up by id, generation, and a live state, never by `(job_id, job_attempt)` alone. A reset attempt
counter therefore cannot revive a superseded row, and the reservation still credits once.

A teardown's operation identity is fixed per activation and its attempt id per identity, so a
reclaimed or new teardown takes over with the interrupted teardown's identity. ADR-0584's
same-operation integrity rule binds only the operation's own phase records (`admitted` through
`terminal`), not `watermark-installed`, `takeover-superseded` or `takeover-acknowledged`, which
keep their own fences; the takeover anchors and recovers the suspended teardown. Recovery
observation and the final proof select the `mutation-started` record of their own generation.

Rejected for this amendment:

- **Fence only a `current` teardown authority (no change).** verified: the #2880 live proof
  (PR #2883) left an activate authority allocating generation 5 over the generation-4 teardown
  takeover.
- **A new job per public teardown.** verified: `jobs.dedup_key` is unique (migration 0001), and
  the teardown dedup key is fixed per System.
- **Give the takeover record the suspended operation's attempt id.** verified:
  `AuthorityTakeoverRequestV1` has no attempt field (`protocol.py`, commit 727de823e), and the
  worker sends the takeover before it can read the head.
- **Recycle every terminal teardown job.** judgment: a `succeeded` teardown already credited the
  reservation, and re-running it only adds a replay path to the exactly-once rule.

### Amendment (2026-09-28): an exhausted authority job does not wedge `running` (#2889)

The public teardown also re-runs a `running` authority teardown job whose final attempt's lease
has lapsed (`attempt >= max_attempts`, `lease_expires_at` before the database clock), with the
identical marker. The lease is judged in the recycling `UPDATE` itself, so a live final attempt
replays unchanged. That job keeps its attempt counter and gains another `max_attempts` budget
rather than resetting to 0, because its dead attempt may still be running: a fresh attempt with
the same number on the same incarnation would pass that attempt's heartbeat and finalize fences.
A `succeeded` job is still never reset.

The reconciler dead-letters an authority-marked non-teardown job (`failed`, `lease_expired`)
only when it is `running`, exhausted, lease-lapsed and has no `allocating` or `current`
authority row, checked after locking the job row (migration 0162). Every path that can commit a
receipt needs such a row and a `running` job, and allocation and the losing-result classifier
recheck the job under its own row lock, so no receipt can commit for it; the receipt paths keep
sole ownership of every job they could still finish. A stray whose authority is `allocating` or
`current` waits until a newer allocation supersedes it.

Rejected for this amendment:

- **Terminalize every lapsed exhausted marked job in the reconciler.** judgment: it would end
  jobs whose `current` authority can still commit, splitting terminalization from the receipt.
- **Cancel, then recycle `canceled`.** judgment: two public calls for one recovery, and
  `jobs.cancel` flips a running job without any authority check.
- **Recycle a `succeeded` teardown when the prior read showed `running`.** verified: the server
  locks the System with a blake2b key (`db/locks.py` `_lock_key`) and the commit function with
  `hashtextextended('kdive:system:' ...)` (migration 0122), so an old attempt can commit
  between the read and the reset.

### Amendment (2026-09-29): an exhausted retained teardown is dead-lettered (#2917)

A `retained_quarantine` teardown receipt on the job's final attempt (`attempt >= max_attempts`)
ends the job `failed` with `error_category = 'conflict'` and the teardown authority `retired`,
in the receipt transaction (migration 0164). This is what the `fail` path does at exhaustion.
Before, the job was requeued where no worker can claim it, and the authority was `superseded`,
which could leave the activation with no `current` or `retired` dispatch route for the public
teardown. A non-final attempt still requeues and supersedes. The receipt and the `retained`
result are unchanged, and a `retired` authority cannot commit again. Migration 0164 also moves
authority-marked teardown jobs that were already `queued` and exhausted, and the superseded root
authority of their retained receipt, to the same states. Recovery is the public teardown's
`failed` recycle defined above, so the reservation still credits once. The reconciler's
external-boot repair lanes (`reconciler/repairs/external_boot.py`) now see such a job as not live,
as they already do after a `fail` at exhaustion. A successor they enqueue serializes with a public
recycle through the per-System lock and the authority fences.

Rejected for this amendment:

- **Do nothing.** verified: issue #2917 records a live job `queued` at attempt 24/24 that stayed
  unclaimed through a worker restart; its reservation is never credited.
- **Grant one more attempt at exhaustion, as the authority-System retained path does
  (migration 0149).** judgment: deterministic retained churn (#2901) would then retry without a
  bound and never show a terminal state.
- **Recycle a `queued` exhausted row in the public teardown.** judgment: it widens a generic
  queue policy for a state one writer produces, and leaves the row stranded until a caller acts.
- **A reconciler lane that dead-letters queued exhausted teardowns.** judgment: a periodic sweep
  and a new security-definer function for a state the finalizer can prevent in its own
  transaction.
- **Keep the authority `superseded` and widen the dispatch route to `superseded` rows.**
  judgment: a `superseded` row can be one that lost an allocation race, so the route would rest
  on rows the fences already treat as dead.

### Amendment (2026-09-29): observing another generation's teardown record (#2921)

The host teardown record is keyed by activation, and a later generation of the same subject
(`binding`, `plan_identity`, `provider_kind`, `authority_instance`) overwrites it at `begin`.
Observing generation N against a record that does not match N's anchor now distinguishes three
cases in both libvirt providers:

- another subject, or the same generation with different anchor fields, still refuses
  (`provider_conflict`);
- a later generation's record means N is superseded: the provider raises
  `SystemTeardownSupersededError` and the service answers `superseded` with no facts;
- an earlier generation's record means N never reached `begin`: the observation reports facts
  owned by N's anchor with no reservation and no completion time, exactly as when no record
  exists.

N therefore never credits from another generation's record. A predecessor-owned observation
proves only `retained_quarantine`; the reservation credits once, through the generation whose
exact anchor owns the record, provided that generation's `begin` adopts it; `begin` still
requires an identical reservation, and a reservation that changed between generations is not
addressed here. Observation still writes nothing. Takeover recovery of a
generation that died between `mutation-started` and `begin` now reaches `terminal` instead of
failing every takeover with `provider_conflict`. In recovery, a later generation's record cannot
occur: a generation begins only after its own `takeover-acknowledged`, which requires every
earlier teardown phase resolved. It can occur when a concurrent successor begins before the
superseded generation's post-commit re-observation.

Rejected for this amendment:

- **Key the record by generation.** judgment: a persisted-format change and migration of host
  files, to keep history nothing reads; the successor must still see its predecessor's progress.
- **Observe with the retained record's own anchor.** judgment: N would report, and could credit,
  a reservation owned by another generation.
- **Answer `superseded` for a predecessor record too.** verified: the takeover that recovers N is
  N's successor, so `superseded` would fail every takeover exactly as `provider_conflict` does.

### Amendment (2026-09-30): no ordinary teardown or release strands an authority domain (#2966)

The ordinary `teardown_handler` refuses an unmarked TEARDOWN job when the System has any
external-boot activation, not only one that still restricts it. It raises the existing
terminal `conflict` (`external_boot_teardown_not_supported`) under the System lock, before the
`tearing_down` transition and any provider call. Before, after a clean release (`recovered`,
`cleanup_complete`), the ordinary provisioner acted on its own libvirt URI, found no domain,
and committed `torn_down` while the authority daemon kept the domain running (#2865 proof).

`allocations.release` (and break-glass release, host drain, and the orphaned-active reaper)
refuses with `conflict` (`external_boot_system_teardown_required`, with the `system_id`) while a
System on the allocation that is not `torn_down` has external-boot history. The authority
allocator admits a teardown only on an `active` allocation (`0122_external_boot_authority.sql`),
so a released allocation would leave that System with no teardown path. This mirrors the
pre-activation fence that already denies release for an authority-owned System (ADR-0623).
Lease expiry still ends such an allocation; that path is tracked in #2992. It is also the only
end for an allocation whose System the authority teardown cannot take (a pre-fix `tearing_down`
System, or an unresolved authority route), and a platform operator outside the project cannot
clear it, because `systems.teardown` needs the project `admin` role.

The public `systems.teardown` replaces an ordinary `{system}:teardown` job in state `failed`, or
`canceled` before any worker claimed it, with the authority-marked teardown (recycle policy
`TERMINAL_OR_CANCELED`, entered only for those two states). An ordinary job in any other state
still returns `ordinary_teardown_fenced_by_external_boot`. After this fence an ordinary job for
such a System makes no provider call, so replacing it skips no mutation. The recycle keeps the
job's `authorizing` value, so the authority commit's audit row names the principal that enqueued
the refused job, such as the reconciler.

Producers that enqueue through `enqueue_control_teardown` (the orphaned-System lane,
investigation force-close, break-glass teardown) still enqueue an unmarked job for such a
System; the worker refuses it until `systems.teardown` runs.

Rejected for this amendment:

- **Do nothing.** verified: the #2865 proof (issue #2865 comment 5903037345) committed
  `torn_down` through the ordinary path while the authority domain kept running.
- **Route in `enqueue_control_teardown`.** judgment: `build_external_boot_payload` needs a
  `ProviderResolver`, which `JobOperations`, break-glass and the reconciler lane would each have
  to carry; the worker refusal is still needed for jobs already queued.
- **Refuse in `enqueue_control_teardown`.** judgment: the orphaned-System lane would log the
  refusal on every pass, and investigation force-close would roll back a close whose System an
  admin can tear down afterwards; the worker refusal is still needed for jobs already queued.
- **Route in the worker.** verified: `ExternalBootOperations.run` reads the marker from
  `job.payload`, and only the enqueueing server mints it under the System lock
  (`jobs/handlers/external_boot/router.py`, `mcp/tools/lifecycle/systems/admin.py` at
  2e0d9eae7).
- **Refuse in the worker without the public recycle.** verified: `_enqueue_authority_teardown`
  returns `conflict` for any ordinary prior (`admin.py` at 2e0d9eae7), and `jobs.dedup_key` is
  unique, so the refused job would block every supported teardown.
- **Admit an authority teardown on a released allocation.** judgment: it changes the ADR-0584
  allocation binding in a migration; the release refusal keeps the allocation `active` for the
  common path at no schema cost.

### Amendment (2026-10-01): the stalled `tearing_down` lane skips external-boot history (#3015)

`repair_stalled_tearing_down_systems` reads the System's latest external-boot activation under
the System lock. A System with any activation row is skipped: the lane enqueues nothing and
logs one WARNING per System per process, naming the activation. This replaces the re-enqueue
the #2966 amendment accepted for such Systems, which wrote a failed attempt every pass. A prior
`{system}:teardown` row carrying an authority marker is never recycled by this lane, so an
unmarked payload cannot overwrite it. The supported exit for such a System is tracked in #3026.

A skipped System stays in the lane's candidate set, so it spends one slot of the per-pass limit
each pass. The set is the finite residue of pre-fix ordinary teardowns (no new System reaches
`tearing_down` with history), and the in-lock visit is what emits the warning, so the candidate
query does not exclude it. Only 100 or more such Systems would crowd out a recoverable one.

## Consequences

The server fails closed when historical authority routing is unavailable.
Authority commit recovery, rather than read-only observation, must resume an
interrupted host teardown. Migration 0147 widens the activation state and
teardown completion constraints.

## Considered & rejected

- **Worker calls ordinary provisioning teardown.** **verified:**
  `providers/local_libvirt/lifecycle/provisioning.py` builds its connection from
  `KDIVE_LIBVIRT_URI`, while authority composition receives a fixed provider
  socket. That can address a different daemon and bypasses authority ownership.
- **Reuse `abandoned`.** **verified:** the activation model requires abandoned
  terminal evidence with outcome `abandoned`; physical teardown does not prove
  that outcome.
- **Make recovery observation resume destruction.** **verified:** observation
  is a read-only classification boundary. Resuming a mutation there would give
  a recovery read capability destructive semantics.
