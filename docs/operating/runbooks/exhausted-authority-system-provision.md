# Exhausted authority System provision

This runbook covers an authority-owned System that never reached its first activation and whose
`provision` job lost its worker on the final attempt before the provider authority wrote a
terminal receipt. No retry or repair path exists for that job: only the authority may
terminalize it, and the job has no attempts left. The supported exit is an explicit teardown
whose absence receipt discharges the job
([ADR-0754](../../adr/0754-receipt-bound-authority-system-discharge.md), #3143).

## Symptom

- `allocations.release` answers `conflict` with reason
  `authority_system_preactivation_mutation_fenced` and next actions `systems.get` and
  `systems.teardown`.
- `systems.get` shows the System in `provisioning`.
- The System's `provision` job stays `running` with `attempt` equal to `max_attempts` and a
  `lease_expires_at` in the past.

## Confirm the state

With authorized read-only database access:

```sql
SELECT id, state, attempt, max_attempts, lease_expires_at, created_at
FROM jobs
WHERE kind = 'provision'
  AND payload #>> '{authority_system_v1,system_id}' = '<system-id>';
```

## Recover

1. Run `systems.teardown <system-id>`, which needs the project `admin` role. It queues the
   authority-owned preactivation teardown. That teardown supersedes the lost provision attempt
   and asks the provider authority to prove the System's domain and storage absent.
2. Wait for the teardown job with `jobs.wait`, then confirm with `systems.get` that the System
   is `torn_down`. The System reaches `torn_down` only when the authority's
   preactivation-absence receipt is consumed.
3. Release the Allocation with `allocations.release`. A consumed absence receipt admits release
   even though the System never activated; no other operation is admitted by it.

The reconciler then cancels the exhausted `provision` job. The canceled job's `result_ref`
holds the absence receipt digest. The job keeps its attempt count and receives no new attempt.

## Limits

- **Authority unavailable.** The teardown job waits for the provider authority. Restore the
  authority service and let the teardown retry. Do not edit job or System rows by hand.
- **Retained quarantine.** If the authority cannot prove absence, it retains the System in
  quarantine and the teardown retries. Release stays refused until absence is proven.
- **Another System on the Allocation.** Release checks every System the Allocation ever held.
  An absence receipt for one System does not excuse another that is still restricted.
- **No manual state writes.** Writing `systems.state` or the job row directly skips the
  authority's absence proof and can let `allocations.release` free a host whose domain still
  runs.
