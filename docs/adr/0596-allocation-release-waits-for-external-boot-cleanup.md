# 0596 — Allocation release waits for external-boot cleanup

## Status

Accepted (2026-09-04)

## Context

An external-boot authority is allocated only while its owning Allocation is `active`.
`allocations.release` currently changes that Allocation to `released` without consulting the
System-wide external-boot admission matrix. If the System still has an activation whose cleanup is
incomplete, later release, cleanup, or teardown jobs are then refused by the authority allocation
function. The recovery objects and restricting activation can no longer converge.

Allocation release already holds the project and Allocation locks. External-boot admission is
serialized by the System lock, and the global co-hold order is
`PROJECT -> ALLOCATION -> SYSTEM`.

## Decision

Add `allocation_release` to the closed external-boot operation vocabulary and admit it only when no
external-boot activation restricts any System owned by the Allocation. The allocation release
service discovers those Systems while holding the Allocation lock, takes their System locks in
stable identifier order, and runs the ordinary matrix guard before changing Allocation state.

An outstanding activation therefore returns the matrix's existing `conflict` denial and leaves the
Allocation active. The caller first completes the external-boot release or teardown path; a retry
after cleanup is the ordinary idempotent allocation release.

Requested Allocations have no System and retain their direct cancellation path. Already-terminal
Allocations retain their existing idempotent or stale-handle behavior without consulting the
matrix.

## Consequences

- External-boot authority acquisition remains reachable until cleanup has completed.
- Release takes one bounded System-lock sequence in addition to its existing locks. Sequential
  historical Systems are included because any uncleaned activation still relies on the Allocation.
- The matrix remains the single definition of whether an activation restricts an operation.
- `docs/debt/0007-allocation-release-bypasses-the-external-boot-matrix.md` can close with an
  executable regression test.

## Considered & rejected

- **Admit Allocation release and teach authority acquisition to work after release.** judgment:
  this widens the lifetime and authorization model of every external-boot authority operation to
  support an ordering the caller can avoid.
- **Check without taking each System lock.** verified: `src/kdive/services/external_boot/admission.py`
  documents the System lock as the serialization boundary, so an unlocked check can race the
  activation create or cleanup edge it is meant to decide.
- **Inspect only the newest System.** verified: the top-level domain model permits sequential
  Systems per Allocation, while the authority SQL binds every activation to the same Allocation;
  an older uncleaned activation still needs that Allocation to remain active.
