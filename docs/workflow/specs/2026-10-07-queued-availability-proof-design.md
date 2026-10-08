# Queued availability proof

## Scope and evidence

Issue #3106 requires actual nonzero queue evidence in the existing catalog tool carrier,
followed by withdrawal and restored zero. The approved scope is the carrier and its owning
regressions; full accounting proofs remain #3098 and native POWER qualification #2818.
No production admission, promotion, quota, transport, or authorization contract changes.

The current carrier grants one allocation but never requests queue admission. Its independent
SQL oracle compares only total depth. ADR-0069 already governs requested allocations and
withdrawal; ADR-0722 governs the existing tool-cell matrix. No new ADR is necessary.

## Controlled fixture

Use the existing funded project and HTTP allocation/accounting tools, with read-only SQL
as the independent oracle. A unique fixture subject identifies its allocations even if an
HTTP response is lost. Preserve the existing general `_granted` helper for other carriers.

Before mutation require an empty global queue, no occupying allocations, exactly one
eligible local-libvirt host visible to the funded project, capacity one and room for the
existing 1-vCPU/1-GiB/1-GiB request, and an existing quota permitting one grant. An unsuitable
fixture is BLOCKED with an actionable precondition; do not change host capacity or saturate
an arbitrary fleet. This matches the verified disposable service fixture.

Snapshot all three quota caps. Through `accounting.set_quota`, retain both concurrency
caps and set pending capacity to at least two. Hold one by-ID grant on the selected host.
Request one local-libvirt by-kind and one same-host by-ID allocation with `on_capacity=queue`.
Require both envelopes and database rows to be `requested`, with their distinct selectors.
Compare the complete queue aggregate with independent SQL: total two, local-libvirt by-kind
one, and by-ID one. Preserve the existing per-host occupancy, headroom, and shape assertions.

## Failure model and cleanup

Enter cleanup protection before the first quota write. Discover only this unique subject's
committed rows in the funded project, including visible rows whose reply was lost. An absent
row cannot establish completion of an in-flight request. Track indeterminate request outcomes
and retain known blockers when any allocation mutation completion is uncertain, reporting the
subject and known IDs for operator reconciliation. Do not add a cancellation protocol. Withdraw queued rows
through `allocations.release`, attempting each independently, before releasing any owned
grant. Verify withdrawal by database read. If withdrawal or its verification fails, retain
blocking grants and report their exact IDs with the unresolved queue IDs and recovery order.
A failed cleanup is never a passing cell, including when the original assertion also failed.

Attempt restoration of every original quota cap independently of allocation cleanup failure,
and verify the values by SQL. Existing quota administration allows restoring pending zero
with residual requested rows; that does not withdraw them or make cleanup successful.
Recovery uses existing tools: withdraw listed queued IDs, verify released, release listed
blockers, then restore the recorded caps. Audit/history rows and quota timestamps remain.
On success prove all owned rows released, queue zero, original host occupancy and all caps.
The exclusive disposable proof window avoids concurrent quota writers; races fail the proof.

## Validation and limits

Boundary doubles exercise the actual carrier, full SQL aggregate comparison and ordered
failure cleanup. Faults must detect omitted queue creation, an incorrect by-kind/by-ID split,
release-before-withdraw, skipped quota restoration, and hidden cleanup errors.
Update the owning live-testing runbook with topology, normal cleanup and bounded recovery.
Run all twelve existing availability cells, covering direct/gateway and default/recovery
configurations, against an attested candidate. No guest or provider operation is required.
Retain configuration-specific outcomes and cleanup records. Ordinary tests are not HTTP
proof, and architecture-independent evidence does not qualify native POWER.
