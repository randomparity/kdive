# Stuck `tearing_down` System with external-boot history

This runbook covers a System that sits in `tearing_down` and has at least one external-boot
activation row. Such a System is residue from an ordinary teardown that ran before the worker
began refusing ordinary teardowns for any external-boot history
([ADR-0620](../../adr/0620-authority-owned-system-teardown.md), #2966). No new System reaches this
state.

**No supported manual step moves such a System to `torn_down`.** This page describes how to
recognize the System, which tools refuse it and why, and what lease expiry does and does not
end. Do not improvise a recovery from it.

## Symptom

The reconciler logs this WARNING once per System per process:

```text
reconciler: system <system-id> is stuck in tearing_down with external-boot activation
<activation-id>; the ordinary teardown is refused (external_boot_teardown_not_supported), so no
job is requeued and the System needs operator recovery
```

If an earlier `systems.teardown` or `ops.force_teardown` replaced the refused job with an
authority-marked `{system}:teardown` job, the reconciler skips the System without this
WARNING. Use the queries below to find it.

## Confirm the state

With authorized read-only database access, list each such System with its Allocation,
Investigation, and newest activation:

```sql
SELECT s.id AS system_id, s.state, s.investigation_id,
       a.id AS allocation_id, a.state AS allocation_state, a.lease_expiry,
       e.id AS activation_id, e.state AS activation_state, e.cleanup_complete
FROM systems s
JOIN allocations a ON a.id = s.allocation_id
JOIN LATERAL (
    SELECT id, state, cleanup_complete FROM external_boot_activations
    WHERE system_id = s.id
    ORDER BY created_at DESC, id DESC
    LIMIT 1
) e ON true
WHERE s.state = 'tearing_down';
```

Then read its teardown job:

```sql
SELECT id, state, attempt, error_category,
       payload ? 'external_boot_authority_v1' AS authority_marked
FROM jobs
WHERE dedup_key = '<system-id>:teardown';
```

An unmarked job in `failed` with error category `conflict` is the ordinary teardown that the
worker refused.

## Why no tool ends it

Only two writers move a System from `tearing_down` to `torn_down`, and neither admits this one:

- **The ordinary teardown worker.** It refuses any System with an activation row as a terminal
  `conflict` (`external_boot_teardown_not_supported`) before any provider call, because the
  provider-host authority owns the host domain.
- **The authority teardown.** Migration 0147's authority allocator and teardown receipt admit
  purpose `teardown` only for a System in `provisioning`, `ready`, `reprovisioning`,
  `restoring`, `paused`, `crashing`, `crashed`, or `failed`. `tearing_down` is not in that list.

Each operator tool therefore refuses or makes no progress:

| Tool | Result |
|---|---|
| `systems.teardown` | Replaces the refused ordinary job with an authority-marked teardown. The allocator answers `superseded`, so each attempt fails with `stale_handle` and the System state does not change. |
| `ops.force_teardown` | Takes the same route as `systems.teardown`, with the same result. |
| `allocations.release`, `ops.force_release`, `resources.drain` | Refused with `conflict`, reason `external_boot_system_teardown_required`, while the System is not `torn_down`. |
| `investigations.close` | Refused with `bound_systems_live`, because the System is not terminal. |
| `investigations.close force=true` | Refused with `conflict`, reason `external_boot_system_teardown_required`, before any write. |

Do not write `systems.state` directly, in either direction. A direct update to `torn_down`
skips the authority teardown that destroys the host domain, so the domain keeps running with no
System to account for it, and it lets `allocations.release` succeed. A direct update to any
other state uses a transition the System state machine does not have, and the effect of an
authority teardown from that state has not been verified.

## What lease expiry ends

The reconciler's lease-expiry sweep moves the Allocation to `expired` once `lease_expiry`
passes. The sweep refuses only while an activation still restricts the System. A pre-fix
teardown ran only after the activation stopped restricting it (a cleaned `recovered` or
`abandoned` activation), so for this residue the sweep proceeds. This frees the Allocation's
host capacity.

Lease expiry does not end anything else:

- The System stays in `tearing_down`. The reconciler keeps it in its stalled-teardown candidate
  set and enqueues nothing for it.
- The authority teardown cannot run after expiry either, because the authority allocator also
  requires an `active` Allocation (#2992).
- The host domain that the provider-host authority owns is not destroyed by any kdive path.
- A bound Investigation stays `open`, because both forms of `investigations.close` refuse it.

## Escalation

Record the System, Allocation, Investigation, and activation ids from the queries above, and
report them on #3026. Ending such a System requires a change to kdive: either the schema exit
that #3026 describes, in which the 0147 allocator and receipt also admit `tearing_down` for
purpose `teardown`, or a reviewed operator procedure that has been proven on a lab host. Until
then, the System and its bound Investigation remain as they are, and the Allocation ends at
lease expiry.
