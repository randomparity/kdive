# ADR 0501 — The staging-drain lane ages on the investigation, and states the provision race outright

- **Status:** Accepted
- **Date:** 2026-07-29
- **Amends:** [ADR-0494](0494-token-keyed-staging-drain.md) §5 — the lane's age gate moves from
  `systems.created_at` to `investigations.created_at`, and the protection that gate was proxying for
  becomes an explicit state predicate rather than an age heuristic. Decision 5 is otherwise
  unchanged: same worklist shape, same job, same empty `artifact_ids`, same
  `ROOTFS_STAGING_DRAIN_BACKOFF`.
- **Completes:** [ADR-0495](0495-reclaim-defers-a-live-held-checksum.md), whose Consequences name
  this as "the secondary residual ADR-0494 introduced … left in place and not fixed here", and say
  what fixing it requires: *"re-deriving that lane's age gate from something other than the `systems`
  row, which is a change to ADR-0494's decision 5 and its own disclosed steady-state cost."*
- **Spec:** [`../specs/2026-07-29-investigation-keyed-staging-drain-age-gate-1686-design.md`](../specs/2026-07-29-investigation-keyed-staging-drain-age-gate-1686-design.md)

## Context

`sweep_unowned_investigation_rootfs_staging` is the only retry for the **drained** half of the
uploaded-rootfs staging leak: a never-closed (`open`/`active`) investigation whose rootfs `artifacts`
rows have all gone, so neither the close-driven lane (keyed on a marker only `investigations.close`
sets) nor the TTL lane (a pure `artifacts` join) selects it. ADR-0494 decision 5 keyed its worklist
on `systems` for a sound reason — in that state there is no `artifacts` row left to key on, and the
`systems` row is the causal record for a staged base, retired in place rather than deleted.

It also took its age gate from the same row: `s.created_at < now() - retention`. That is the part
this ADR changes, and it is wrong for a reason ADR-0494 did not consider: **content-addressed reuse**
(ADR-0441). A base is staged at `<uploads>/<inv>/<token>.qcow2`, keyed on the content address, so a
System provisioned *minutes ago* legitimately attaches to a checksum this investigation staged
months ago. The bytes are old; the row that references them is new. Keying the gate on the `systems`
row therefore withholds the drained half's only retry until that *System* ages past
`investigation_rootfs_retention` — 30 days by default, against a lane whose intended cadence is
`ROOTFS_STAGING_DRAIN_BACKOFF` (6 hours). A ~120x degradation, and it is reachable end to end:

1. An uploaded-rootfs `artifacts` row can outlive any System (upload precedes provision), so the TTL
   lane drains the last row while the only `upload`-profile System is young.
2. ADR-0495's own disclosed residual then leaves a live-held `<token>.<uuid>.partial` behind: a
   fetcher that has resolved its row but not yet created its partial is invisible to the reclaim's
   `flock` probe.
3. The drain tail's `flock`-gated partial glob defers and **clears the marker**, per ADR-0452 §5.
4. Nothing re-triggers. The retained-row retry ADR-0495 built does not apply — this is the half where
   no row exists — and this lane, the only other trigger, requires a System past retention.

The gate was not arbitrary. ADR-0494's Consequences record what it bought: it "keeps the lane off a
System that is staging its base right now, between the `mkdir` and the row resolution", the window
its own *"Run the `rmdir` unconditionally"* rejection describes. That window is still live: #1558 is
**open**, and while ADR-0495 implemented its option 1, ADR-0495 explicitly does not close it and
discloses the "row resolved, partial not yet created" gap that keeps the doomed-fetcher path
reachable. So the gate cannot simply be deleted — but it also never did the job well. Under a
past-retention sibling System, `s.created_at` admits the job while another System of the same
investigation is mid-`mkdir`; the drain tail then sweeps the one staging directory they share. That
is not a hypothetical reading of the SQL: it is a test that fails on `main`.

## Decision

1. **The age gate reads `investigations.created_at`.** `_UNOWNED_STAGING_INV_SQL`'s predicate becomes
   `i.created_at < now() - %s`, against the same `investigation_rootfs_retention` the lane already
   received.

   The investigation is the right key on all three counts the `systems` row was chosen for. It is
   **causal**: staging is per-investigation (`<uploads>/<inv>/`), and every base the directory ever
   held was staged for this investigation, so `investigations.created_at` is necessarily no younger
   than the oldest thing there. It is **durable**: rows are never deleted, and `open`/`active` is
   already in the predicate. And it applies **the same policy to the same bytes** as the
   artifacts-keyed TTL lane — which is exactly the equivalence ADR-0494 claimed for the gate it had,
   and which content-addressed reuse breaks for `systems` but not for the investigation.

   Rejected alternatives are in the last section; the two the issue and ADR-0494 raise — the staged
   row's own timestamp, and durable per-investigation drain state — are both unavailable in the
   drained half by construction.

