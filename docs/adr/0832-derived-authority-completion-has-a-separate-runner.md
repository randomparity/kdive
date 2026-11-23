# 0832 — Derived authority completion has a separate runner

## Status

Accepted (2026-11-23)

## Context

At 80285cfd2, release and teardown finish their authority receipt transactions
inside run_operation's before_port callback. They must nevertheless supply
provider and result callbacks that only raise assertions. The release wrapper
also casts its result. Ordinary activation and recovery genuinely need the
optional early result hook for deadline and attempt commits.

## Decision

Keep admission, invocation-bound port assembly, authority acknowledgement and
preparation at one owner in the external-boot runner module. After that shared
setup, use run_operation for the ordinary provider/result path and
run_derived_operation for one async completion callback with no provider or
result placeholder. Preserve the exact returned carrier subtype.

The derived callback retains COMMIT-phase failure translation and its existing
receipt/replay behavior. The worker still distinguishes a completion whose
root transaction already finished from an ordinary result it must commit.
ADRs 0593, 0614 and 0620 retain their marker, authority and durable ownership
rules; this record changes the internal call shape, not their protocols.
The ordinary early result hook remains because its intermediate result leaves
the job running for re-entry under the existing readiness policy.

## Consequences

There is one context setup and two explicit execution paths. Invocation-bound
ports and effective timing travel together through setup, so failure deadlines
use the same settings as provider execution. No new workflow, schema, permission
or provider API is introduced. The three assertion callbacks and unused recovery
factory are removed. Typed producer construction is a following separate change.

## Considered & rejected

- **Keep assertion callbacks.** verified: reading run_operation, release_handler
  and teardown_handler at 80285cfd2 shows each derived callback returns before
  the required provider/result functions can run on a normal exit.
- **Make provider/result callbacks optional in one runner.** judgment: that
  permits invalid combinations and moves mode checking into runtime branches.
- **Add operation strategy objects.** judgment: two existing execution shapes
  need a function boundary, not a new object hierarchy or workflow abstraction.
