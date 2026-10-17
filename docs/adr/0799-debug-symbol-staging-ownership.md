# 0799 — attachment-owned GDB symbol staging

## Status

Accepted (2026-10-16)

Operator approved bounded D1–D4 implementation; required live proof remains separately gated.
Issue #3229; approved proposal SHA256
7e4a8f84ab8c30eb77601a97f310d6be5e2236bfc3680662940da634b728563b.

## Context

#3229's successful stage_and_attach loses directory ownership; runtime reap exits GDB without
removing files. Module trees are cached by Run for process lifetime. Separate reconciler cannot
reach the owner-process registry. ADR-0034 already makes GDB attachments process-local;
ADR-0278's module-symbol behavior must retain readable files while GDB uses them.

## Decision

Use one private root per attachment, lazy modules beneath it, and no process-wide run cache.
The MCP runtime retains ownership until GDB/thread quiescence and root deletion succeed. Explicit
end, owner-process DB-state sweep and orderly shutdown invoke that same retryable cleanup path.
Expose incomplete cleanup through existing infrastructure_failure with recovery by end_session.
Do not delete historical roots or add durable ownership; bound recovery to orderly owner lifetime.

## Consequences

Same-Run attachments duplicate module staging, trading deduplication for independent ownership.
Canonical session identity keys operations and cleanup; shutdown tracks first attaches before
ownership publication. Cancellation drains the dispatched thread before releasing the cleanup lock. Exit failure
retains files and owner state for retry. Internal resolver/attachment contracts migrate together.
New public failure wording and abrupt-death limitation have explicit D1–D4 implementation approval; live proof remains gated.

## Considered & rejected

- Do nothing. verified: private repro at 0b177a335 exits 1 after controller exit with the
  staged root still present; leaves C1–C3 unmet.
- Per-Run reference counting. judgment: coordination and failure states exceed an unmeasured
  deduplication benefit; independent attachment roots are easier to verify.
- End-session-only cleanup. verified: reconciler/repairs/debug_sessions.py detaches rows in a
  separate process without the MCP registry, so abandoned sessions require owner reconciliation.
- Delete in finally regardless of exit. judgment: incompatible with required after-exit ordering.
- Durable cross-replica ownership. judgment: exceeds this process-local repair and #2803 non-goals.
