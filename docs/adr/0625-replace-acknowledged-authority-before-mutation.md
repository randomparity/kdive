# 0625 — Replace an acknowledged authority before mutation

## Status

Accepted (2026-09-06)

## Context

ADR-0584 allows a durable job retry to allocate a newer authority generation. The journal SQL
already accepts a successor after an in-flight mutation or a terminal record, but omitted the
state where the prior authority was acknowledged and its provider mutation never began. A worker
loss in that interval leaves the journal at `takeover-acknowledged`. The successor can append its
local watermark, but the database rejects the same record, leaving the authority host fail-closed
on a file/database head mismatch.

## Decision

An allocating successor may append `watermark-installed` after a
`takeover-acknowledged` head when that head has no pending takeover. The existing allocation,
active-worker, generation, immutable operation, expected sequence, and expected digest checks
remain unchanged. The successor must then complete the existing watermark and acknowledgement
protocol before any provider mutation.

## Consequences

A worker replacement can resume an operation that its acknowledged predecessor never started.
The transition does not infer provider completion, skip takeover acknowledgement, accept a stale
generation, or repair a divergent local journal. Existing fail-closed recovery remains unchanged
for every uncommitted local suffix.

## Considered and rejected

- Treat the acknowledged head as terminal: no provider outcome exists, so that would invent
  evidence.
- Reuse the prior authority from a new worker incarnation: authority identity includes the active
  worker incarnation, so reuse would bypass the worker fence.
- Truncate a rejected watermark automatically: a rejection can also signal concurrency or
  tampering; automatic truncation would erase evidence needed for operator recovery.