2. **The `mkdir` ↔ row-resolution protection becomes an explicit anti-join, not an age proxy.** A
   second `NOT EXISTS` excludes an investigation any of whose Systems is in
   `_MID_MATERIALIZE_STATE_VALUES`. Two properties are load-bearing:

   **It reads ADR-0441 §6's curated `ROOTFS_BASE_PRE_OVERLAY_SYSTEM_STATES` rather than restating a
   list.** That set — `provisioning`, `reprovisioning`, `restoring` — is already this codebase's
   answer to "a System legitimately needs its base with no overlay file yet", it is what the
   reclaim's own pin gate reads, and it is guarded by `test_reclaim_classification_is_exhaustive`, so
   a new non-terminal `SystemState` added without being classified reddens CI. A hand-written
   two-element `{provisioning, reprovisioning}` here — which #1686 suggests — would inherit none of
   that and could drift from the pin gate on what "mid-materialize" means. Including `restoring` is
   also strictly the safer direction.

   **It is investigation-scoped, not per-`systems`-row.** The job carries an empty `artifact_ids` and
   the drain tail sweeps the *one* staging directory every System of the investigation shares, so a
   per-row exclusion would let a settled sibling re-admit the job and `rmdir` under the provisioning
   one. This is strictly stronger than what `s.created_at` gave, which admitted exactly that.

3. **Time is still compared against Postgres `now()`, in SQL.** Unchanged, and restated because it
   is the property most easily lost when a predicate is re-derived: no Python-side clock enters the
   gate.

4. **No schema, no migration.** `investigations.created_at timestamptz NOT NULL DEFAULT now()` has
   existed since `0001_init.sql`. The sweep's signature, the job kind, the payload, the dedup key,
   the `repair_kind`, and the reconciler catalog registration are all untouched.

## Consequences

- A reused long-staged checksum is retried on `ROOTFS_STAGING_DRAIN_BACKOFF`'s ~6-hour cadence
  regardless of how young the referencing System is — #1686's acceptance criterion. The 30-day
  strand is gone, and with it the last "no trigger at all" hole ADR-0494 and ADR-0495 left in the
  uploaded-rootfs reclaim.
- **The lane's permanent steady-state cost is re-stated, not merely inherited, because this ADR
  widens the worklist in one direction and narrows it in another.** ADR-0494's analysis stands
  unchanged in *shape*: the worklist is a steady state rather than a condition that clears, since
  `systems` rows are retired in place and never leave the match, so a never-closed investigation that
  ever staged an uploaded base is selected for the rest of its life whether or not its staging
  directory holds anything — which the reconciler cannot see, holding no filesystem (ADR-0442). Each
  selected pass still costs a `jobs` delete-and-insert, a worker dequeue/lease/complete cycle, an
  `INVESTIGATION` advisory-lock transaction, one `pinned_rootfs_tokens` enumeration with an `os.stat`
  per referencing System, and one `readdir`, bounded to four passes a day by the lane's own 6-hour
  backoff.

  What changes is **when an investigation joins that steady state, not how many eventually do**.
  Because a System is always created after its investigation, `i.created_at <= s.created_at`, so the
  new predicate is strictly weaker and every affected investigation is admitted *earlier* — by the
  gap between the investigation's creation and its first `upload`-profile System's, which for the
  reuse case this ADR exists for is the whole point and is bounded by the retention window itself. In
  the limit the population is identical: every `open`/`active` investigation that ever staged an
  uploaded base and has drained its rows is selected under either gate. So the permanent rate is
  unchanged and the transient is an earlier start, against a lane that was already the cheapest of
  the three by design. The profile predicate still bounds the worklist away from the whole `systems`
  table.

  Pulling the other way, decision 2 **removes** rows the old gate admitted: an investigation with a
  mid-materialize System is now excluded outright, where `s.created_at` admitted it whenever any
  sibling was past retention. That is a correctness gain that also lowers the count.
- **Decision 2 buys the protection at the price of a new exclusion, and that exclusion is bounded by
  a chain worth naming rather than assuming.** A System wedged in `provisioning`/`reprovisioning`/
  `restoring` blocks its whole investigation's drain for as long as it sits there, and there is **no**
  `repair_stalled_provisioning_systems` — `repairs/systems.py` has one for `crashing`, `restoring`
  and `creating` snapshots, not for `provisioning`. What retires it is the allocation path:
  `sweep_expired_allocations` expires the lease (`lease_expiry`, 4h on this deployment), then
  `repair_orphaned_systems` enqueues a teardown for any non-terminal System whose allocation is
  terminal, which lands it in `torn_down` and out of the anti-join. So the exclusion is bounded on
  the order of the lease window, not the 30-day retention this ADR removes — strictly better than
  what it replaces in both arms. It is **not** bounded for an allocation with a NULL `lease_expiry`,
  which `sweep_expired_allocations`' own predicate skips; that is pre-existing and unchanged, and it
  is the one shape in which decision 2 can strand a drain indefinitely.
