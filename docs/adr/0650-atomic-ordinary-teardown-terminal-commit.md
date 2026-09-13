# 0650 — Atomically finalize ordinary System teardown

## Status

Accepted (2026-09-13)

## Context

Ordinary `systems.teardown` commits `systems.state = 'torn_down'`, calls the provider, then
discharges remote-module mutation obligations through the worker-fenced repository method. A
worker death or discharge failure between those commits leaves a terminal System with an open
obligation. `systems.teardown` then short-circuits, so the ordinary path cannot retry the
discharge. ADR-0634 repairs those leaked rows but identifies this handler as their continuing
producer.

The early terminal write also fences a slow provision: its commit sees a terminal state and reaps
its just-created domain rather than reviving a System whose teardown has started. Deferring that
write until after the provider call without another fence reopens that race. Discharging before
the provider call is not acceptable either: a failed provider call would leave a live System whose
retention obligation has been released.

## Decision

Add the non-terminal `tearing_down` System state. Ordinary teardown transitions into it under the
existing System advisory lock before calling the provider. It is a lifecycle fence: a provision
that completes after this state is committed reaps its just-created domain, it retains capacity and
rootfs ownership, and it has exactly one successor, `torn_down`.

After the provider confirms absence, the same locked database transaction discharges mutation
obligations, updates `tearing_down -> torn_down`, and writes the audit record. A failed provider
call or failed final transaction leaves `tearing_down`, never `torn_down`. The reconciler retries
a fenced System when its ordinary teardown job is absent or no longer active: it recycles only a
failed or succeeded row under the same deduplication key, never selects an operator-canceled row.
The repair is capped at 100 candidates per pass and rechecks state and job activity under the System
lock before it requeues. Add a migration for the database state constraint and update direct
state-set consumers and generated references.

## Consequences

- `torn_down` becomes a stronger fact: ordinary teardown cannot commit it with an open mutation
  obligation.
- A System can visibly remain `tearing_down` while provider deletion or its retry is in progress.
  It still consumes its allocation's capacity and pins its rootfs base.
- A dead-lettered ordinary teardown is retried by the bounded reconciler lane, including while its
  Allocation is still live. Operator cancellation remains a stop: the lane never recycles a
  canceled job.
- The existing reconciler repair lane remains responsible for historical leaks; it is not removed
  or widened by this change.
- The state is a public returned value, so generated CLI and MCP reference artifacts must be
  regenerated in the same change. It is excluded from console scheduling, matching the prior
  `torn_down` behavior after teardown begins.

## Considered & rejected

- **Discharge before the provider call.** verified: the current helper documents discharge as
  occurring only after provider-proven physical absence, and provider failure must retain module
  volumes for the still-live System.
- **Move only the `torn_down` update after the provider call.** verified: the current early update
  is the admission fence for a slow provision; `tests/adversarial/test_provider_state_races.py`
  proves the fence prevents a new domain from committing during teardown.
- **Keep `torn_down` as the early fence and rely on the reconciler.** verified: ADR-0634 identifies
  that ordering as the only continuing producer of the leaked pair; repair does not remove the
  cause.
- **Use a job-state predicate as the fence.** judgment: lifecycle state is the existing guarded
  cross-handler fence, whereas cancellation can make a still-running teardown job terminal before
  its provider call completes.
