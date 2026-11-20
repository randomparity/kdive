# Charge hot-plugged scratch disks to a project storage cap (#3720)

Decision: [ADR-0827](../../adr/0827-charge-hot-plugged-scratch-disks-to-a-project-storage-cap.md).
Builds on [ADR-0823](../../adr/0823-provider-device-hotplug-nic-and-scratch-disk.md) (the device
ledger) and ADR-0520 (the private-image cap this follows). Epic: #1380.

## Problem

`systems.plug_device kind=disk` provisions up to 32 GB of provider storage per device and 8 devices
per System with no project-level limit. The only bound is per System (256 GB), so a project with N
Systems can hold 256 × N GB. The ledger already records `size_gb` for every device.

## Differences from #3720

The issue's text is the request; these points of the design differ from it, for the operator to
acknowledge when reviewing the PR.

- The issue assumes the storage can join "the existing disk quota/budget dimensions". None exists:
  `Quota` holds concurrency caps, the kcu budget prices vcpus and memory only, and the System's own
  disk is checked against a host ceiling per request and never accumulated (ADR-0827 Context). The
  design adds a cap and does not touch the budget.
- The issue's Expected section releases the charge on reprovision. It does on success. After a
  reprovision that fails, the System is not `READY`, its rows are `lost`, and the charge stays until
  teardown, because a row is the only record that storage may exist.
- The limit is a server setting shared by all projects, not a value an admin sets per project. The
  per-project column is the rejected alternative in ADR-0827.

## Design

### The cap

A new `Setting` in `config/core_settings.py`, beside `IMAGE_PRIVATE_MAX_BYTES`:

```python
HOTPLUG_DISK_PROJECT_MAX_GB = Setting(
    name="KDIVE_HOTPLUG_DISK_PROJECT_MAX_GB",
    parse=_nonnegative_int,
    default="256",
    group="devices",
    processes=_SERVER,
    help="Per-project cap in GB on the hot-plugged scratch disks its Systems hold at once. ...",
    suggest="an integer number of GB, e.g. 256",
)
```

`_nonnegative_int` rejects a negative value at startup. `0` refuses every
hot-plugged disk. The setting appears in the generated config reference
(`docs/guide/reference/config.md`; `just config-docs`, checked by `config-docs-check`) and gets an
operator note in `docs/operating/runbooks/live-testing.md`, where the hot-plug cells are described.
The worker and reconciler do not read it.

### The charge

One function in `db/repositories.py`, beside `devices_for_system` (the services layer does not
import from `kdive.mcp`, so it cannot take `DISK_MAX_GB` from `devices.py`, and needs no bound):

```python
async def hotplug_disk_gb_for_project(conn: AsyncConnection, project: str) -> int:
    """GB held by the project's hot-plugged disks, in every ledger state."""
```

It runs the sum in the ADR over `system_devices JOIN systems`, `kind = 'disk'`, any state. The tool
never stores a disk row without a size (`devices.py:104-127`). The caller holds the PROJECT lock. `kdive_server` reads both tables (`0201`, `0107`). There is no new index: the join
uses `system_devices_system_id_idx`, and the `systems.project` filter is the scan
`_within_system_quota` already runs (`services/systems/admission.py:491-514`).

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

This is the `PROJECT → SYSTEM` order of ADR-0040 and `systems.provision`
(`services/systems/admission.py:547`). `_admit_plug` re-reads the System under both locks, as it
does today (`_ready_system`). `_plug_refusal` (`devices.py:196-212`) gains `kind` and `size_gb`
parameters (`_admit_plug` has both) and a last step, after external-boot, in-flight and `device_limit`, for `kind=disk` only:

```python
cap_gb = config.require(HOTPLUG_DISK_PROJECT_MAX_GB)
charged_gb = await hotplug_disk_gb_for_project(conn, system.project)
if charged_gb + size_gb > cap_gb:
    return _quota_refusal(system_id, cap_gb, charged_gb, size_gb)
```

A NIC skips the step. The refusal:

| Field | Value |
|---|---|
| `error_category` | `quota_exceeded` |
| `data.reason` | `hotplug_disk_quota_exceeded` |
| `data.limit_gb`, `data.charged_gb`, `data.requested_gb` | integers |
| `suggested_next_actions` | `systems.unplug_device`, `systems.list_devices` |
| Side effects | none: no row, no job, no audit row, not recorded for replay |

The detail text names the three figures and tells the caller to un-plug a device, or ask the
operator to raise `KDIVE_HOTPLUG_DISK_PROJECT_MAX_GB`. `quota_exceeded` is already in the category
table (`docs/guide/errors.md`, snapshotted into `mcp/resources/_content/errors.md` by
`just resources-docs`) and is not retried by a bare retry
(`domain/errors.py:133`), which fits: a retry succeeds only after something changes. The
envelope's other fields follow `_conflict`'s shape (`devices.py:67-74`). The tool text of
`systems.plug_device` lists the new refusal (regenerated into `docs/guide/reference/systems.md` by
`just docs`, checked by `docs-check`), and the error reference lists `hotplug_disk_quota_exceeded`
under `quota_exceeded`.

### Release

The charge is the ledger, so it ends when the row does. Every row delete and state change, with its
charge effect:

