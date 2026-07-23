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
