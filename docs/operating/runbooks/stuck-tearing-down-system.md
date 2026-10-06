# Stuck `tearing_down` System with external-boot history

This runbook covers a System that sits in `tearing_down` and has at least one external-boot
activation row. Such a System is residue from an ordinary teardown that ran before the worker
began refusing ordinary teardowns for any external-boot history
([ADR-0620](../../adr/0620-authority-owned-system-teardown.md), #2966). No new System reaches this
state. Only the authority teardown may finish it, because the provider-host authority owns the
host domain.

## Symptom

The reconciler logs one WARNING per System and cause:

```text
reconciler: system <system-id> is stuck in tearing_down with external-boot activation
<activation-id>; the ordinary teardown is refused (external_boot_teardown_not_supported), so no
job is requeued; run systems.teardown (<runbook>)

reconciler: system <system-id> is stuck in tearing_down behind authority-marked teardown job
<job-id> (<state>); this lane never replaces it, so re-run systems.teardown (<runbook>)

reconciler: system <system-id> is stuck in tearing_down behind authority-marked teardown job
<job-id> (<state>); no supported exit exists for it (<runbook>)
```

The reconciler does not log a System whose teardown job is `queued`, `running`, or `canceled`.
Use the queries below to find one.

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
SELECT id, state, attempt, max_attempts, lease_expires_at, error_category,
       payload ? 'external_boot_authority_v1' AS authority_marked,
       payload ? 'authority_system_v1' AS authority_system_marked
FROM jobs
WHERE dedup_key = '<system-id>:teardown';
```

Read the job row as follows:

- **Unmarked and `failed`, with error category `conflict`.** This is the ordinary teardown that
  the worker refused.
- **Authority-marked and `running`, with `attempt` below `max_attempts`.** It is waiting for its
  lease to lapse. The next claim retries it with no action from you.
- **Authority-marked and `running`, with `attempt` at `max_attempts` and a past
  `lease_expires_at`.** It is exhausted. Re-run the teardown as described below.

## Recover

The authority teardown runs whatever the Allocation state, including after the lease expired.

1. Run `systems.teardown <system-id>`, which needs the project `admin` role. A platform operator
   outside the project can use `kdivectl ops force-teardown` instead. It calls `ops.force_teardown`
   and needs `platform_admin`, `--force`, and a reason. Both tools take the same route:
   - a failed unmarked job is replaced by an authority-marked teardown on the same job row;
   - a failed or exhausted authority-marked job is requeued on the same row.
2. Wait for the job with `jobs.wait`, then confirm with `systems.get` that the System is
   `torn_down`. The authority teardown records the `tearing_down->torn_down` transition in the
   audit log, in the same transaction as the teardown receipt.
3. If the Allocation is still `active`, release it with `allocations.release`. Close any bound
   Investigation with `investigations.close`.

## Limits

- **No route.** `external_boot_teardown_authority_unresolved` means the System has no
  unambiguous authority route. `systems.teardown` refuses it, as it does for a System in any
  other state.
- **Authority-System marker.** A teardown job carrying `authority_system_v1` belongs to a System
  with an authority-System binding. If that System also has no activation, it has no supported
  exit from `tearing_down`.
- **No manual state writes.** Do not write `systems.state` directly in either direction. A
  direct write to `torn_down` skips the authority teardown that destroys the host domain and lets
  `allocations.release` succeed while that domain still runs. A direct write to any other state
  uses a transition the System state machine does not have.
