# 0624 — Bind a staged-path catalog image to its inspected root

## Status

Accepted (2026-09-06)

## Context

ADR-0228 registered a local-libvirt `staged-path` image without a digest, and ADR-0296
carried the adjacent `build-fs` provenance sidecar into the catalog without using its
mechanically inspected root identity. That was sufficient for ordinary disk/GRUB boot, but it
left no catalog identity from which System admission could create the immutable root-provenance
snapshot required by ADR-0583 for external direct-kernel boot.

The sidecar already contains a closed `RootSpecV1` whose `stage-inspection` authority binds an
architecture and `staged-image` SHA-256 identity. Reconcile must remain a fast metadata pass: a
rootfs can be multiple GiB, so hashing every declared file during every pass is not acceptable.
The existing local-component path validator already streams an optional SHA-256 at admission and
materialization boundaries.

## Decision

For a `staged-path` image, inventory reconcile will promote `RootSpecV1.source.identity` into the
catalog row's optional digest only when the bounded sidecar contains a valid root specification
with `authority=stage-inspection`, `source.kind=staged-image`, and an architecture equal to the
inventory entry. Reconcile parses only the sidecar and never reads or hashes the image.

Materialization will pass that optional catalog digest to the existing local-component path
validator. It will therefore stream and compare the actual file bytes before provisioning uses
the path. A row without an eligible root specification keeps its previous digest-less
`staged-path` behavior. If an established row temporarily loses its sidecar, reconcile preserves
its existing provenance and derived digest; changing only the declared path cannot remove the
byte check.

System admission may resolve the resulting catalog row into the existing immutable
`system_root_provenance` snapshot. This applies only when admitting a new System. It does not
backfill, rewrite, or repair an existing System.

## Consequences

An operator can register an already built, mechanically inspected local image without copying it
through object storage or manually modifying Postgres. New Systems can use the catalog reference,
or a local reference carrying the same SHA-256, and acquire the root authority needed by external
direct-kernel boot.

Malformed, differently sourced, or architecture-mismatched root specifications remain visible as
ordinary provenance but produce a reconcile warning and no digest. Sidecarless paths remain
available under ADR-0228's declared-path trust model. A promoted digest adds one streaming read at
each materialization boundary; it adds no multi-GiB work to reconciliation and no schema migration.

A privileged operator can still replace both a staged image and its sidecar, which is unchanged
from the local provider's host-filesystem trust boundary. Replacing only the image is detected
before provisioning.

## Considered & rejected

- verified: `src/kdive/services/images/publish.py` creates a new image build and object-store
  publication; it does not register an already staged local file, so using it would add a
  multi-GiB copy and a different build workflow.
- verified: `src/kdive/inventory/reconcile/images.py` already reads sidecars with a 64 KiB bound,
  while `src/kdive/components/local_paths.py` already streams optional SHA-256 verification in
  1 MiB chunks. Re-hashing every image in reconcile would duplicate the byte gate on an
  unbounded-frequency path.
- judgment: Add a digest field to `StagedPathSource`: the mechanically inspected sidecar is the
  existing authoritative producer, while another operator-entered value could disagree with it.
- judgment: Backfill old Systems after catalog reconciliation: System root provenance is an
  immutable admission snapshot, so changing it later would replace history rather than attest a
  new System.
