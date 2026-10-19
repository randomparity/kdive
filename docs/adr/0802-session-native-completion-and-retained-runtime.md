# 0802 — Session native completion and retained runtime

## Status

Accepted (2026-10-18)

Implements the operator-approved transient-helper completion contract for #3245.
Extends ADR-0801 without changing its directory identity or same-UID trust limits.

## Context

A transient domain creation call can raise after submitting native work. Reaping its
client does not establish that the daemon completed that work. Suppressed destroy and
connection-close failures also hide incomplete cleanup. Removing the short session
runtime in either case can discard resources still needed by uncertain native work.

## Decision

`boot_gdbstub_domain` accepts an optional lifecycle observer with four phases:
`create-submitting`, `create-returned`, `destroy-submitting`, `destroy-returned`.
The domain handle is assigned before observing successful creation. A failed creation
submission observer prevents creation. A failed destruction submission observer records
its error but still allows the existing owned-domain destruction attempt exactly once.
A successful destroy return is positive native completion; an inactive snapshot is not.

Native uncertainty or a lifecycle receipt failure after submission retains the session
runtime directory. Restoration first returns the previous XDG environment; retention
never removes the directory or marks it removed. Connection-close failures also retain
runtime. Ordinary successful callers preserve their existing yielded value and cleanup.
No observer is required for these strict default failure semantics.

The helper preserves a sole original exception object. Multiple primary and cleanup
errors are raised in an ordered `BaseExceptionGroup`, primary first. Destruction,
connection close and environment restoration are attempted as applicable even when an
earlier cleanup action fails. The helper does not retry uncertain native calls.

## Consequences

Callers must now handle visible cleanup errors instead of assuming successful teardown.
Domain ownership stays with the helper; backing storage stays with each caller. The
private qualification parent separately requires matching native completion receipts
and successful owned-child completion before deleting its backing resources. This does
not change other callers' existing disk-cleanup policies or promise universal storage
retention. Neither process exit nor an absent domain inventory substitutes for native
completion. A later proof-publication failure cannot recreate an already removed runtime
whose native completion was positively established.

## Considered & rejected

- Keep suppression or infer completion from client reap: neither establishes daemon
  completion, and both can discard uncertain runtime state.
- Add a native cancellation controller or another helper return API: unnecessary for
  retaining uncertainty and making the existing lifecycle boundaries observable.
