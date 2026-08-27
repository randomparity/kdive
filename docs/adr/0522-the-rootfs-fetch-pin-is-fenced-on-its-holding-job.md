# 0522 — The rootfs fetch pin is fenced on its holding job, not on a derived deadline

## Status

Accepted (2026-07-30)

Discharges the deferral recorded in
[ADR-0515](0515-a-durable-fetch-lease-pins-a-base-in-flight.md)'s `## Considered & rejected`
("Fence on the holding job instead of a deadline"), and retires its §3 and §4. ADR-0515 is not
superseded as a whole: §1, §2, §5 and §6 are in force unchanged, and the amendment appended to its
`## Decision` records the boundary.

## Context

ADR-0515 pins an uploaded-rootfs base during an in-flight fetch with a `rootfs_fetch_leases` row
carrying a **6-hour `expires_at`**. Migration 0087 states in prose why it had to (lines 19-28), and
ADR-0515 §3 states it again: the lease could not borrow ADR-0502's fence, because

> the provision seam hands it a `RootfsUploadContext` with no job identity, and `UploadFetch` is a
> bare `(ctx) -> Path` callable — so there is nothing to fence on without plumbing a job id through
> the provider seam.

That is a statement about the seam, not about the mechanism, and ADR-0515 named its own better
answer in the same record: `object_write_leases` (ADR-0502, migration 0084) is the structurally
identical lease, and it declines a deadline entirely because its liveness *is* its holding job's —
`jobs.state = 'running' AND jobs.lease_expires_at > now()`, which `dequeue` reclaims the complement
of and the worker's `heartbeat` renews for as long as the handler runs.

The cost of not having that handle is stated plainly in ADR-0515's `## Consequences`: **a fetcher
killed by `SIGKILL` pins its base for up to 6 hours.** Its staged base, its object and its
`artifacts` row are all retained until the row expires. Bounded, visible in `rootfs_fetch_leases`,
reclaimed with no operator action — but for those 6 hours a base of up to the 50 GiB canonical cap
is not returned, per investigation.

The 6 hours is derived rather than picked (ADR-0515 §4): 50 GiB ÷ a 5 MiB/s floor rate = 2 h 51 m
for one full-cap transfer, doubled because the lease is deliberately taken *before* the
per-(investigation, checksum) session lock and so covers a sibling's whole transfer and then its
own, rounded up. The floor rate is chosen for an asymmetry rather than measured — expiring under a
live fetcher silently reopens the race, so erring long costs a bounded visible leak while erring
short costs correctness. That reasoning is sound and is exactly why the constant cannot simply be
tuned down. What it cannot do is be *right*: there is no value that neither leaks nor expires early,
because the constant is answering a liveness question with an estimate.

#1740 is the follow-up ADR-0515 named for itself.

## Decision

### 1. The provision seam carries the job that owns the call

`Provisioner.provision` and `Provisioner.reprovision` take a keyword `job_id: UUID | None`. The job
handler supplies `job.id` at `_provider_lifecycle_call`, and local-libvirt threads it through
`MaterializeRootfs` into `RootfsUploadContext`, which the `UploadFetch` callable already receives.
The other three providers accept and ignore it: fault-inject mints a synthetic domain and stages
nothing, and remote-libvirt's base volume is staged by the operator ahead of provisioning rather
than downloaded inside the call, so neither records durable in-flight state for a reclaim to race.

It is a threaded argument rather than a contextvar. The tree already has a `job_id` log-context var
(ADR-0014) that propagates across `asyncio.to_thread`, and reading it here would have touched no
signatures. Rejected: this value is not diagnostic context. A caller that fails to supply it
silently unfences a multi-GiB download, and the failure is invisible — the provision succeeds, the
reclaim races it, nothing raises. An argument the type checker requires is the only form of that
obligation a future call site cannot forget, and it is the same reason ADR-0502's own
`hold_write_lease` takes `job_id` explicitly.

`None` is permitted and means "no job owns this call". It is reachable from
`validate_rootfs_ref`, the admission-time validator, which materializes with a zero `system_id` and
no job at all. A `None` there is not an error; it simply has no fence.

### 2. `job_id NOT NULL REFERENCES jobs (id) ON DELETE CASCADE`, and `expires_at` is dropped
(migration 0090)