- **The anti-join is a state test, so it inherits state's blind spot.** A System that was
  mid-provision and has since gone `torn_down` or `failed` no longer excludes anything, even if its
  detached, uncancellable download is still writing — the same limitation ADR-0495 records for the
  pin gate, whose fix is #1558's option 2 (the classifier). The `flock` probe in
  `_reclaim_one_checksum` and the drain tail's own `flock`-gated partial glob remain the defence
  there; nothing about them changes here. What this ADR guarantees is narrower and worth stating
  plainly: the lane no longer *issues* a drain job against an investigation whose System the state
  column says is mid-materialize. It does not claim to know that no download is in flight.
- **The lane still has no supporting index, and this ADR does not add one.** `investigations` is
  joined by primary key and `i.created_at` is a filter on the joined row, so the plan shape is
  unchanged from ADR-0494's: a sequential scan of `systems` with a per-row jsonb extraction, plus the
  `artifacts` anti-join. The new `systems` anti-join is a second scan of a table already being
  scanned, which the planner hashes at this cardinality. Named because ADR-0494 named it.
- **`investigations.created_at` becomes load-bearing for a retention decision for the first time.**
  It was previously record metadata. Nothing writes it but the `DEFAULT now()`, and nothing back-dates
  it, so this is a read of a column that was already durable and already true — but a future feature
  that clones or re-dates investigations would now move bytes' retention with it.
- No migration, no config setting, no new dependency, no MCP tool, no RBAC surface change, no new
  `repair_kind`. Not an AI surface. The reconciler catalog and `loop.py` are untouched — the sweep's
  signature is unchanged.
- Test coverage is where the old gate had none: on `main` the age gate was unasserted in **both**
  directions for the reuse case, so this change had to arrive with new tests rather than by flipping
  old ones. Two failed before it (the young-System retry, and the whole-investigation exclusion under
  a past-retention sibling) and the anti-join's four assertions were verified to redden when it is
  removed.

## Considered & rejected

- **The staged base's own timestamp — the `artifacts` row's `created_at`, mirroring `gc.py:39`.**
  Rejected as unavailable by construction, which is the whole reason this lane exists: it fires only
  when `NOT EXISTS` any rootfs `artifacts` row, so in its worklist there is no row to carry a
  timestamp. #1686 proposes this first and it is the natural mirror of the TTL lane; it answers a
  question about a different lane.
- **The partial's or the staging directory's mtime.** Rejected. The reconciler holds no filesystem —
  the invariant ADR-0442 exists to enforce, because on a host-process local-libvirt deployment the
  worker runs as root and the reconciler as the invoking user (#1522). A filesystem read here would
  be the one thing decision 5 was built to avoid, and the reason the job carries an empty worklist.
- **Durable per-investigation drain state (a "staging last drained at" column).** Rejected, and
  already rejected on record by ADR-0494's Consequences as "a schema change to save a `readdir`".
  Nothing but this sweep would read it.
- **Delete the age gate outright and rely on the anti-join alone.** Rejected. The gate is not only a
  race proxy; it is also the retention *policy* that governs these bytes, the equivalence ADR-0494
  claimed with the artifacts-keyed TTL lane. Dropping it would enqueue a drain job for every
  never-closed uploaded-rootfs investigation from its first pass onward, which is ADR-0494's
  steady-state cost with its one bound removed.
- **Restate the excluded states as `{provisioning, reprovisioning}`, per #1686's suggestion.**
  Rejected in favour of reading `ROOTFS_BASE_PRE_OVERLAY_SYSTEM_STATES`, per decision 2: a
  hand-written list inherits neither the exhaustiveness guard nor the pin gate's definition, and
  omitting `restoring` is the less safe direction for no benefit.
- **Exclude a mid-materialize System only while its lifecycle job is still `queued`/`running`,
  mirroring `repair_stalled_restoring_systems`' predicate.** Not rejected on merit — it is a real
  refinement, and it would bound the new exclusion by `repair_abandoned_jobs`' cadence instead of the
  allocation lease. Rejected as scope: it adds a third `NOT EXISTS` over `jobs` and a
  `SYSTEM_FAILING_JOB_KINDS` dependency to shorten a window that the lease chain already bounds well
  inside the interval this ADR is removing. Worth revisiting if a stalled-`provisioning` System is
  ever observed to hold a drain.
- **Set `rootfs_cleanup_pending_at` on an open investigation to reuse the close-driven lane.**
  Rejected for the fourth time, on ADR-0452's, ADR-0494's and ADR-0495's recorded reasoning: the
  column is durable, record-model-visible state (`domain/lifecycle/records.py`) meaning "this
  investigation was closed and its rootfs is being reclaimed", and an open investigation carrying it
  reads as closed to every consumer.
