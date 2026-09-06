# ADR-0614: Derive external-boot release phases from one immutable request

## Status

Accepted

## Context

An active external-boot activation cannot be released by accounting alone. It must first restore
the source boot, delete and verify the recovery objects, and only then credit reserved capacity.
Each provider mutation must remain bound to an authority journal operation, while the public job
must not become terminal before the whole release is complete.

## Decision

The public release job retains its immutable `purpose=release`, `operation=release` marker. After
that root authority is allocated and acknowledged, SQL derives two bounded phase bindings from the
root authority, claimed job attempt, worker incarnation, activation, plan, and request identity:
`recover` and `cleanup`. Their identities and digests are domain-separated canonical hashes.

The authority host resolves and journals only those exact derived bindings. The worker commits the
recover result to move `active` to `recovered`. The cleanup phase deletes the provider objects and
must return an `absent` observation. Its commit stores one immutable authority-bound cleanup receipt
but does not release capacity or mark cleanup complete.

The final root release result consumes that exact receipt in the same transaction that inserts the
reservation release and marks the activation cleanup complete. A missing, stale, mismatched, or
already-consumed receipt fails closed. Re-entry resumes from the durable activation/receipt state;
it never repeats a phase whose exact terminal journal and committed receipt already exist.

Low-level release handling for already recovered legacy callers remains supported, but an active
public release always uses the derived sequence.

## Consequences

Provider deletion and verified absence precede capacity credit. One public job remains running for
the full sequence, and each host mutation retains an independently replayable journal record without
rewriting the root payload. The schema gains a narrow receipt table and phase-specific resolver and
commit functions; it gains no generic workflow or payload-mutation mechanism.