The column that replaces the deadline is not optional. A lease naming no job satisfies no liveness
test and nothing in this subsystem ever clears it — `failed` is terminal, `torn_down` is the
achieved post-state — so it would pin its base until an operator noticed, which is strictly worse
than the 6 hours it replaced. The database refuses it rather than trusting every writer.

`expires_at` is dropped rather than left unread. Leaving it would leave a writer able to set it and
a reader able to believe it: two definitions of when this pin ends, one of them dead. Both indexes
that carried it go with it, and `rootfs_fetch_leases_job_id_idx` is added — the FK gives no index on
the referencing side, and the reap now selects on the join.

`ON DELETE CASCADE` is `object_write_leases`' own rule for the same relationship.

The number skips 0088 and 0089, which two sibling branches held while this was authored.
[ADR-0517](0517-migration-numbers-are-strictly-ascending-across-merges.md)'s guard requires a
version strictly above the base branch maximum rather than exactly one above it, so an abandoned
number is a legitimate gap; taking 0090 makes this branch's gate independent of which sibling
merges first, instead of coupling three PRs' merge order to one integer.

Rows predating the migration are deleted. They carry no holder, so under a `NOT NULL` column there
is nothing to put in them; they are transient evidence about in-flight downloads rather than records
of anything. §5 states what that costs.

### 3. The pin predicate is `LIVE_HOLDER_SQL`, imported rather than restated

`fetch_lease_pins_base` asks whether any row for this `(investigation_id, token)` is held by a job
that is still a live claim, using `kdive.artifacts.uploads.write_lease.LIVE_HOLDER_SQL` verbatim. This is
its third reader, after `object_write_leases`' own fence and the reconciler's orphan sweep
(`reconciler/cleanup/upload_fences.py`).

Sharing the constant is the point rather than an economy. A hand-written second copy of "the holder
is still alive" could drift from the one the job runner enforces, and it would drift silently: too
strict withholds reclaim from live jobs, too loose deletes bytes a live download is writing.

`reap_dead_fetch_leases` (renamed from `reap_expired_fetch_leases`) carries the **same** predicate
negated, so the pass that honours a lease and the pass that collects one cannot disagree. A reap
looser than the fence would delete a row that is actively protecting a transfer.

Both halves are evaluated by Postgres, so no worker clock enters the comparison — the property
ADR-0515 §3's last paragraph established, unchanged and now inherited rather than restated.

### 4. Nothing else about ADR-0515 changes

§1 (a row per holder), §2 (the lease is taken before `_resolve_object`), §5 (the reap is hygiene,
not correctness) and §6 (the state classifier is not widened) are in force exactly as written.
`domain/capacity/state.py` is untouched and `test_rootfs_reclaim_gate.py`'s AC-8 cases are
unmodified, as #1702 left them.

ADR-0495's `flock` probe stays, for the reason ADR-0515's own "Why ADR-0495's `flock` probe stays,
restated" section gives: it fails open, so it can only ever withhold a reclaim and never license
one. That argument does not depend on what the database marker is fenced on.

## Consequences

**ADR-0515's residual is closed, not narrowed.** A fetcher killed by `SIGKILL` still releases
nothing, but its pin now lapses when the worker stops heartbeating that job's lease — the job-lease
interval — rather than after a worst-case transfer estimate. The derived constant, the 50 GiB
numerator and the 5 MiB/s floor rate all go away with §4; there is no longer a number to tune, and
no asymmetry to reason about, because the pin's validity is no longer an estimate of anything.

**A fetch that outlives its job's lease loses its pin.** This is the direction ADR-0515 §4 called
the silent failure, and it is worth naming rather than assuming away. It cannot arise from a slow
transfer: the worker heartbeats the job lease for as long as the handler runs, and the provision
handler is the frame the download runs inside (`asyncio.to_thread`), so a legitimately slow stage
keeps renewing. It arises only where the worker has stopped heartbeating — which is the condition
the fence is *for*. Where ADR-0515's deadline could expire under a live fetcher on a slow host, this
cannot, because the thing being tested is the fetcher's own liveness rather than a proxy for it.

