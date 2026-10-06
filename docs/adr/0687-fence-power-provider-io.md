# 0687 — Fence power provider I/O through completion

## Status

Accepted (2026-09-26)

Amends [ADR-0685](0685-operator-power-off-clean-shutdown.md)'s OFF-only worker fence.

## Context

ADR-0685 holds a System session lock while operator OFF waits for the provider. ON, CYCLE, and
RESET still release their READY precheck's transaction lock before provider I/O. A force-crash job
can commit CRASHING in that gap, allowing the older power job to touch the guest after the marker.
RESUME uses a separate PAUSED-to-READY flow and is named in issue #2831's power-action scope.

## Decision

The worker holds the same System session advisory lock from each power action's state precheck
through its provider call. For RESUME, the lock also covers its PAUSED-to-READY update and audit;
the existing READY idempotent no-op remains. Short transaction locks continue to nest under the
session lock. On cancellation, the handler waits for the fenced task, including its provider
thread and connection-mode restoration, before propagating cancellation. OFF retains its clean
shutdown wait contract from ADR-0685. Force-crash marker and NMI behavior is unchanged.

## Consequences

- A force-crash marker waits for an earlier power call's provider I/O to finish. A power call
  arriving after the marker is rejected by its existing state check.
- A cancelled power job may wait for a blocking provider call to return before its System lock
  and connection are released. Non-OFF provider calls have no new timeout.
- Existing action authorization, provider port, audit fields, and RESUME state rules remain.
- The shielded task returns its provider-kind metrics tag to the worker task on success, failure,
  or cancellation, retaining the existing worker telemetry label.

## Considered & rejected

- **Keep transaction-only prechecks for short power actions.** verified: the branch base
  `77fb30b7f` releases `_power_target`'s transaction lock before `control.power` in
  `src/kdive/jobs/handlers/control/control.py`; the race described in #2831 remains possible.
- **Give each action a separate session-lock implementation.** judgment: repeating lock and
  cancellation cleanup increases the chance that one action releases its fence early.
- **Change force-crash to wait after committing CRASHING.** judgment: that leaves a power job
  free to run after the marker, the ordering this issue requires the worker to prevent.
