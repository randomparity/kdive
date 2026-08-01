# 0527 — Scope forced daemon termination to teardown

## Status

Accepted (2026-08-01)

## Context

The shared `stop_daemons` helper sends SIGTERM and waits ten seconds. Workers finish their current
job before acting on SIGTERM, so teardown can leave one alive while reporting success. The same
helper runs before every bring-up; escalating there would abandon legitimate work whenever an
operator starts the stack.

## Decision

Forced daemon termination is an explicit teardown-only operation exposed as `down.sh --force`.
The command always performs the existing graceful stop first, then SIGKILLs only matching daemons
which remain. Plain teardown and bring-up remain graceful-only. Signal-delivery failures and
post-SIGKILL survivors are named; a failed forced stop prevents backend teardown.
Before SIGKILL, teardown rescans the daemon matcher and skips a discovered pid which no longer
matches. Portable shell cannot make process discovery and signalling atomic, so a residual reuse
window remains between that final check and `kill`; keeping the check adjacent bounds that risk
without adding a second process-control mechanism.

## Consequences

- Operators have one supported way to end a stack whose worker ignores SIGTERM.
- `up.sh` never escalates, preserving running jobs during bring-up attempts.
- `--force` may abandon jobs; their existing queue leases govern later reclaim and attempt use.
- Force failure is visible and leaves backends running for diagnosis or retry.

## Considered & rejected

- **Escalate in `stop_daemons`.** Its bring-up callers make that boundary too broad.
- **Always escalate in `down.sh`.** Plain teardown would unexpectedly become destructive.
- **Keep prescribing manual `kill -9`.** It duplicates pid discovery and privilege decisions in
  operator instructions instead of providing a tested lifecycle command.