**A lane with no job stages unleased.** `acquire_fetch_lease` degrades with a `WARNING` and records
nothing, on the same `ENOLCK` precedent as its database-fault path: the reclaim reverts to its
pre-ADR-0515 reach, which is a rare and survivable race, where failing would turn it into a total
uploaded-rootfs provisioning outage. The log line is the only evidence — the provision succeeds
either way — so it says what is missing and what that costs.

**Rolling upgrade.** While 0090 is applied but a pre-0090 process is still running, an old fetcher's
`INSERT` names a dropped column and takes the existing acquire-fault degrade (unleased, logged); an
old reclaim's pin probe names it too and raises, which fails that reclaim job **before** it deletes
anything. Both directions fail toward retention, and both end when the process restarts. The
migration's own row deletion has the same shape and the same bound: one reclaim pass may proceed as
it did before ADR-0515 for a fetch straddling the upgrade, and ADR-0495's `flock` probe still covers
that fetch once it reaches its partial.

**The `Provisioner` port is one parameter wider,** and three of its four implementations ignore it.
That is the price of the seam: the parameter has to exist at the port for the handler to pass it
uniformly, and a local-libvirt-only keyword would have made the dispatcher provider-aware.

## Considered & rejected

**Tune the 6-hour TTL down.** Rejected, and ADR-0515 §4 is why: the constant sits at the top of a
derivation whose numerator is the canonical upload cap and whose denominator is a floor rate chosen
so the deadline never fires on a slow-but-working transfer. Lowering it trades the leak window
against expiring under a live fetcher, and that second failure is silent. There is no value that
does neither, which is the argument for removing the constant rather than moving it.

**Read the job id from the `bind_context` log contextvar.** Rejected in §1: correctness on a
logging channel, with a silent failure mode and nothing that requires a caller to bind it.

**Resolve the holding job inside the fetch, from `jobs.payload->>'system_id'`.** Appealing because
migration 0082 already indexes exactly that expression, and it would have needed no seam change at
all. Rejected: it infers identity rather than carrying it. The fetch would be asking "which job
looks like it might own this System?" and treating the answer as the thing its pin's liveness rests
on — correct today only because the System lifecycle happens to serialize its jobs, and silently
wrong the first time that stops holding. It also makes a reclaim's correctness depend on a payload
shape no schema constrains.

**Bind the job id into the `UploadFetch` closure at provider construction.** Rejected on lifetime:
`LocalLibvirtProvisioning` is built per Resource by `providers/local_libvirt/composition.py`, not
per job, so the closure would capture whichever job happened to construct it and every later
provision would fence on a stale holder — a lease that reads as protection and lapses at the wrong
time.

**Keep `expires_at` as a backstop alongside the job fence.** Rejected: a second, independent
deadline would have to exceed the longest legitimate transfer, and nothing in the tree bounds one —
the identical argument ADR-0502 makes for its own lease. Too small silently reopens the race; large
enough to be safe is the 6 hours this record removes. Two fences also mean two answers to "when does
this pin end", and the looser one is the only one that matters, so the tighter one is decoration.

**Move `LIVE_HOLDER_SQL` to a neutral module** (`jobs/` or `db/`) now that three subsystems read it.
Rejected for this change: it is a rename touching two files outside this issue's reach and buys
nothing today, since `kdive.artifacts` is already imported freely from the provider path. Worth
doing when a fourth reader appears, and named here so the next reader knows it was weighed.

**Delete ADR-0515 §4 in place rather than appending an amendment.** Rejected because it is not
available: the records gate's `E-REWRITE` rule counts every line a merged record's section had and
the head does not, so both an outright deletion and the strikethrough form `docs/adr/README.md`
describes for partial supersession fail it. The appended-note form is what ADR-0015 itself uses for
its two partial supersessions, and it is what this change uses.

## References

- Issue #1740 (this record), #1702 (ADR-0515), #1558, #1687 (ADR-0502)
- ADR-0515 — the deadline-bounded lease this fences differently; §3 and §4 retired here, §1/§2/§5/§6
  in force
- ADR-0502 / migration 0084 — `object_write_leases`: the deadline it could decline, and the
  `LIVE_HOLDER_SQL` this reuses
- ADR-0495 — the `flock` probe, unchanged and still fail-open
- ADR-0518 — the schema-immutability guard, which is why 0087 is not edited
- ADR-0015 — migrations are forward-only; an applied file is never edited
- ADR-0014 — the log context this deliberately does not use as a carrier
