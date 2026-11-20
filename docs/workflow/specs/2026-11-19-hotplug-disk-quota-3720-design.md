# Charge hot-plugged scratch disks to a project storage quota (#3720)

Decision: [ADR-0827](../../adr/0827-charge-hot-plugged-scratch-disks-to-a-project-storage-quota.md).
Builds on [ADR-0823](../../adr/0823-provider-device-hotplug-nic-and-scratch-disk.md) (the device
ledger) and ADR-0007 §4 (quota admission). Epic: #1380.

## Problem

`systems.plug_device kind=disk` provisions up to 32 GB of provider storage per device and 8 devices
per System with no project-level limit. The only bound is per System (256 GB), so a project with N
Systems can hold 256 × N GB. The ledger already records `size_gb` for every device.

## Design

### The limit

Migration (next free number when implemented; forward-only, ADR-0015):

```sql
ALTER TABLE quotas
    ADD COLUMN max_hotplug_disk_gb integer NOT NULL DEFAULT 0
        CONSTRAINT quotas_max_hotplug_disk_gb_check CHECK (max_hotplug_disk_gb >= 0);
```

Existing rows backfill to 0, as `max_pending_allocations` did (`0016_pending_queue.sql`). No grant
change: `kdive_server` already holds SELECT, INSERT and UPDATE on `quotas`
(`0107_process_role_data_access.sql`). `Quota` (`domain/accounting/records.py:42`) gains
`max_hotplug_disk_gb: int = 0`. The four `migrations[-K:]` window tests need the new row and `K+1`.

`accounting.set_quota` (`mcp/tools/accounting/admin.py:79`; `QuotaSetRequest` at `:35`) gains
`max_hotplug_disk_gb: int | None = None`, admin only, rejected below 0 with the existing
`CONFIGURATION_ERROR` path. `None` keeps the stored value, or 0 when the project has no quota row.
`QUOTAS.upsert` writes every non-key column (`db/repositories.py:643-650`), so the tool reads the
current row inside its transaction and passes the kept value; the other three caps keep their
present semantics. The success `data` and the audit `args` gain `max_hotplug_disk_gb`. The CLI verb
(`cli/commands/_generated_verbs.py`) and the tool index are generated from the tool, and the
reference docs gain the parameter.

`kdive seed-project` is unchanged: its INSERT names two columns, so a seeded project gets 0 and an
operator raises it with `accounting.set_quota`. The setup scripts and the lab accounting helper
(`scripts/operations/kdive_set_accounting.py`, which calls `set_quota`) pass it, so a lab project
can plug a disk.

### The charge

One function in a new `services/systems/hotplug_quota.py`:

```python
async def hotplug_disk_usage(conn: AsyncConnection, project: str) -> tuple[int, int]:
    """Return (limit_gb, charged_gb); limit_gb is 0 when the project has no quota row."""
```

It runs two statements: `SELECT max_hotplug_disk_gb FROM quotas WHERE project = %s`, and the sum
in the ADR over `system_devices JOIN systems` with `kind = 'disk'`, any state. `size_gb` is non-null
for a disk row (the tool rejects a disk without one, `devices.py:104-127`); `coalesce` covers an
empty set. The caller holds the PROJECT lock. `kdive_server` can read all three tables
(`0107`, `0201`).

### Admission

