# ADR 0092 — Image & rootfs lifecycle: managed subsystem, build-plane port, DB catalog (M2.4)

- **Status:** Proposed
- **Date:** 2026-06-10
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0052](0052-bootable-rootfs-image-builder.md)
  (the unprivileged libguestfs rootfs build the local plane now orchestrates in-process),
  [ADR-0080](0080-remote-provisioning-disk-image-profile.md) (the remote provisioning
  disk-image whose placeholder digest this replaces), [ADR-0048](0048-external-build-artifact-ingestion.md)
  (the presigned-PUT ingest + validation the private-upload path reuses), and
  [ADR-0021](0021-reconciler-loop-drift-repair.md) (the periodic drift-repair loop the new sweeps
  extend).
- **Spec:** [`../superpowers/specs/2026-06-10-m24-image-rootfs-lifecycle-design.md`](../superpowers/specs/2026-06-10-m24-image-rootfs-lifecycle-design.md)
- **Milestone:** M2.4

## Context

Base-OS/rootfs images are the last day-2 operator obligation in the M2.x band that is still
unscripted. Three bash scripts under `scripts/live-vm/` build a local-libvirt rootfs by hand;
the remote-libvirt provisioning disk-image (ADR-0080) rides a placeholder digest; and the
rootfs catalog is read-only YAML in the source tree, loaded synchronously, with no publish,
versioning, or drift repair. A separate process cannot reconcile object-store state against a
YAML file on an operator's disk, so a half-published image or a storage leak is undetectable.

The band gate is operator-run on real hardware against the published image; an unscripted,
unverifiable image build cannot pass it.

## Decision

We will add `kdive.images`, a provider-agnostic subsystem with per-provider Python build
planes, a DB-backed catalog as the single source of truth, and reconciler drift repair.

1. **A `RootfsBuildPlane` port replaces the bash scripts.** Per-provider implementations
   (local-libvirt orchestrating the libguestfs stages in-process; remote-libvirt building a
   real provisioning disk-image) produce a qcow2 plus recorded provenance — pinned inputs such
   that a rebuild from the same spec yields a matching `build_id`/digest. The bash scripts are
   removed, not kept. The local plane is exercised on the live-stack path, not stubbed.
2. **The `image_catalog` Postgres table is the single source of truth.** Migration `0023`
   creates it; a data migration seeds the current `fixtures/local-libvirt/*.yaml` rows and the
   YAML files are removed. Resolution in the provisioning/`materialize` path moves to async DB
   reads. There is no dual backing.
3. **Publish/register is a two-write with the reconciler as the recovery path.** Publish the
   object, gate on `store.head()`, then register the row; a row is visible only after its
   object's HEAD succeeds (mirrors ADR-0048). Two reconciler sweeps repair drift —
   `leaked_images` (object, no row → delete object) and `dangling_images` (row, no object →
   remove row) — the same drift-repair pattern the platform uses for artifacts/Systems, not a
   bespoke rollback.
4. **Image management stays an operator surface.** Build/publish run as an `IMAGE_BUILD` job an
   operator verb enqueues, processed by the worker; the agent-facing MCP tool surface is not
   extended. Operator/mutating verbs route through the M1.3 break-glass path.

## Consequences

- The synchronous YAML catalog loader and the `fixtures/` source-tree catalog are removed;
  provisioning resolution becomes an async DB read in the resolver and its two callers.
- The reconciler gains two sweeps (three with ADR-0093's private-image prune) and matching
  `ReconcileReport` counts; per-pass cost grows by the object-prefix and per-row HEAD checks.
- Every new provider that ships a base image implements `RootfsBuildPlane`, as the systems
  registrar already requires a `rootfs_validator`.
- A new `IMAGE_BUILD` job kind, a migration `0023`, and a `services/images/` service layer
  shared by the worker and the `kdivectl images` verbs.
- The remote plane's real image closes a known M3-entry gap (the ADR-0080 placeholder digest).

## Alternatives considered

- **Keep the bash scripts, wrap them in a managed port.** Smaller change, but the scripts stay
  the build mechanism — provenance and reproducibility bolt on awkwardly around shell, and two
  build idioms (shell rootfs, Python kernel) persist. Rejected for a uniform Python build seam.
- **Keep the YAML catalog, add a separate DB table for managed images.** Avoids touching the
  hot resolution path, but creates two sources of truth and a union read; "register into the
  catalog" with reconciler row-sweeps then means a second catalog. Rejected per replace-don't-
  deprecate: one DB table, YAML seeded in and removed.
- **Model image build as a per-System run.** Reuses the runs lifecycle, but base-image build is
  operator infra, not an agent-driven per-System action; it would put operator infra on the
  agent MCP surface. Rejected for an `IMAGE_BUILD` job on the existing worker tier.
