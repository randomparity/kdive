# External-boot reconciler detection lanes

Issue: #2203
Decision: [ADR-0596](../../adr/0596-allocation-release-waits-for-external-boot-cleanup.md)
Existing decisions: [ADR-0583](../../adr/0583-external-run-boot-uses-prepared-recovery-points.md),
[ADR-0593](../../adr/0593-external-boot-operations-ride-marked-boot-and-teardown-jobs.md), and
[ADR-0595](../../adr/0595-external-boot-reentry-uses-provider-receipts.md).

## Outcome

Repeated reconciler passes detect post-preparation external-boot work abandoned by a worker and
enqueue the existing authority-marked `boot` or `teardown` operation that resumes it. The
reconciler retains SELECT-only access to external-boot tables; only the worker authority commit
changes activation, attempt, reservation, release, cleanup, or System state.

## Scope decisions

The operator selected the bounded interpretation after inspecting the merged prerequisites:

- `external_boot_reservations.activation_id` has an `ON DELETE CASCADE` foreign key, so a durable
  reservation without an activation row is structurally impossible. The schema constraint and a
  regression proof discharge that proposed lane; no unreachable repair is added.
- Initial `preparing` state needs the full immutable `ExternalBootPlan`, while the activation row
  stores only its identity. Atomic initial admission and retry therefore remain with #2204, whose
  server caller owns the plan. This issue starts at `prepared`.
- #2204 MCP contract promotion, provider adapters, the #2201 claim migration, and ppc64le-only live
  verification stay excluded.

## Candidate model

Four catalog lanes select durable activation states and enqueue one next operation:

| Lane | Durable candidate | Enqueued operation |
|---|---|---|
| activation | `prepared`, or `activating` past its persisted activation deadline | `activate` |
| recovery | `recovering` past the current attempt's persisted recovery deadline | `recover` or `resolve-conflict`, preserving the attempt's durable basis |
| release | terminal activation with cleanup incomplete and a ready reservation but no release row | `release` |
| cleanup | terminal activation with cleanup incomplete and a release row | `cleanup` for `recovered`/`abandoned`; `teardown` for `recovery_conflict`/`recovery_failed` once the System is failed |

An activation in a stable `active` state is not abandoned work. A recovery still inside its
deadline is not abandoned work. A queued job, or a running job whose lease is live or whose attempt
budget allows the ordinary claim path to reclaim it, suppresses another enqueue.

The source tuple comes from the activation's newest validated authority-marked job: provider kind,
source authority instance, authorizing principal/project, and the prior job identifier. The lane
passes
that tuple through `build_external_boot_payload`, so provider binding and activation ownership are
checked by the same helper used by live admission. Missing or malformed source evidence is a
candidate-local failure: it is logged without raw payload data, left unchanged, and retried next
pass without starving another candidate.

The repair operation identity and deduplication key are deterministic from activation, desired
operation, and source job. A second pass sees the queued/running repair and reports zero. If that
repair itself consumes its attempt budget and loses its lease, it becomes the next source job; the
next deterministic link can be enqueued without mutating the stranded row. Authority allocation
supersedes the old authority while its commit remains the only writer of lifecycle state.

## Database authority and concurrency

Candidate reads and `queue.enqueue` are the reconciler's only database actions. No code in the
repair module issues `INSERT`, `UPDATE`, or `DELETE` against the four external-boot tables, calls an
activation repository mutator, or imports a provider adapter. Integration coverage executes the
lanes as `kdive_reconciler`, for which migration 0121 grants SELECT alone.

Each lane reads at most 100 activation candidates per pass and considers at most the newest 100
authority-marked source jobs per activation. Stable identifier ordering makes the remainder
eligible on later passes while bounding database work driven by durable tenant history.

Concurrent passes may identify the same candidate, but the deterministic job deduplication key
makes enqueue idempotent. The worker rechecks activation, Allocation, System, Run, authority, and
deadline facts under the existing authority protocol before any provider mutation or commit.

## Reporting and configuration

Each lane is a separate `_REPAIR_CATALOG` entry and has a scalar `ReconcileReport` count ending in
`_enqueued`. The existing `repair_counts` mirror and `ALL_REPAIR_KINDS` derive from the catalog, so
telemetry remains bounded and total. `ReconcileConfig` gains only the provider resolver needed by
`build_external_boot_payload`; the authority instance is read from the already-required process
setting when production composition builds the configuration. The successor marker always uses
that current configured authority instance so the live authority process can acknowledge it. A
differing source authority instance is accepted as restart provenance only and is not copied into
the successor marker. The remaining source identity must validate; otherwise the candidate fails
locally and no job is enqueued.

The catalog runner continues to isolate a raising lane on its own pooled connection. Candidate
errors within a lane are isolated individually.

## Allocation release

ADR-0596 adds `ALLOCATION_RELEASE` to the matrix. Both release implementations share one locked
external-boot precondition: the project release path and the reconciler's orphaned-active
`reclaim_under_lock` path. The release service keeps the existing terminal
fast paths, then under `PROJECT -> ALLOCATION` discovers every System for a releasable Allocation,
takes their System locks in stable UUID order, and checks the matrix for each. Any restricting
activation returns `conflict` without a transition, accounting credit, or audit transition. This
preserves the active Allocation required by every later authority allocation.

## Verification

Tests cover:

1. each lane's candidate and suppression predicates, operation choice, deterministic deduplication,
   and zero-count second pass;
2. expired activation and recovery deadlines using the persisted timestamps rather than a new
   calculation;
3. staged release then cleanup/teardown convergence through real worker handlers;
4. a lapsed, exhausted worker job yielding one successor rather than remaining the only path;
5. one raising lane appearing in `ReconcileReport.failures` while sibling lanes still run;
6. a `kdive_reconciler` database session successfully enqueueing while direct external-boot writes
   remain denied;
7. import-closure exclusion of local-libvirt, remote-libvirt, and `libvirt`;
8. the reservation foreign key preventing a row without an activation;
9. allocation release denied under the System lock for every restricting activation, then admitted
   after cleanup;
10. the orphaned-active reaper likewise retaining an Allocation whose terminal historical System
    has incomplete external-boot cleanup;
11. a source from authority instance A producing one successor for current instance B, which B
    acknowledges and converges; and
12. unchanged catalog cardinality shape plus repository lint, type, record, and test guardrails.

## Security model

No new external entry point is added. The relevant trust boundary is the durable jobs table: values
originally admitted by an authenticated server caller are read by the reconciler and copied into a
new job. The repair validates the closed payload model, rebuilds through the provider-binding helper,
and never logs payload or authorizing values. It cannot invent a project, principal, provider, or
authority instance.

An authenticated tenant can influence the original job only through existing MCP validation and
authorization. The reconciler does not act on arbitrary unmarked jobs, cannot widen roles, and
cannot write lifecycle truth. It uses only the configured current authority instance, never a
payload-controlled replacement. Stable deduplication bounds repeated passes to one live repair job
per candidate/source pair. Provider mutation remains behind worker incarnation credentials and the
provider-authority acknowledgement and journal fences.

Private host identifiers, credentials, provider diagnostics, and raw payloads are excluded from
logs and reports. Provider-specific behavior and the promoted MCP request surface remain outside
this design.
