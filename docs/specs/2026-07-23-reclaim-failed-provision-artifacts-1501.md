# Reclaim host + object-store artifacts on a provision that fails after materialization (#1501)

- **Issue:** [#1501](https://github.com/randomparity/kdive/issues/1501)
- **ADR:** [ADR-0435](../adr/0435-reclaim-failed-provision-artifacts.md)
- **Date:** 2026-07-23

## Problem

A local-libvirt provision that fails **after** it has materialized host artifacts leaks them.
Two independent orphan planes exist; a single-site fix is insufficient.

### Plane 1 — host files (provider)

`LocalLibvirtProvisioning.provision` (`providers/local_libvirt/lifecycle/provisioning.py`)
materializes the rootfs base, extracts the baseline kernel directory, and creates the overlay
**before** its `try` block. The only failure cleanup is `cleanup_overlay_if_created`, and it
covers just the overlay for failures raised *inside* the `try`. A `_prepare_baseline_kernel`
raise (the reported trigger: `rootfs /boot has multiple kernels`) happens outside the `try`
entirely, so it reclaims nothing. Even an in-`try` failure leaves the baseline directory and — since
#743 (ADR-0434) — the multi-GB staged uploaded rootfs behind. A `failed` System cannot be torn
down (`failed -> torn_down` is not a legal transition), so `teardown` — the only place that
reclaims these files — never runs for it.

### Plane 2 — S3 object + upload manifest (reconciler)

`_commit_uploaded_rootfs` runs only on the `provisioning -> ready` path, so a failed provision
never registers the `artifacts` row that teardown reclaims. The reconciler upload reaper
(`reconciler/cleanup/uploads.py`) gates the `systems` owner to `state = 'defined'`, so a terminal
`failed` upload System past its manifest deadline is skipped — stranding the SENSITIVE S3 object
and its `upload_manifests` row indefinitely (no `owner_kind='systems'` expiry reaper collects
them).

## Acceptance

A local-libvirt provision that fails after materializing the rootfs base leaves no orphaned
staged-rootfs / baseline / overlay host files (reclaimed on the failure path), and its uncommitted
S3 object + manifest are reclaimed by the reconciler.

## Design

### Plane 1 — widen `provision()`'s transactional reclaim, gated on pre-existence

Move the materialize / baseline / overlay / render steps **inside** the `try` whose `except
CategorizedError` reclaims. The reclaim removes only the artifacts **this call created**, decided by
a pre-existence snapshot taken *before* the mutating block:

- `overlay_pre`, `baseline_pre` via the existing `overlay_exists` / `baseline_exists` seams;
- `staged_pre` via a new `uploaded_rootfs_exists` seam, and only for the `upload` rootfs kind.

On failure, for each artifact whose pre-existence snapshot was `False`, a **best-effort** removal
runs (`remove_overlay_for_domain` / `remove_baseline_for_domain` /
`remove_uploaded_rootfs_for_domain`), swallowing a secondary `CategorizedError` so it never masks
the original provisioning error. `_resolve_guest_arch` stays *before* the snapshot/`try`, preserving
its "zero artifact on arch drift" contract.

**Why pre-existence, not blanket removal.** A pre-existing overlay / staged base / baseline may back
a live or recoverable prior attempt; removing it would corrupt that attempt's backing chain. This
mirrors the established `test_provision_failure_keeps_preexisting_overlay` contract, now extended to
all three artifacts. Removal of a not-yet-created artifact after an early failure (e.g. materialize
raised) is a harmless idempotent no-op (`missing_ok` / `FileNotFoundError`).

`cleanup_overlay_if_created` is subsumed by the unified reclaim and removed as dead code.

### Plane 2 — relax the `systems` reaper gate to `{defined, failed}`

Extend the reaper's `systems` predicate from `state = 'defined'` to `state IN ('defined',
'failed')`, in both the candidate select and the per-owner locked re-read. `provisioning` stays
excluded — an in-flight provision may still be reading the staged object. The existing per-key skip
(`SELECT 1 FROM artifacts WHERE object_key = %s`) already exempts any committed object, so a
`failed` System that somehow holds a committed row (it cannot — commit deletes the manifest) is safe
regardless. `owner_pre_finalize` is renamed `owner_reapable` and its state maps become tuples used
with `state = ANY(%s)`.

## Non-goals

- Re-scoping the uploaded rootfs to the investigation lifetime (#1502, deferred to design).
- Reclaiming the host staged file in the narrow worker-death-then-reused-then-failed redelivery
  case: honoring the pre-existence contract means a reused (pre-existing) staged file is preserved.
  The single-attempt case — the reported bug — is fully reclaimed; the S3 backstop covers the
  object regardless.

## Test plan

- Provider: a `_prepare_baseline_kernel` raise after materializing an `upload` rootfs reclaims the
  staged rootfs + baseline this call created; a pre-existing staged/baseline is preserved; a
  reclaim-time `CategorizedError` does not mask the original error; the existing overlay-reclaim
  tests still hold.
- Reconciler: a terminal `failed` upload System past its manifest deadline with no committed row
  has its object + manifest reaped; a committed object stays exempt; `ready`/`provisioning` Systems
  are still skipped.
