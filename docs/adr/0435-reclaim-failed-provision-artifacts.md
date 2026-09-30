# ADR 0435 — Reclaim host + object-store artifacts on a provision that fails after materialization

- **Status:** Accepted
- **Date:** 2026-07-23
- **Depends on:** [ADR-0434](0434-local-libvirt-agent-uploaded-rootfs-staging.md) (the staged
  uploaded rootfs whose failure-path leak this closes, and the teardown reclaim this mirrors),
  [ADR-0272](0272-provision-baseline-kernel-boot.md) (the per-System baseline directory),
  [ADR-0060](0060-per-system-rootfs-overlay.md) (the per-System overlay and its
  create-only-when-absent contract), [ADR-0048](0048-external-build-artifact-ingestion.md)
  (`_commit_uploaded_rootfs`, the upload window, and the manifest reaper this relaxes),
  [ADR-0104](0104-chunked-external-upload-reassembly.md) (the reconciler upload reaper).
- **Spec:** [`../specs/2026-07-23-reclaim-failed-provision-artifacts-1501.md`](../specs/2026-07-23-reclaim-failed-provision-artifacts-1501.md)

## Context

A local-libvirt provision that fails **after** it has materialized host artifacts leaks them
(#1501). The trigger observed live: a `_prepare_baseline_kernel` raise (`rootfs /boot has multiple
kernels`) left a 1.3 GiB `rootfs-uploads/local-systems-<id>-rootfs.qcow2` orphan that neither
`systems.teardown` nor the reconciler removed.

There are two independent orphan planes, so a single-site fix is insufficient:

1. **Host files.** `LocalLibvirtProvisioning.provision` materializes the rootfs base
   (`_materialize_rootfs`), extracts the baseline-kernel directory (`_prepare_baseline_kernel`),
   and creates the overlay (`prepare_overlay`) **before** its `try` block. The only failure cleanup,
   `cleanup_overlay_if_created`, covers just the overlay and only for failures raised *inside* the
   `try`. A `_prepare_baseline_kernel` raise happens outside the `try` and reclaims nothing; even an
   in-`try` failure leaves the baseline directory and the multi-GB staged uploaded rootfs (ADR-0434)
   behind. These are reclaimed only by `teardown`, which a `failed` System can never run
   (`failed -> torn_down` is not a legal transition).

2. **S3 object + upload manifest.** `_commit_uploaded_rootfs` runs only on the
   `provisioning -> ready` path, so a failed provision never registers the `artifacts` row teardown
   would reclaim. The reconciler upload reaper (`reconciler/cleanup/uploads.py`) gates the `systems`
   owner to `state = 'defined'`, so a terminal `failed` upload System past its manifest deadline is
   skipped — stranding the SENSITIVE S3 object + `upload_manifests` row indefinitely (no
   `owner_kind='systems'` expiry reaper collects them).

The baseline-directory leak predates #743; #743/ADR-0434 added the larger staged-rootfs payload,
which is why it surfaced now.

## Decision

### 1. Widen `provision()`'s transactional reclaim, gated on pre-existence

The materialize / baseline / overlay / render steps move **inside** the `try` whose
`except CategorizedError` reclaims. The reclaim removes only the artifacts **this call created**,
decided by a pre-existence snapshot taken *before* the mutating block: `overlay_pre` /
`baseline_pre` (the existing `overlay_exists` / `baseline_exists` seams) and, for the `upload`
rootfs kind only, `staged_pre` (a new `uploaded_rootfs_exists` seam). On failure, each artifact
whose snapshot was `False` is removed **best-effort** through the same
`remove_{overlay,baseline,uploaded_rootfs}_for_domain` helpers `teardown` uses, swallowing a
secondary `CategorizedError` so it never masks the original provisioning error.

`_resolve_guest_arch` stays *before* the snapshot and `try`, preserving its ADR-0340 "zero
overlay/baseline/staged on an arch drift" contract. `cleanup_overlay_if_created` is subsumed by
the unified reclaim and removed as dead code.

**Why pre-existence, not blanket removal.** A pre-existing overlay, staged base, or baseline may
back a live or recoverable prior attempt; removing it would corrupt that attempt's backing chain.
This extends the established `test_provision_failure_keeps_preexisting_overlay` contract from the
overlay to all three artifacts. Removing a not-yet-created artifact after an early failure (e.g.
materialize raised before the overlay existed) is a harmless idempotent no-op (`missing_ok` /
`FileNotFoundError`).

### 2. Relax the reconciler `systems` reaper gate to `{defined, failed}`

The reaper's `systems` predicate widens from `state = 'defined'` to `state IN ('defined',
'failed')`, in both the candidate select and the per-owner locked re-read, so a terminal `failed`
upload System past its manifest deadline is reaped. `provisioning` stays **excluded** — an in-flight
provision may still be reading the staged object. The per-key skip
(`SELECT 1 FROM artifacts WHERE object_key = %s`) already exempts any committed object, so nothing a
`ready` (or ex-`ready`) System committed can be deleted; a `failed` System never committed (commit
runs only at `ready` and deletes the manifest), so its object is uncommitted by construction.
`owner_pre_finalize` is renamed `owner_reapable` and its per-owner-kind state maps become tuples
consumed with `state = ANY(%s)`.

This reconciler backstop is preferred over a failure-path S3 delete because it is not gated on a
teardown the failed System cannot perform, and it remains useful regardless of the eventual #1502
lifetime redesign.

## Consequences

- A local-libvirt provision that fails after materialization leaves no orphaned staged-rootfs,
  baseline-directory, or overlay host file, and its uncommitted S3 object + manifest are reclaimed
  by the reconciler. The acceptance criterion of #1501 holds on both planes.
- No schema change, no migration, no MCP-surface change. Plane 1 is a widened `try`, a pre-existence
  snapshot, an existence seam, and a best-effort reclaim; plane 2 is a two-value gate relaxation.
- **Residual — the redelivery-reuse case.** A worker death mid-provision (not a `CategorizedError`)
  that staged the rootfs, followed by a redelivered attempt that reuses it and then fails
  deterministically, preserves the *pre-existing* staged host file (the pre-existence contract wins
  over reclaim). The reported single-attempt bug is fully reclaimed, and the S3 backstop reclaims
  the object regardless; only the host file lingers in this narrow case. Reclaiming a file that may
  back a prior overlay is more dangerous than leaking it, so the contract is kept.
- **Residual — the reclaim is best-effort.** A reclaim `OSError` during a failed provision is logged
  and swallowed to preserve the original error; the reconciler and a later teardown (if the System
  is ever reprovisioned) remain the backstops.
- **#1502 unaffected.** Re-scoping the uploaded rootfs to the investigation lifetime is deferred to
  a design session; this backstop is orthogonal to it.

### Amendment (2026-09-29): teardown of a failed System reclaims without a transition (#2908)

The Context above says the host artifacts a failed provision leaves behind "are reclaimed only by
`teardown`, which a `failed` System can never run". The transition half stays true: `failed` has
no outbound edge, and `failed -> torn_down` remains illegal. The job half no longer holds. A
teardown job for a `failed` System (queued, for example, behind a provision that then failed) now
skips the `tearing_down` move, runs the same idempotent provider snapshot and domain teardown and
core reclaim a `torn_down` re-run does, leaves the System `failed`, and succeeds. Before this, it
raised `IllegalTransition` and burned every attempt as `infrastructure_failure`.
Mutation-obligation discharge for a `failed` System stays with ADR-0652's reconciler lane, and
this does not make the reconciler enqueue teardown for `failed` Systems.
[ADR-0441](0441-investigation-scoped-uploaded-rootfs.md) repeats the same premise; its
overlay-absence gate does not depend on it and is unchanged. Rejected: a no-op teardown for
`failed`, which would leave provider leftovers unreclaimed, and a `failed -> tearing_down` edge,
which would widen a terminal state. Design:
[`2026-09-29-failed-system-teardown-design.md`](../workflow/specs/2026-09-29-failed-system-teardown-design.md).

### Amendment (2026-09-29): `systems.teardown` re-runs a dead-lettered teardown (#2929)

The #2908 design accepted that a `failed` System's teardown job, once it dead-letters, is not
re-run ([`2026-09-29-failed-system-teardown-design.md`](../workflow/specs/2026-09-29-failed-system-teardown-design.md),
Failure model). By operator decision the public `systems.teardown` now re-runs it. On the ordinary
path (no external-boot activation, no authority binding, System not `torn_down`), a `failed`
`{uid}:teardown` row, whatever its category, is reset to a fresh queued attempt under the System
lock and behind the external-boot admission matrix (`JobRecyclePolicy.FAILED`). A `queued`,
`running`, `succeeded`, or `canceled` row still replays. This covers teardowns that dead-lettered as
`infrastructure_failure` before #2913. `ops.force_teardown` keeps replaying the dead row. No
reconciler lane selects `failed` Systems, so
[ADR-0441](0441-investigation-scoped-uploaded-rootfs.md)'s orphan-lane exclusion and overlay-absence
gate are unchanged. Design:
[`2026-09-29-failed-teardown-rerun-design.md`](../workflow/specs/2026-09-29-failed-teardown-rerun-design.md).

### Amendment (2026-09-29): teardown refuses a `reprovisioning` System (#2928)

`reprovisioning` is the only non-terminal System state with no teardown edge, so a teardown job
that met one raised `IllegalTransition`, which the worker classified as retryable
`infrastructure_failure` and re-ran until attempts ran out. By operator decision the teardown path
refuses instead of waiting. On the ordinary path (no external-boot activation history, which
routes to authority teardown first), `systems.teardown` returns a `conflict` under the System lock,
carrying `current_status: reprovisioning`, and enqueues nothing. It refuses ahead of the dedup
replay and the enqueue, so a live `{uid}:teardown` row is not replayed and a failed one is not
recycled while the reprovision runs. A teardown job that still
meets a `reprovisioning` System, for example one queued before the reprovision began, fails once
with a terminal `conflict`. Once the reprovision settles, the operator re-runs `systems.teardown`,
which recycles that failed row under the #2929 amendment above. `repair_orphaned_systems` skips
`reprovisioning` Systems, both in its candidate query and in its System-locked recheck, and
enqueues on a later pass once the System is `ready`. The lane replays an existing
`{uid}:teardown` row rather than recycling it, so a row that already failed this way needs the
operator's `systems.teardown` as well. This matches
`investigations.close(force=True)`, which already refuses while a bound System is mid-reprovision.
Rejected: deferring the job without charging an attempt, which needs a queue primitive the worker
does not have.

### Amendment (2026-09-29): reprovision refuses under a live teardown job (#2979)

An ordinary teardown enqueue leaves the System `ready`, so the #2928 amendment's case of a teardown
queued before the reprovision began was reachable. `systems.reprovision` now reads the
`{uid}:teardown` row under the System lock and refuses a `ready` System with a `conflict`
(`reason: teardown_in_progress`) while that row is `queued` or `running`, writing nothing. A settled
row does not block. The teardown handler's terminal `conflict` on a `reprovisioning` System stays as
a backstop.

### Amendment (2026-09-30): recycle policy of every ordinary teardown enqueuer (#2978)

`enqueue_control_teardown` now takes a required `recycle` policy, so no path that enqueues an
ordinary `{uid}:teardown` row inherits one silently. On its ordinary branch a prior row carrying
`authority_system_v1` or `external_boot_authority_v1` always replays, because a recycle rewrites
the payload and the authority re-run path ([ADR-0620](0620-authority-owned-system-teardown.md),
#2917) keys on that marker. The preactivation-authority branch is unchanged. By operator decision:

- `systems.teardown` (`mcp/tools/lifecycle/systems/admin.py`): `FAILED`, per the #2929 amendment.
- `ops.force_teardown` (`mcp/tools/ops/security/breakglass.py`): `FAILED`, so the break-glass path
  re-runs a dead-lettered unmarked row. This replaces the #2929 amendment's "`ops.force_teardown`
  keeps replaying the dead row". It also refuses a `reprovisioning` System with a `conflict`
  (`current_status: reprovisioning`) under the System lock and writes no job, as
  `systems.teardown` does under the #2928 amendment.
- `enqueue_teardown` (`jobs/service_operations.py`, investigation force-close): `NEVER`; a
  dead-lettered row replays.
- `repair_orphaned_systems`: `NEVER`, and it never recycles a failed row. A new read-only lane,
  `stranded_orphan_teardowns`, logs one WARNING per orphaned System per failure (keyed on the
  failed row's `updated_at`, in process memory) naming the remedy: `systems.teardown`, or
  `systems.get` for an authority-marked row. Its count feeds
  `kdive.reconciler.repairs{repair_kind="stranded_orphan_teardowns"}`. `tearing_down` Systems are
  excluded from it.
- `repair_stalled_tearing_down_systems`: `TERMINAL` (unchanged), for a `tearing_down` System.

No reconciler lane re-runs a teardown for a `failed` System; ADR-0441's exclusion stands.
Rejected: a bounded recycle in the orphan lane, which would re-run a teardown the handler already
refused with no new evidence; persisting the warning dedupe, which costs a migration for a
log-noise bound the counter already covers. Design:
[`2026-09-30-teardown-recycle-policy-2978-design.md`](../workflow/specs/2026-09-30-teardown-recycle-policy-2978-design.md).

### Amendment (2026-09-30): the reconciler settles a stalled reprovision (#2980)

`reprovisioning` leaves only through the reprovision handler, so a reprovision job that
dead-lettered, was canceled, or is absent stranded the System: the #2928 amendment refuses its
teardown and the orphan lane skips it. The reconciler lane `stalled_reprovisioning_systems`, run
after `abandoned_jobs`, now moves such a System to `failed` under the System lock. It writes a
`reprovisioning->failed` audit row (tool `systems.reprovision`, reconciler principal). A
half-rebuilt disk is indeterminate, so the lane never returns the System to `ready`. Once the
System is `failed`, `systems.teardown` reclaims it under the #2908 and #2929 amendments. The lane
does not tear the System down itself (ADR-0441) and does not recycle the reprovision job.

Reprovision jobs match on `kind` and `payload->>'system_id'`, because the dedup key is per
profile. A `queued` or `running` job blocks at any age. A non-capture handler is not cancelled when
its heartbeat stops (`jobs/worker.py` `_dispatch`, `_heartbeat_loop`), so three terminal rows can
hide a running handler and block for 15 minutes after their last write, the `_TEARDOWN_SETTLE`
bound of ADR-0634:

- `canceled`, since `jobs.cancel` leaves the handler running;
- `failed` with `lease_expired`, which `repair_abandoned_jobs` writes over a lapsed attempt;
- `failed` at `attempt > 1`, because `claim_worker_job` reclaims a lapsed `running` row as the next
  attempt, which can fail while the earlier handler still runs.

A `failed` row at `attempt = 1` without `lease_expired` was written after the only handler returned,
so it settles at once. A handler that outlives the window finds the System `failed`: its commit
applies only from `reprovisioning`, and it reaps its own domain.

The lane records the new non-retryable `reprovision_incomplete` category (migration 0166) under
ADR-0513 §1a precedence: only when the newest terminal reprovision job has no category or
`lease_expired`. Otherwise the column stays NULL and the ADR-0454 job fallback answers.

Known consequence: `systems.reprovision` with a profile the System already applied replays that
profile's terminal job (`recycle=NEVER`), so no handler runs. Such a System now settles to
`failed` instead of staying `reprovisioning`. The admission fix is not part of this amendment.
Spec: [`../workflow/specs/2026-09-29-stalled-reprovision-lane-2980-design.md`](../workflow/specs/2026-09-29-stalled-reprovision-lane-2980-design.md).
