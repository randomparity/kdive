# Provider device hot-plug and un-plug (#3685)

Decision: [ADR-0823](../../adr/0823-provider-device-hotplug-nic-and-scratch-disk.md). Epic:
#1380. Build: #3686 (port, tools, fault-inject), #3687 (libvirt), #3688 (PowerVM).

## Problem

An agent testing a driver needs to add and remove a NIC or a disk on a running System. No such
operation exists, and the two provider families differ in what they can attach and in how
un-plug completes. The ADR fixes the device class, the capability and the persistence; this spec
holds the contract #3686 builds against.

## Design

### Capability and port

`ProviderSupport.hotplug_devices: frozenset[HotplugDeviceKind]` (empty default) and
`ProviderRuntime.device_hotplug: DeviceHotplugger | None`; signatures are in the ADR. Both live
in `providers/core/runtime.py` and `providers/ports/lifecycle.py`; `HotplugDeviceKind`,
`DeviceSpec` and `PlugOutcome` go beside `Snapshotter`. `__post_init__` adds
`_require_capability_port_parity(advertised=bool(support.hotplug_devices), port=device_hotplug,
...)`. The port raises `CONFIGURATION_ERROR` for a kind outside the advertised set. The worker
runs the synchronous port in a thread, as it does for `Snapshotter`.

### Tools

All three resolve the runtime with `with_runtime_for_system`, validate the id, then refuse a
provider with an empty `hotplug_devices`, or one that lacks the requested kind, with
`capability_unsupported(capability="hotplug:<kind>", supported=<sorted kinds>)`.

`systems.plug_device(system_id, kind, network=None, size_gb=None, idempotency_key=None)`,
contributor, job. `kind` is `nic` or `disk`. `network` applies to `nic` only, 1 to 64 characters
of `[A-Za-z0-9._-]`; `None` selects the provider default. `size_gb` is required for `disk`, an
integer 1 to 32 (GB, one device). A parameter for the wrong kind, a missing `size_gb`, or a value
out of range is `configuration_error` with `data.reason` `invalid_kind`, `invalid_network`,
`invalid_size` or `parameter_mismatch`. On admission, in one transaction under the per-System
advisory lock: insert the row as `plugging`, enqueue `PLUG_DEVICE` (payload `system_id`,
`device_id`, dedup key `plug:<device_id>`). Response: status `running`,
`data={job_id, device_id, kind, state: "plugging", settle_timeout_s: 60}`,
`suggested_next_actions=["jobs.get", "systems.list_devices"]`.

`systems.unplug_device(system_id, device_id, idempotency_key=None)`, contributor, job. Admitted
for `attached`, `failed` and `lost` rows. Sets the row `unplugging` and enqueues `UNPLUG_DEVICE`
in one transaction. Response: status `running`,
`data={job_id, device_id, state: "unplugging", settle_timeout_s: 30}`. A `device_id` that is not
a row of this System is `not_found` with the fixed detail (ADR-0123).

`systems.list_devices(system_id)`, viewer, database read. `data={devices, device_count,
device_limit: 8, server_time}`. Each device: `device_id`, `kind`, `state`, `network`, `size_gb`,
`identity` (`mac` for a nic, `serial` for a disk; empty until attached), `detail`
(redacted provider text, set on `failed`), `created_at`, `updated_at`.

Admission refusals shared by plug and un-plug, all `conflict` with `data.reason`: `system_not_ready`
(state is not `READY`), `device_op_in_flight` (a plug or un-plug job is queued or running on the
System, or the target row is `plugging`/`unplugging`), and, for plug only, `device_limit` (8 rows
already). Role denial is `authorization_denied`. Both mutating tools are audited with
`transition` `plug_device` / `unplug_device`.

The wrapper docstrings must carry the limits below. Both job kinds join `ACTIVE_JOB_KINDS` and
`STATE_FENCED_JOB_KINDS`, are not in `CONTRIBUTOR_CANCELABLE_JOB_KINDS`, and register handlers in
`jobs/assembly.py`.

### Limit contract

| Limit | Unit | Clock | Scope | On violation | Recovery |
|---|---|---|---|---|---|
| Settle window: 60 s plug, 30 s un-plug | seconds | worker monotonic clock, from the start of the provider call (queue wait excluded) | one job; a retry starts a new window | job fails `control_failure`, `reason: settle_timeout`, `data.waited_s`; row becomes `failed`; the device may still appear or vanish later | `systems.list_devices`, then `systems.unplug_device` (idempotent) or plug again |
| 8 devices | rows, any state | none | per System | `conflict`, `reason: device_limit`, `data.limit: 8` | un-plug a `failed`, `lost` or `attached` row |
| Disk 1 to 32 GB | gigabytes | none | per device | `configuration_error`, `invalid_size` | request a size in range |

