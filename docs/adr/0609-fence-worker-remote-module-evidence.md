# 0609 — Fence worker remote-module evidence writes

## Status

Accepted (2026-09-06)

## Context

Remote-module restore and reap run in the worker process, while the durable attempt obligations
they advance are protected from direct worker writes. Granting table mutation would let any worker
alter another System's retention evidence. Using a server or administrator connection in provider
work would bypass the deployment's role boundary.

The evidence commit must also survive process loss without accepting caller-selected attempt
coordinates. The existing job lease and incarnation credential identify the active worker, and the
closed module-attempt receipt in the durable job payload identifies the only attempt that job may
advance.

## Decision

Migration 0134 adds one `SECURITY DEFINER` function for the two worker-owned evidence transitions:
recording write-once terminal evidence while opening reap retention, and discharging an already
open reap obligation. `PUBLIC` has no execute privilege; only `kdive_worker` may call it. The
function fixes an empty search path and checks the active protocol-4 incarnation credential, exact
running job and attempt number, and exact receipt tuple stored in the job payload. It then takes the
System advisory lock and locks the exact obligation row before applying its guarded transition.

The worker runtime receives a typed context containing only the job identifier, job attempt,
incarnation credential, and closed preparation request. Missing or stale context fails closed.
Workers retain `SELECT`-only table grants. Mutation-obligation opening and discharge remain
server-owned and are not exposed by the function.

## Consequences

A restarted worker may replay the same terminal or reap evidence transition, but cannot select a
different job, attempt, System, Run, or nonce. A cancelled job or stale incarnation cannot advance
retention state. Transaction rollback leaves no authorization residue, and ordinary worker SQL
still cannot update the obligation table directly.

## Considered & rejected

- **Grant worker table updates.** Rejected because row predicates in application code are not a
  database authority boundary.
- **Use a server pool from the worker.** Rejected because it gives provider execution unrelated
  server authority.
- **Carry only a nonce in process memory.** Rejected because a nonce not bound to the durable job
  payload is caller-selectable and cannot prove ownership after restart.