| Event | Row | Charge |
|---|---|---|
| `unplug_device` settles | deleted (`_drive_unplug`) | released in that commit |
| plug rejected `configuration_error`, no handle | deleted (`_delete_row`) | released |
| plug fails with a handle, `settle_timeout`, or unexpected error | `failed` | held; `unplug_device` frees it |
| `unplug_device` refused or system not ready | returns to `from_state` | held |
| teardown | `release_all` best effort, then rows deleted (`_reclaim_devices`, `jobs/handlers/systems.py:712-723`) | released, also when `release_all` faulted |
| reprovision succeeds | rows deleted (`jobs/handlers/systems.py:264`) | released |
| reprovision fails after `release_all` | `attached` rows to `lost`; the System is `FAILED` | held until teardown (un-plug needs `READY`) |
| power OFF/CYCLE, snapshot restore succeeds | `attached` rows to `lost`; the System stays `READY` | held until `unplug_device` or teardown; the backing volume stays on the host |
| system-reclaim proves absence | rows deleted (`jobs/handlers/system_reclaim.py:113`) | released |
| un-plug settle timeout or provider fault | `unplugging` to `failed` | held |
| plug re-verify finds the System not `READY` | `plugging` to `failed` | held |
| reconciler `repair_stalled_device_ops` | `plugging`/`unplugging` to `failed` | held |
| reconciler fails a stalled restore or reprovision | rows stay `attached` on a `FAILED` System | held until teardown |
| System row deleted | cascade | released |

No release code is added. The implementation PR adds tests that assert the charge after each row
event, so a later change to a delete path cannot leave it behind.

### Reads and the cap lowered

A refusal reports the three figures. There is no read tool for the charge, and none is added
(non-goals). Restarting with a cap below the charge leaves devices in place and refuses plugs until
the charge is under it; nothing is un-plugged.

## Failure model

1. Actors and deployments: contributors plugging on READY Systems of one project; workers and the
   reconciler deleting rows; one PostgreSQL; every provider that advertises `hotplug_devices`; one
   server configuration.
2. Invariants: while the PROJECT lock is held, `charged_gb` counts exactly the disk rows
   committed; a plug that commits has passed `charged + size <= cap` against that count; a row's
   charge ends only when the row is deleted, and a row is deleted only after the provider freed the
   storage or proved it absent (ADR-0823), except teardown and reprovision success, which delete
   after a best-effort `release_all`.
3. Accepted: a `failed` or `lost` row can charge storage already freed (until un-plug or teardown);
   teardown and reprovision success delete rows when `release_all` faulted, so a leaked volume stops
   charging while it still exists (ADR-0823 accepts the leak); a volume the ledger never recorded is not charged; two
   projects cannot have different caps; a server restart can lower the cap below the current
   charge; servers with different settings behind one database apply their own cap; during a rolling upgrade
   servers on the old version admit plugs with no check, so the cap can be exceeded until all run
   the new one; a refusal is not audited, like the tool's other refusals, so an operator sees a
   pinned project only through the refusal's figures.
4. Concurrency: two plugs on different Systems of one project serialize on the PROJECT lock, so the
   second sees the first's `plugging` row (READ COMMITTED, the sum runs after the lock is held). A
   delete racing a check can only lower the count the check reads. A plug and a provision of the
   same project serialize on the same lock for the admission transaction only; no provider call
   runs under it. The lock order is the documented total order; un-plug takes only the SYSTEM lock,
   as now.
5. Covered elsewhere: per-host free disk is ADR-0312's ceiling; the reconciler repair and the delete
   paths are ADR-0823's; idempotent replay of a successful plug returns the recorded envelope
   without re-checking the cap, and a refusal is never recorded.

## Non-goals

Each is outside what #3720 asks and has no owner issue; none is filed here.

- A per-project cap or override (the ADR-0827 alternative), and an admin tool to set one. File it
  if an operator needs projects to differ.
- Pricing hot-plugged or System disk in kcu, or a time-integrated storage charge.
- Charging the System's own disk, or checking hot-plugged disks against the host's free space.
- A read surface for the charge (`accounting.usage`, `list_devices` totals). The refusal carries the
  figures; add a read when an operator asks.
- Un-plugging devices when the cap is lowered, and charging NICs.
- PowerVM: the contract needs only that `DeviceSpec.size_gb` and the ledger row carry the size,
  which they do. Owner: #3688.

## Implementation split

One issue, one PR. The change has no migration, no new tool parameter and no ordering with any
other PR, and ADR-0827 flips to Accepted in it.

| Title | Scope | Effort | Live proof |
|---|---|---|---|
| `feat(systems): refuse hot-plugged disks over the project storage cap` | the setting and its generated config reference (`just config-docs`), `hotplug_disk_gb_for_project`, the PROJECT lock in the plug path, the `_plug_refusal` step and refusal envelope, tool and error docs (`just docs`, `just resources-docs`), the `live-testing.md` note, `Accepted` status and ADR-0827 citations in `src/` and `tests/`; tests: boundary (cap, cap+1), cap 0, NIC unaffected, two Systems of one project racing for the last gigabytes under the lock, the charge after every row event in the release table | M | no: the rule is a database sum; the existing fault-inject and libvirt hot-plug cells keep passing under the 256 GB default and need no change |