### Ledger

Migration (number assigned by #3686): `system_devices(id uuid primary key, system_id uuid not null
references systems(id) on delete cascade, kind text not null, state text not null, network text,
size_gb integer, provider_handle text, identity jsonb not null default '{}', detail text,
created_at timestamptz not null, updated_at timestamptz not null)` with CHECKs on `kind` and
`state`, and an index on `system_id`. It also widens `jobs_kind_check` with `plug_device` and
`unplug_device`. `SystemDeviceState` joins the guarded-transition table in
`domain/capacity/state.py`:

| From | To | Cause |
|---|---|---|
| `plugging` | `attached` | provider settled |
| `plugging` | `failed` | provider error, `settle_timeout`, or reconciler |
| `attached` | `unplugging` | `unplug_device` admitted |
| `attached` | `lost` | power OFF/CYCLE or snapshot restore succeeded |
| `unplugging` | `attached` | `unplug_refused` |
| `unplugging` | `failed` | `settle_timeout`, provider fault, or reconciler |
| `failed`, `lost` | `unplugging` | `unplug_device` admitted |
| `unplugging` | row deleted | provider settled |

The worker commits `provider_handle` and `identity` as soon as `plug` returns, before it polls.
`repair_stalled_device_ops` (reconciler) moves `plugging`/`unplugging` rows that have no active
job of the matching kind to `failed`, without provider I/O. The power worker and the restore
worker move the System's `attached` rows to `lost` in the transaction that commits their result.
Provider text in `detail` passes the redactor.

### Errors from the port

`CONTROL_FAILURE` + `unplug_refused` (guest or HMC refused; also `handle_unknown`),
`CONTROL_FAILURE` + `settle_timeout` (worker deadline), `INFRASTRUCTURE_FAILURE` (provider fault,
domain absent), `CONFIGURATION_ERROR` + `unknown_network`. No new `ErrorCategory` value.

### Providers

- fault-inject: an in-memory device table per domain; the resource capability key
  `hotplug_behaviour` (`busy`, `slow`, `hang`, ADR-0072 style) makes un-plug refuse, settle after
  two polls, or never settle; the existing per-plane `fault_rate` injects
  `INFRASTRUCTURE_FAILURE`. An unknown `device_id` and a non-`READY` System need no provider hook.
- libvirt (#3687): `attachDeviceFlags` / `detachDeviceFlags` with the live flag only; the device
  alias carries `device_id`; detach is polled through `is_attached` since libvirt returns before
  the guest answers. remote-libvirt resolves the allocated host through `binding`.
- PowerVM (#3688): DLPAR add and remove through the HMC driver of #1822; an active RMC connection
  is required and its absence is `unplug_refused` / a plug `CONTROL_FAILURE`; the slot number or
  adapter id is the `provider_handle`. Complete only after a recorded ppc64le run.

## Failure model

1. Actors and deployments: an MCP client holding contributor on a `READY` System; fault-inject,
   local-libvirt, remote-libvirt and PowerVM runtimes; x86_64 and ppc64le; one worker per job.
2. Invariants: a ledger row exists exactly while kdive may have a device attached or backing
   storage to free; provider calls on one domain never overlap; the boot disk and SSH NIC are
   never listed or removed; a provider without the kind refuses before enqueue.
3. Accepted: a worker that dies between the provider call and the commit leaves a `failed` row
   with no handle (cleared by `unplug_device` when the provider can find the device by
   `device_id`); a snapshot restore can bring back an un-plugged device with no row; devices are
   not kept across power cycles; disk space is not charged to a quota; a guest that ignores an
   eject shows as `settle_timeout`, not as a distinct state.
4. Covered elsewhere: forced un-plug, pre-existing volumes, PCIe and SR-IOV, vFC and CPU/memory
   hot-plug are unowned deferrals (ADR-0823); PowerVM transport is #1822 and #1818; real-POWER
   proof is the `verification:ppc64le` gate on #3688.

## Impact on the child issues

#3686 persists device state (the ADR says so), so its step 4 is required and its acceptance
criteria need the ledger, migration and repair; #3686 lists the "unknown device" error as a
category, which is `not_found`; "wrong System state" is `conflict`. #3687 uses the live flag only
and no persistent flag. #3688's "refusal while the LPAR is in a state that disallows DLPAR" is
the `unplug_refused` / plug `control_failure` path.
