# 0607 — Persist remote module recovery geometry

## Status

Accepted (2026-09-06)

## Context

Remote-module cleanup and restore must reopen deterministic source and scratch volumes after the
worker that prepared them has exited. The version-1 recovery reference identifies those volumes,
but it does not record the source capacity needed to validate their immutable geometry. Rebuilding
the original module entries merely to recover that value makes restart recovery depend on inputs
that are neither durable nor required for deletion.

## Decision

New preparation results and obligation evidence emit provider-private recovery reference version 2.
It retains the version-1 operation, provider, pool, and source-content identity and adds the actual
prepared source capacity in bytes. The capacity is an integer other than `bool`, positive, aligned
to 4096 bytes, and no larger than 10,499,653,632 bytes, the maximum produced from the accepted
source-input bounds.

Recovery derives and validates both deterministic volume requests from the persisted reference.
It does not reconstruct module entries or an image writer. Reopen, restore, teardown, deletion,
inventory, and reap therefore need only the durable exact operation tuple, fixed provider binding,
and version-2 reference. A capacity or identity mismatch fails closed before provider mutation.

Migration 0133 permits stored version-1 rows for read compatibility and admits version 2 without
rewriting historical evidence. Runtime recovery rejects a version-1 reference because its geometry
cannot be authenticated. New evidence writes version 2. A fresh preparation may still use the
ordinary entries and writer to create a new attempt; those inputs are not recovery prerequisites.

## Consequences

Worker restart can validate, restore, and reap existing volumes from durable references alone.
Existing version-1 evidence remains readable by the repository but cannot authorize provider
recovery; an operator must resolve that legacy obligation through an explicit repair path. The
reference duplicates one bounded derived value so recovery does not recreate transient source
inputs.

## Considered & rejected

- **Rebuild entries during recovery.** Rejected because deletion and validation would depend on
  transient preparation inputs after a worker restart.
- **Infer capacity from the live volume.** Rejected because provider state cannot define the
  expected geometry used to authenticate that same provider state.
- **Rewrite version-1 evidence in place.** Rejected because the missing capacity cannot be derived
  from the durable reference without consulting untrusted live state or unavailable inputs.
