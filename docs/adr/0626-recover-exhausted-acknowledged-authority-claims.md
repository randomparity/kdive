# 0626 — Recover exhausted acknowledged authority claims

## Status

Accepted (2026-09-06)

## Context

The durable queue charges an attempt when a worker claims a job. A worker can therefore consume
the final configured attempt, install and acknowledge an external-boot authority watermark, and
then stop before core records the acknowledgement or the authority admits a provider mutation.
The expired job remains `running`, but ordinary claim eligibility requires
`attempt < max_attempts`. Generic abandoned-job repair deliberately cannot terminalize an
authority-marked job because it has no authority receipt. The exact acknowledged no-mutation
state is consequently stranded even though ADR-0625 permits a successor authority. Failed
successor watermark attempts made before ADR-0625 can also leave newer allocating authority rows
while the trusted head remains on an older acknowledged predecessor.

## Decision

An expired external-boot job at exactly `attempt == max_attempts` receives one replacement claim
when the database can verify an exact `takeover-acknowledged` journal head for its authority chain.
The proof must bind the job, System, activation, Run, plan, purpose, provider, authority instance,
operation, operation identity, authority generations, journal sequence, and journal digest. The
head must have no pending takeover or suspended operation. It may belong to the latest authority,
or to an older acknowledged predecessor followed only by same-operation, unacknowledged,
superseded allocation attempts and one latest allocating authority bound to the exhausted job
attempt. No intervening authority may carry an acknowledgement or divergent binding. An allocating
head authority must have no core acknowledgement; a current head authority must have the matching
immutable core acknowledgement. Missing, malformed, divergent, non-latest, or partially advanced
evidence remains unclaimable.

The atomic claim increments both `attempt` and `max_attempts`. Ordinary claims and handler failures
retain their configured retry budget. A replacement that also stops before provider admission can
receive another claim only after it independently produces another exact acknowledged
no-mutation head. The queue-depth function uses the same eligibility predicate as claim. Integer
exhaustion fails closed rather than wrapping either counter.

## Consequences

The queue no longer strands the recoverable state that ADR-0625 admits. The exception does not
retry a poison handler or infer that a provider call ended: the authority journal proves that no
provider mutation was admitted. Every replacement remains a distinct job attempt and authority
generation, so stale worker and result fences remain intact.

`max_attempts` continues to bound ordinary dispatches. Its persisted value may grow by one only as
part of an acknowledged no-mutation replacement claim, making the exceptional allowance visible
in the job row.

## Considered & rejected

- Dead-letter the exhausted job: generic repair has no authority result and would invent a
  terminal lifecycle outcome.
- Reuse the exhausted attempt: attempt identity is part of the worker and authority fences, so
  reuse would let stale writes share the successor's claim.
- Admit every exhausted authority-marked job: a marker alone does not prove provider quiescence.
  Exact acknowledged journal evidence is required.
