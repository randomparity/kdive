# 0615 — Authority-owned recovery orphan disposition

## Status

Proposed (2026-09-06)

## Context

Recovery cleanup can leave bounded provider objects in quarantine when a worker loses its
attempt. The administrative request already records the selected objects and disposition, but a
worker that reads those rows and calls a provider port can alter object references, ownership
facts, or a replacement attempt's outcome.

## Decision

The worker sends a closed authority request containing only the durable orphan-request UUID, the
claimed job UUID, and its positive attempt number. It uses the configured fixed authority route;
the request contains no provider path, object binding, disposition, authority identity, or
credential reference.

The authority authenticates the worker and resolves the exact running job, attempt, lease,
immutable payload digest, request deadline, and bounded quarantine selection in a privileged SQL
function. It derives provider bindings only from that selection, serializes the operation on the
existing System authority lane, re-observes before mutation, and commits each terminal disposition
with a second fenced SQL function. The commit is idempotent for the same request/job and rejects a
stale attempt or changed ownership evidence. The ordinary worker role has no quarantine or orphan
request table privilege.

## Consequences

Lost authority responses can be replayed without a second provider mutation. A later worker claim
must authenticate and pass its exact current attempt fence before it can resume incomplete work.
The public administrative tool remains responsible only for authenticated bounded admission; it
does not grant direct provider authority to the worker.

## Considered & rejected

- **Pass the selected object bindings in the worker payload.** A worker could replay or substitute
  private provider targets.
- **Give the worker a restricted provider port.** The credential and lifecycle fence would still
  be outside the authority boundary.
- **Hold a database transaction around provider I/O.** Provider work may block and must not retain
  database locks.