`_keyed_admission` (`devices.py:270-295`) is shared by plug and un-plug. It takes the SYSTEM lock
after an unlocked `SYSTEMS.get`. For plug it also takes the PROJECT lock first, using the project
from that read (a System's project never changes):

```text
conn.transaction()
  -> advisory_xact_lock(PROJECT, system.project)   # plug only
  -> advisory_xact_lock(SYSTEM, uid)
  -> keyed_mutation(... admit ...)
```

This is the `PROJECT → SYSTEM` order of ADR-0040 and `systems.provision` (`admission.py:547`).
`_admit_plug` re-reads the System under both locks, as it does today (`_ready_system`). `_plug_refusal`
(`devices.py:196-212`) gains a last step, after external-boot, in-flight and `device_limit`, and
only for `kind=disk`:

```python
limit_gb, charged_gb = await hotplug_disk_usage(conn, system.project)
if charged_gb + size_gb > limit_gb:
    return quota_refusal(system_id, limit_gb, charged_gb, size_gb)
```

`_plug_refusal` takes `kind` and `size_gb` for this. A NIC skips the step. The refusal:

| Field | Value |
|---|---|
| `error_category` | `quota_exceeded` |
| `data.reason` | `hotplug_disk_quota_exceeded` |
| `data.limit_gb`, `data.charged_gb`, `data.requested_gb` | integers; `limit_gb` is 0 with no quota row or a 0 limit |
| `suggested_next_actions` | `systems.unplug_device`, `systems.list_devices` |
| Side effects | none: no row, no job, no audit row, not recorded for replay |

The detail text names the three figures and tells the caller to un-plug a device or ask a project
admin to raise `max_hotplug_disk_gb`. `quota_exceeded` is already in the category table
(`mcp/resources/_content/errors.md`); `ErrorCategory.QUOTA_EXCEEDED` is non-retryable by a bare
retry (`domain/errors.py:133`), which fits: a retry succeeds only after something changes. The
envelope's other fields follow `_conflict`'s shape (`devices.py:60-67`).

The tool docs for `systems.plug_device` list the new refusal; the error reference lists
`hotplug_disk_quota_exceeded` under `quota_exceeded`.

### Release

The charge is the ledger, so it ends when the row does. Every path, with its charge effect:

| Event | Row | Charge |
|---|---|---|
| `unplug_device` settles | deleted (`_drive_unplug`) | released in that commit |
| plug rejected `configuration_error`, no handle | deleted (`_delete_row`) | released |
| plug fails with a handle, `settle_timeout`, or unexpected error | `failed` | held; `unplug_device` frees it |
| `unplug_device` refused or system not ready | returns to `from_state` | held |
| teardown | `release_all` best effort, then rows deleted (`_reclaim_devices`, `jobs/handlers/systems.py:712-723`) | released, also when `release_all` faulted |
| reprovision succeeds | rows deleted (`jobs/handlers/systems.py:264`) | released |
| reprovision fails after `release_all` | `attached` rows to `lost` | held until `unplug_device` or teardown |
| power OFF/CYCLE, snapshot restore succeeds | `attached` rows to `lost` | held: the backing volume stays on the host |
| system-reclaim proves absence | rows deleted (`jobs/handlers/system_reclaim.py:113`) | released |
| reconciler `repair_stalled_device_ops` | `plugging`/`unplugging` to `failed` | held |
| System row deleted | cascade | released |

No release code is added. The implementation PR adds tests that assert the charge after each row
event, so a future change to a delete path cannot leave it behind.

### Reads and limits lowered

A refusal reports the three figures, and `accounting.set_quota` echoes the limit. There is no read
tool for the charge, and none is added here (non-goals). Lowering the limit below the charge
leaves devices in place, refuses plugs until the charge is under it, and never un-plugs anything.

## Failure model

1. Actors and deployments: contributors plugging on READY Systems of one project, an admin setting
   the limit, workers and the reconciler deleting rows; one PostgreSQL; every provider that
   advertises `hotplug_devices`.
2. Invariants: while the PROJECT lock is held, `charged_gb` counts exactly the disk rows
   committed; a plug that commits has passed `charged + size <= limit` against that count; a row's
   charge ends only when the row is deleted, and a row is deleted only after the provider freed the
   storage or proved it absent (ADR-0823), except teardown, which deletes after a best-effort
   release.
3. Accepted: a `failed` or `lost` row can charge storage already freed (until un-plug or teardown);
   teardown deletes rows when `release_all` faulted, so a leaked volume stops charging while it
   still exists (ADR-0823 accepts the leak); a volume the ledger never recorded is not charged; two
   projects cannot share a limit; an admin can set a limit below the current charge; a deleted
   quota row refuses new plugs and leaves devices.
4. Concurrency: two plugs on different Systems of one project serialize on the PROJECT lock, so the
   second sees the first's `plugging` row. A delete racing a check can only lower the count the
   check reads. A plug and a provision of the same project serialize on the same lock for the
   admission transaction only; no provider call runs under it. The lock order is the documented
   total order; un-plug takes only the SYSTEM lock, as now.
5. Covered elsewhere: per-host free disk is ADR-0312's ceiling; the reconciler repair and the
   delete paths are ADR-0823's; idempotent replay of a successful plug returns the recorded
   envelope without re-checking the quota, and a refusal is never recorded.

## Non-goals

- Pricing hot-plugged or System disk in kcu, or any time-integrated storage charge. No owner; a
  separate decision if operators want storage in the budget.
- Charging the System's own disk, or checking hot-plugged disks against the host's free space. No
  owner.
- A read surface for the charge (`accounting.usage`, `list_devices` totals). Owner: none; file it
  when an operator asks.
- A per-System or per-provider limit, a global default, or limit inheritance across projects.
- Un-plugging devices when a limit is lowered, and charging NICs.
- PowerVM: the contract needs only that `DeviceSpec.size_gb` and the ledger row carry the size,
  which they do. Owner: #3688.

## Implementation split

Order A, then B; C may land with or after B. No PR needs a live provider run: the charge is a
database rule and fault-inject (which advertises both kinds) exercises the plug path in the
existing live cells.

| # | Title | Scope | Effort | Live proof |
|---|---|---|---|---|
| A | `feat(accounting): add max_hotplug_disk_gb to project quotas` | migration, `Quota`, `set_quota` parameter with keep-on-omit, audit and echo, CLI and tool index regeneration, docs, `migrations[-K:]` window tests, ADR-0827 citations in `src/` and `tests/` once B is in the series | M | no |
| B | `feat(systems): refuse hot-plugged disks over the project storage quota` | `hotplug_disk_usage`, PROJECT lock in the plug path, `_plug_refusal` step, refusal envelope, tool and error docs, tests: boundary (limit, limit+1), no quota row, NIC unaffected, two Systems racing the last gigabytes under the lock, charge after every row event in the release table | M | no; one fault-inject plug cell to refusal if the coverage obligations require it |
| C | `chore(ops): set max_hotplug_disk_gb in the setup and lab accounting helpers` | `kdive_set_accounting.py`, setup scripts, `docs/operating/project-onboarding.md`, a published operator knob if a new `KDIVE_` variable is added (`env-docs-check`) | S | no |

Where #3720's text differs from this spec, this spec governs. A and B flip ADR-0827 to Accepted in
the PR that completes the decision (B), per the ADR process.
