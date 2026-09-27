# ADR 0687 — Reproducible lab fixture identity

## Status

Proposed

## Context

Issue #2805 prepares the lab/input foundation for #2803. ADR-0388's warm store checks artifact
hashes and a kernel NVR but does not bind reuse to its catalog selection, builder revision or
retained package provenance. The operator approved extending existing preparation paths.

## Decision

The live-vm stores retain one versioned JSON manifest binding a checksum-pinned catalog source,
clean builder commit tied to the executing Python import source, NVR/build ID, and the
rootfs/kernel/debuginfo/provenance/config hashes.
Its canonical digest identifies the fixture. Warm reuse verifies the complete record and bytes;
legacy or mismatched records rebuild through the existing atomic store publication path.
TCG staging uses the same evidence. Existing wiring output is unchanged. This amends ADR-0388's
reuse key and internal manifest representation; its matching-debuginfo and two-store decisions stand.

Use existing source verification and provenance owners. Template-only virt-builder inputs cannot
enter this reproducible store until pinned source evidence exists; direct build-fs stays available.
Keep target identities private and publish selected capability/fixture facts. Explicit native and
foreign preflight remain prerequisites, not successful scenario results. The operator owns lane
reservations; resource observations do not allocate capacity.

## Consequences

An old warm set needs one rebuild. A changed builder or catalog input invalidates warm reuse.
Mutable package repositories can produce a different fixture ID, which requires requalification;
this does not promise byte-identical rebuilds. Missing provenance fails staging rather than
silently producing release evidence. Full scenario and native POWER qualification remain downstream.

## Considered & rejected

- Keep NVR-only reuse. verified: `warm-store.sh:is_warm` at 77fb30b7f tests NVR and three
  file digests without comparing the selected catalog image or build provenance.
- Add a separate qualification store. judgment: duplicates existing staging, locking and
  publication ownership for this foundation slice.
- Add a lab scheduler. judgment: unnecessary for the operator-approved sequential reservations.
