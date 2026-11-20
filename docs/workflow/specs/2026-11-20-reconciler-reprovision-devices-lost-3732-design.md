# Reconciler reprovision repair marks attached devices lost (#3732)

Decision record: [ADR-0823](../../adr/0823-provider-device-hotplug-nic-and-scratch-disk.md) (device ledger), no
amendment. Follows #3714, which closed the same gap on the job-handler failure path.

## Problem

`reprovision_handler` frees every hot-plugged device's backing storage before it calls the
provider. A System stranded by a dead-lettered, canceled or absent job is moved to
`failed` by `repair_stalled_reprovisioning_systems`, but its `attached` `system_devices` rows are
left reading `attached`. The repair cannot tell whether the job died before or after that release,
so the device set is indeterminate and `lost` is the honest state.

## Scope

In: one call in the existing per-System transaction of the repair, plus tests. Out: the
stalled-`restoring` repair (a failed restore does not prove the device set was replaced) and the
job-handler path from #3714.

## Contract

Inside the transaction that holds the System advisory lock, immediately after
`SYSTEMS.update_state(... FAILED)`, the repair calls `mark_attached_devices_lost(conn, system_id)`.
`attached` rows become `lost`; rows in any other state are untouched. A failure of either write
rolls back both, because both run in the one transaction the existing `except` retries next pass.
A candidate the recheck defers writes nothing.

No schema change: `0201_system_devices_role_grants.sql` already grants `kdive_reconciler`
SELECT, UPDATE and DELETE on `system_devices`.

## Failure model

- Reconciler role lacks UPDATE: the statement raises, the transaction rolls back, the System
  stays `reprovisioning` and retries. The role-parametrised test proves the grant is present.
- Job appears under the lock: the recheck skips the System before any write.

## Tests

`tests/reconciler/test_stalled_reprovision_recovery.py`: a stranded System with an `attached` and
a `failed` row ends with the first `lost` and the second unchanged, under the superuser and the
`kdive_reconciler` role; a deferred System keeps its `attached` row; a forced failure after the device write (category
record) leaves the System `reprovisioning` and the row `attached`. Removing the call turns the
first test red.
