# 0827 — Hot-plugged scratch disks are charged to a per-project storage quota derived from the device ledger

## Status

Proposed

Implementation is tracked by #3720. Nothing in this record is implemented yet.

## Context

ADR-0823 lets a contributor hot-plug up to 8 scratch disks of up to 32 GB into a `READY` System
(`DEVICE_LIMIT`, `DISK_MAX_GB`, `src/kdive/mcp/tools/lifecycle/systems/devices.py:52-54`). The
backing storage is a local qcow2 file or a remote storage volume. ADR-0823 left it uncharged and
told a deployment that needs a limit to file it; #3720 is that issue. The per-System bound (256 GB)
is the only limit today, so a project with many Systems has no ceiling on hot-plugged storage.

The issue frames the work as joining "the existing disk quota/budget dimensions". Checked against
`origin/main` at 76834c6b0, no such dimension exists:

- `Quota` holds three concurrency caps: `max_concurrent_allocations`, `max_concurrent_systems`,
  `max_pending_allocations` (`src/kdive/domain/accounting/records.py:42-48`). None is a size.
- `Budget` is a kcu running total. The kcu rate is `coeff × (W_CPU × vcpus + W_MEM × memory_gb)`
  (`src/kdive/domain/accounting/cost.py:128-136`); disk is not an input. ADR-0312 records disk as
  "priced-past".
- `validate_disk_against_resource` (`cost.py:225-250`) compares one System's `disk_gb` with the
  host's advertised ceiling at admission and keeps nothing afterwards. The System's own disk is
  therefore bounded per request but not accumulated or charged per project.

So the choice is not between two existing dimensions. A project-level storage limit does not exist
for any disk, and this record adds the first one, scoped to hot-plugged scratch disks.

Facts the design rests on:

- `system_devices` carries `kind`, `state` and `size_gb` per row (migration `0200`). A row exists
  from admission until a successful un-plug deletes it, and ADR-0823's invariant is that a row
  exists exactly while kdive may have a device attached or backing storage to free.
- Plug admission runs under the System advisory lock and writes the `plugging` row and enqueues the
  job in one transaction (`devices.py:215-267`, `270-295`).
- Rows are deleted at five places: the un-plug handler (`jobs/handlers/devices.py`, `_drive_unplug`),
  a plug the port rejects with `configuration_error` and no handle (`_delete_row` in the plug
  handler), teardown (`_reclaim_devices`, `jobs/handlers/systems.py:712-723`), reprovision success
  (`jobs/handlers/systems.py:264`), and system-reclaim (`jobs/handlers/system_reclaim.py:113`).
- The lock total order is `PROJECT → RESOURCE → ALLOCATION → SYSTEM` (`db/locks.py:36-43`,
  ADR-0040). Provisioning already counts a project's Systems under the PROJECT lock
  (`services/systems/admission.py:491-514`, `747-756`).
- The refusal precedent for a project cap is `ErrorCategory.QUOTA_EXCEEDED`, with no row written
  and the request left to retry after the operator or the project frees capacity
  (`services/systems/admission.py:750-756`).

## Decision

**A new, separate dimension: `quotas.max_hotplug_disk_gb`.** A project's quota row gains one
integer column, the most gigabytes of hot-plugged scratch disk its Systems may hold at once. It is
`NOT NULL DEFAULT 0 CHECK (>= 0)`. Zero refuses every hot-plugged disk, so the limit is fail-closed
like the other caps (ADR-0007 §4: no silent default), and a project with no quota row has a limit
of 0. `Quota` gains `max_hotplug_disk_gb: int = 0`. `accounting.set_quota` gains an optional
`max_hotplug_disk_gb`; an omitted value keeps the stored one (0 for a new row), so an operator who
re-sets the concurrency caps does not zero it. It is admin-only like the rest of `set_quota`, and
its audit record carries the value.

**The charge is derived from the ledger, not stored.** The charge of a project is

```sql
SELECT coalesce(sum(d.size_gb), 0)
FROM system_devices d JOIN systems s ON s.id = d.system_id
WHERE s.project = %s AND d.kind = 'disk'
```

over every state: a row in any state may have backing storage, and `lost` and `failed` rows keep it
until an un-plug or teardown frees it. NICs have no size and are not charged. There is no counter
column and no second table, so a charge cannot drift from the devices the ledger lists.

**The charge is held from admission.** `_admit_plug` inserts the `plugging` row and the charge
exists from that commit. Admission refuses with `QUOTA_EXCEEDED` when
`charged + requested size_gb > max_hotplug_disk_gb`. The check runs after the existing refusals
(external-boot, job in flight, device limit) and before the insert. Plug admission takes the
project advisory lock before the System lock (`PROJECT → SYSTEM`, the order ADR-0040 fixes), so two
plugs on different Systems of one project cannot both pass the check against the same charge.

