# 0613 — Admit initial external boot atomically

## Status

Accepted (2026-09-06)

## Context

An authority-marked boot needs an immutable plan, an activation, reserved recovery capacity, and
a durable job. Creating any subset exposes work the worker cannot safely execute. The public boot
tool already serializes on System then Run, replays settled work before admission, and applies the
external-boot operation matrix.

## Decision

When a fixed authority instance is configured, `runs.boot` constructs the plan only from the
installed build evidence and persisted System root provenance. Under the existing System then Run
locks it re-reads the Run and provider binding, replays an already admitted boot before applying
the `RUN_BOOT` matrix, and then creates the deterministic preparing activation, pending capacity
reservation, and authority-marked job in one transaction. The server stores only the authority
identity and reservation geometry; worker TLS material and provider I/O remain worker-owned.

Store identity and positive reserve and maximum byte counts are operator-owned configuration.
Reserve cannot exceed maximum, and the worker configuration must agree with the persisted reserve.
Before materialization the fenced current worker atomically changes the reservation from pending
to ready under the canonical recovery-store lock. Exhaustion is retryable and performs no provider
mutation. A ready replay does not debit twice; stale jobs, leases, incarnations, generations,
owners, routes, and missing reservations are superseded.

`force` retains the ordinary boot contract: running work is refused and settled work is recycled
before a fresh admission. A request cannot select an authority destination. Unconfigured installs
and boots retain their prior behavior.

## Consequences

Local roots without immutable provenance fail with re-stage guidance. A configured route that is
absent, incomplete, changes while locked, or disagrees with the selected provider fails before a
new activation or job. A cleaned deterministic activation is not silently replaced with a second
schema; callers receive the bounded repository conflict until lifecycle recovery permits reuse.

## Considered & rejected

- **Create the activation before taking the public boot locks.** This bypasses replay and matrix
  admission and races binding changes.
- **Debit capacity in the server.** The server does not hold worker lease and store-mutation facts.
- **Accept a request-selected authority route.** That would make credentials and provider mutation
  caller-routable.
