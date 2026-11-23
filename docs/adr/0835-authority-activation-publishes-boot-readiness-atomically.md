# 0835 — Authority activation publishes boot readiness atomically

## Status

Accepted (2026-11-23)

## Context

At2f2133184, the authority result function commits activation and job success before
Worker records a succeeded boot step through another transaction. A process exit or
exhausted recorder retries leaves debug attach refusing that successful activation.
A real worker/provider-role PostgreSQL reproduction observes succeeded job, absent
step and boot_first before any follow-up worker call; its readiness assertion fails.

## Decision

Add migration0204 using the existing guarded pg_get_functiondef amendment pattern.
Insert the boot readiness row in the installed function's activate branch, after
its validated activation UPDATE, under its existing System and Run locks and the
same transaction as job success, authority retirement and audit. Let insertion
errors abort that transaction. Keep ON CONFLICT(run_id,step) DO NOTHING semantics.

Remove Worker’s separate boot-step writer, retry loop and private helper predicates.
The worker consumes the fenced result as before, with no second publication step.
Keep the function signature, grants, security settings, binding checks and replay.
Do not change existing authority admission or ordinary forced-reboot policy.

## Consequences

A fresh activation with no existing boot row publishes its ready result atomically.
An existing boot row, including a conflicting one, is retained rather than replaced.
A failed insertion leaves the authority attempt available under its existing failure
and reclaim policy; success is not published without that write. This does not backfill
rows from earlier worker failures or alter existing conflicting evidence.

## Considered & rejected

- Retain post-commit retries: [fact] the current worker uses a separate transaction
  and its exhaustion test expects no step. The real-role reproduction above exposes
  the gap even before retries; process exit cannot be repaired by that worker.
- Insert in the generic queue wrapper: [judgment] direct SQL callers and other
  result adapters would still lack the atomic invariant; the fenced branch owns
  the verified Run/System binding and existing locks.
- Replace existing boot rows: [fact] current _write_boot_step uses ON CONFLICT DO
  NOTHING. Overwriting them would change evidence-conflict behavior beyond this fix.
- Change forced authority boot admission: [fact] the ACTIVE matrix excludes RUN_BOOT
  and a real PG probe confirms denial before deletion. This fix preserves that policy.
