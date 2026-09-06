# 0617 — External-boot debug detach remains an unblocking operation

## Status

Accepted (2026-09-06)

## Context

The shipped external-boot admission matrix permits debug detach in every restricting activation
state and does not restrict it to the activation's owning Run. ADR-0583's restricted-state and
owning-Run clauses describe a narrower rule. Debt record 0006 records that discrepancy.

Release refuses while any Run on the System has a live DebugSession. Restricting detach to the
activation's Run would leave other admitted sessions blocking release with no contributor-owned
way to close them. Denying detach after activation enters recovery also strands an existing
provider transport; it does not prevent a new attachment.

## Decision

This decision supersedes only ADR-0583's debug-detach restrictions. Debug detach is admitted in
every restricting external-boot state and for any Run on the same System, subject to the existing
DebugSession authorization and lifecycle checks. It closes an existing session; it does not
implicitly release the external boot or authorize another provider mutation.

Debug attach remains admitted only in `active` and only for the activation's owning Run.
Release continues to refuse any live DebugSession on the System, regardless of its owning Run.
No admission-table behavior changes with this documentation correction for #2204.

## Consequences

Contributors can close sessions that block recovery or release without acquiring System teardown
authority. The existing matrix, reverse-admission, and release-refusal tests protect these rules.
Debt record 0006 is resolved without relaxing debug attach or narrowing the release precondition.

## Considered & rejected

- Restrict detach to the owning Run: a different Run's live session would still block release.
- Ignore other Runs' sessions during release: that would permit a kernel swap underneath them.
- Deny detach during recovery: this would leave existing transports open without protecting a
  new operation boundary.