**The refusal.** `ToolResponse.failure` with `ErrorCategory.QUOTA_EXCEEDED`, `data.reason:
hotplug_disk_quota_exceeded`, and the integers `limit_gb`, `charged_gb`, `requested_gb`.
`suggested_next_actions` is `["systems.unplug_device", "systems.list_devices"]`. No row and no job
are written, and, like every refusal, it is not recorded for idempotent replay
(`mcp/tools/lifecycle/support/_idempotency.py:158-159`).

**Release is deleting the row.** Each of the five delete points above already runs in the same
transaction as, or after, the provider call that frees the storage, and it lowers the charge the
moment it commits. This record adds no release code. A row that stays charges: a `failed` plug, a
refused un-plug, a `lost` row after power OFF/CYCLE, restore or a failed reprovision, and a row the
reconciler failed. The agent clears each with `systems.unplug_device`, which frees any backing
volume and deletes the row. The spec lists every path.

**Limits lowered below the charge.** Setting `max_hotplug_disk_gb` below the current charge removes
nothing. It refuses new plugs until un-plugs, teardown or reclaim bring the charge under it.

Exact signatures, the query's placement, the migration contract, lock handling, the failure model
and the proposed PR split are in the spec,
`docs/workflow/specs/2026-11-19-hotplug-disk-quota-3720-design.md`.

## Consequences

- A project that used `systems.plug_device kind=disk` before this lands is refused until an admin
  sets `max_hotplug_disk_gb`. The feature is days old and unreleased, and a zero default is the
  fail-closed reading of ADR-0007 §4; setup scripts, the lab accounting helper and
  `docs/operating/project-onboarding.md` must set it, in the same implementation series.
- `Quota` stops meaning "concurrency caps" only. ADR-0007 §4 and ADR-0069 keep their decisions;
  this record extends the table with a storage cap.
- Plug admission now takes the PROJECT lock, so plugs in one project serialize with each other and
  with provisioning for the short admission transaction. Un-plug, teardown and reclaim take no
  project lock: they only lower the charge, and a reader that misses an uncommitted delete refuses
  a plug that would have fit, never admits one that does not.
- A `failed` or `lost` row charges storage that may already be gone (a failed reprovision ran
  `release_all`). The over-charge ends at un-plug or teardown and errs toward refusing.
- The charge is a count of provisioned gigabytes, not a time integral. It adds nothing to the kcu
  budget, and a hot-plugged disk is still free of kcu.
- The host's own disk is not part of the check. A project under its limit can still exceed the
  host's free space; per-host disk capacity is the System-disk ceiling's concern (ADR-0312).
- The tool's `size_gb` bound (1 to 32) and `DEVICE_LIMIT` stay.
- Because the charge reads the ledger, ADR-0823's accepted gaps carry over: a worker that dies
  between the provider call and the commit, or a plug that races teardown, can leave a volume the
  ledger does not show, and that volume is not charged.

## Considered & rejected

- **Join the kcu budget.** judgment: disk is not a kcu input, and ADR-0312 left it unpriced.
  Pricing hot-plugged disk alone would invent a disk rate and coefficient; pricing it consistently
  would change the cost of every System. A budget is spent over time and reconciled to an
  allocation (`LedgerEntry.allocation_id` is required, `records.py:58`, `0002_accounting.sql:48`); a plugged disk belongs
  to a System and its charge ends at an un-plug. Pricing storage is a separate decision with no
  owner here.
- **Reuse `max_concurrent_systems`, or fold the limit into the per-System cap.** judgment: a count
  of Systems is not a size, and the per-System cap (8 × 32 GB) is the bound the issue calls
  insufficient.
- **A stored counter (`quotas.hotplug_disk_charged_gb`) or a charge table.** verified: five code
  paths delete rows, plus the reconciler and the foreign-key cascade; a counter needs every one to
  decrement in the same transaction and a repair for the ones that do not, and a missed path
  silently lowers the project's capacity for good. The sum over a table of at most 8 rows per
  System, read under the project lock, costs one indexed join (`system_devices_system_id_idx`,
  `systems.project`) and cannot drift.
- **Charge at settle (when the provider reports `attached`).** judgment: a plug in flight on each
  of N Systems of one project would each pass a check against a charge that includes none of them,
  and overshoot by up to N × 32 GB. The in-flight row is already the durable marker, so charging
  from admission costs nothing extra.
- **A NULL limit meaning unlimited.** judgment: it keeps the shipped behaviour on upgrade but is the
  silent default ADR-0007 §4 refuses, and it makes "no limit configured" and "limit raised"
  indistinguishable in audit. Recorded as the alternative for operator review in the PR; the
  implementation PRs follow the operator's choice.
- **A global or per-provider limit in configuration.** judgment: the issue asks for a project-level
  limit, quotas are per project and admin-set at runtime, and a deployment-wide knob does not let
  two projects differ.
- **Release at a time-based expiry or by reconciler sweep.** judgment: the rows already have
  owners that delete them; a sweep would second-guess the ledger and could free a charge while a
  volume still exists.
- **Charge NICs.** judgment: a NIC has no size and no provider storage.
