# 0823 — Providers advertise hot-plug of NICs and scratch disks; kdive records the devices it plugged in a System ledger

## Status

Accepted (2026-11-18)

Implementation is tracked by #3686 (port, tools, fault-inject), #3687 (libvirt) and #3688
(PowerVM through the HMC). #3686 and #3687 are implemented; #3688 is not, so the PowerVM provider
does not yet advertise hot-plug.

## Context

#1380 asks for a tool that hot-plugs "an adapter" on providers that support it, with IBM POWER
through the HMC as the motivating case. It does not say which device that is, what un-plug does to
a guest that is using the device, or whether kdive records what it attached. No hot-plug code
exists (`rg -n -i 'hot-?plug|attachDevice|detachDevice|dlpar' src` finds nothing at 872559b41).
Optional provider operations are advertised on `ProviderSupport` and wired as a `None`-able
`ProviderRuntime` port with a parity check in `__post_init__` (ADR-0208, ADR-0378, ADR-0385).

The two provider families expose different things. libvirt `attachDeviceFlags` and
`detachDeviceFlags` act on a running domain for NICs, disks and PCI host devices. Detach is a
request to the guest: the call can return before the guest has released the device, and a guest
may never release it. PowerVM dynamic LPAR adds and removes virtual Ethernet, virtual SCSI and
virtual Fibre Channel client adapters, dedicated PCIe slots and SR-IOV logical ports. It needs an
active RMC connection to the guest, and the HMC refuses a remove the guest still holds.

## Decision

**Device class.** The first release plugs two kinds, `nic` and `disk`.

- `nic`: a network interface. libvirt virtio NIC; PowerVM virtual Ethernet client adapter.
- `disk`: a new, empty scratch disk of a requested size that kdive creates and owns, in the format
  the provider's own snapshots need. libvirt virtio-blk qcow2 volume; PowerVM virtual SCSI client
  adapter mapped to a new volume, advertised only if #3688 proves it on a real LPAR.

These are the only two kinds both provider families attach with one dynamic call and a result the
guest can observe (`ip link`, `lsblk`). Deferred, each unowned until an issue names it:
pre-existing volumes as disks (needs a storage-reference model), dedicated PCIe slots and PCI
passthrough (ties into the PCIe allocation claim of ADR-0068 and into guest driver unbinding),
SR-IOV logical ports and VFs (capacity and ownership bookkeeping per physical port), and virtual
Fibre Channel (needs fabric zoning outside kdive). CPU and memory hot-plug stay excluded.

**Provider honesty.** The capability is a set of kinds, not one boolean, so a provider advertises
only what it can prove:

```python
class HotplugDeviceKind(StrEnum):
    NIC = "nic"
    DISK = "disk"

# ProviderSupport (fail closed: empty means unsupported)
hotplug_devices: frozenset[HotplugDeviceKind] = frozenset()
```

fault-inject advertises both. local-libvirt and remote-libvirt advertise the kinds #3687 proves
live. PowerVM advertises the kinds #3688 proves on a real LPAR. A provider that does not set it
refuses with the existing `capability_unsupported` envelope (`configuration_error`; `data` carries
`reason: capability_unsupported`, `capability: hotplug:<kind>`, `provider` and the sorted
`supported` kinds). `systems.get` adds
`data.hotplug_devices`, a sorted list, as it does for `supports_snapshots`.

**Port.** `ProviderRuntime.device_hotplug: DeviceHotplugger | None`, keyed on the provider domain
name like `Snapshotter`. `__post_init__` requires `bool(support.hotplug_devices)` to equal
`device_hotplug is not None`, and the port refuses a kind outside the advertised set.

```python
@dataclass(frozen=True, slots=True)
class DeviceSpec:
    kind: HotplugDeviceKind
    device_id: str            # kdive's ledger id; the provider embeds it in the device's name
    network: str | None       # nic only: provider network or virtual switch; None = default
    size_gb: int | None       # disk only

@dataclass(frozen=True, slots=True)
class PlugOutcome:
    provider_handle: str      # opaque; the only thing unplug and is_attached take
    identity: Mapping[str, str]   # guest-visible: {"mac": ...} for nic, {"serial": ...} for disk
    settled: bool             # False: the provider accepted the request and is still working

class DeviceHotplugger(Protocol):
    def plug(self, domain_name: str, spec: DeviceSpec) -> PlugOutcome: ...
    def unplug(self, domain_name: str, device_id: str, provider_handle: str | None) -> bool: ...
    def is_attached(self, domain_name: str, device_id: str, provider_handle: str | None) -> bool: ...
    def release_all(self, domain_name: str) -> None: ...  # teardown and reprovision, best effort
```

`plug` and `unplug` are idempotent: plugging an already-attached `device_id` returns its outcome,
and unplugging an absent device returns `True` after freeing any backing volume it still holds. The
handle is `None` only when a worker died between the provider call and the ledger commit; the
provider then finds the device by `device_id`, or raises `unplug_refused` with `data.reason:
handle_unknown` when it cannot. `unplug` returns `True` when the device is gone and `False` when the
provider accepted the request and has not finished; a retry that finds its ledger row gone is a
success. `release_all` removes every kdive-plugged device and backing volume of the domain, like
`Snapshotter.delete_all`. Every call carries its own timeout bounded by the job's remaining window.
`unplug` never forces the guest: the provider issues the graceful request only (libvirt without the
force flag, HMC remove without a forced mode) and, once the device is gone, frees the backing volume
of a `disk`. A provider refusal raises `CategorizedError` `CONTROL_FAILURE` with `data.reason:
unplug_refused`; a fault is `INFRASTRUCTURE_FAILURE`; an unknown network is `CONFIGURATION_ERROR`
with `unknown_network`.

**Tools.** Three `systems.*` tools. Plug and un-plug are worker jobs (`JobKind.PLUG_DEVICE`,
`JobKind.UNPLUG_DEVICE`) because the provider call can wait on the guest. Each writes its ledger
row state in the enqueue transaction, so both join `STATE_FENCED_JOB_KINDS` (ADR-0550). Exact
parameters,
`data` fields, errors and limits are in the spec,
`docs/workflow/specs/2026-11-18-provider-device-hotplug-design.md`.

- `systems.plug_device(system_id, kind, network?, size_gb?, idempotency_key?)` — contributor.
- `systems.unplug_device(system_id, device_id, idempotency_key?)` — contributor.
- `systems.list_devices(system_id)` — viewer; a database read, no provider I/O.

Neither mutating tool uses the destructive-op gate. That gate is reserved for `force_crash`
(`security/authz/gate.py`); a graceful un-plug of a device kdive created is contributor lifecycle
over the contributor's own System, like `systems.reprovision`. A future forced un-plug would be a
new decision and would need the gate.

**Persist the devices kdive plugs.** A `system_devices` child ledger (`system_id` references
`systems(id)` with `ON DELETE CASCADE`) holds one row per device from the request until a
successful un-plug deletes it. States: `plugging`, `attached`, `unplugging`, `failed`, `lost`.
Columns, state edges and the repairs are in the spec. The ledger is the index of record, as
`snapshots` is for ADR-0378, because it gives a read for `list_devices` without provider I/O, a
durable in-flight marker for a provider that finishes asynchronously, and the handle-to-device
mapping a provider with no device names (PowerVM slots) cannot recover by itself. Providers do not
list foreign devices: kdive reports and removes only what it plugged, so the boot disk and the SSH
NIC cannot be unplugged by this surface.

**System states and concurrency.** Plug and un-plug are admitted only on a `READY` System: a
`PAUSED` guest has suspended vCPUs and cannot answer an eject, and every other state has no settled
guest. They are allowed during a live Run, like snapshot. Admission takes the per-System advisory
lock and refuses with `conflict` when another plug or un-plug job is active on the System. The
exclusion runs both ways: `control.power`, `systems.snapshot`, `systems.restore`, and
`systems.reprovision` refuse with `conflict` while a plug or un-plug job is queued or running, as
`_active_snapshot_op` does for snapshot jobs, each check inside the System advisory lock. Teardown
is exempt, so a stuck job cannot block reclaim: its `release_all` runs and a racing plug job finds
the System gone and fails. The worker re-checks `READY` and the row at job start. Other jobs that
touch the domain (`force_crash`, SysRq, vmcore capture, debug) are not excluded; a provider fault
from one surfaces as `INFRASTRUCTURE_FAILURE`. `READY` does not prove the guest runs (power OFF
changes no System state), so the port raises `INFRASTRUCTURE_FAILURE` with `data.reason:
domain_not_running` for a stopped domain. A System holds at most 8 ledger rows. The limits are
chosen here because the issue asks for the tool's limit contract; the spec states all five parts.

**Asynchronous completion and un-plug of a device in use.** The worker waits for the provider to
settle: a `settled=False` plug or un-plug is polled with `is_attached` once a second, up to 60 s for
plug and 30 s for un-plug, measured on the worker's monotonic clock from the start of the provider
call. At the deadline the job fails `CONTROL_FAILURE` with `data.reason: settle_timeout` and the row
becomes `failed`. Both job kinds enqueue with `max_attempts=1`: a failure is recorded on the row and
the agent retries by tool call, never by an automatic re-run against a row that already says
`failed`. An operator `jobs.cancel` mid-call is the same case as a dead worker: the row stays
`plugging` or `unplugging` until the reconciler repair fails it. A guest that holds the device
either gets an explicit provider refusal (`unplug_refused`; the row returns to the state the un-plug
started from) or never answers (`settle_timeout`, the row becomes `failed`); the device stays in the
guest in both cases. Retrying `systems.unplug_device` on a `failed` row is safe and removes the row
once the device is gone.

**What invalidates the ledger.** The `systems` row survives teardown and reprovision, so no cascade
fires: their workers, and the system-reclaim job that calls `delete_snapshots_for_system`, call
`release_all` best effort and delete the System's ledger rows. A plug the port rejects with
`configuration_error` deletes its row, since nothing was attached. A `control.power` OFF or CYCLE,
and a snapshot restore, replace the running device set: when they succeed, their worker moves the
System's `attached` rows to `lost`. `unplug_device` on a `lost` row takes the normal path: the
provider finds the device absent, frees any backing volume, and the row is deleted. The reconciler
moves a `plugging` or `unplugging` row with no active job to `failed` (`repair_stalled_device_ops`),
as `repair_stalled_creating_snapshots` does; it makes no provider call.

## Consequences

- #3686 can build the flag, port, tools, migration and fault-inject provider from this record and
  the spec without further design. fault-inject must be able to produce unknown device, busy
  guest, asynchronous settle and an injected fault deterministically.
- The new migration widens `jobs_kind_check` and adds one table. Per the migration landmine, the
  four `migrations[-K:]` window tests need the new row.
- Devices are live-only: kdive does not promise a plugged device survives a power cycle, and a
  restore can bring back a device that was un-plugged. The `lost` state shows the agent that.
- Disk size is capped at 32 GB per device and the backing storage is not charged to a project
  quota; a deployment that needs that must file it. No follow-up exists yet.
- An agent cannot test driver behaviour on surprise removal, because forced un-plug is deferred.
- #3688 depends on #1822 and #1818 and is complete only after a recorded ppc64le run
  (`verification:ppc64le`).

## Considered & rejected

- **"Adapter" means any PCIe or SR-IOV device in the first release.** judgment: passthrough
  claims devices through ADR-0068 allocation state, and un-plug needs the guest driver unbound;
  neither has a safe graceful path on libvirt that matches the DLPAR one.
- **A single `supports_hotplug` boolean.** judgment: libvirt and PowerVM differ per kind, and a
  boolean would let a provider claim a kind it cannot attach; `capture_methods` and
  `debug_transports` already use a frozenset for the same reason.
- **Read attached devices live from the provider and keep no table.** judgment: `list_devices`
  would need a worker job, an asynchronous provider would have no durable in-flight marker, and a
  PowerVM slot cannot be mapped back to a kdive device. The ledger costs one migration and one
  repair.
- **A forced un-plug flag.** judgment: removing a device the guest is using can wedge or crash the
  guest, which is a destructive op for the gate and not a graceful lifecycle call; it has no
  owner yet and can be added under the gate without changing these tools.
- **Persist the device in the provider definition so it survives power cycles.** judgment: libvirt
  can do it, but DLPAR dynamic add updates the running partition only, so a uniform promise would
  need a profile write on PowerVM that this record cannot evidence.
- **Plug and un-plug as synchronous server calls.** judgment: the server plane never waits on a
  provider (AGENTS.md); a 30 to 60 s guest wait belongs in a worker job, as with snapshot.
- **Cancelable plug and un-plug jobs.** judgment: a cancel mid-call leaves the device state
  indeterminate; the settle deadline already bounds the wait, so they stay out of
  `CONTRIBUTOR_CANCELABLE_JOB_KINDS` and only an operator can cancel them.
- **Gate un-plug with the destructive-op gate.** verified: `OPT_IN_DESTRUCTIVE_JOB_KINDS` holds
  only the `force_crash` job kind and ADR-0378 rejected the gate for restore on the same grounds.

### Amendment (2026-11-19): reclaim deletes rows only; a failed reprovision marks devices `lost` (#3717)

This amends "What invalidates the ledger", not the decision. That paragraph says teardown,
reprovision and the system-reclaim job all call `release_all` best effort and delete the System's
ledger rows. As shipped (#3710, #3714), only teardown and reprovision call `release_all`. The
system-reclaim path deletes the rows alone, because the provider proved physical absence and no
device is left to release. A reprovision deletes the rows when it succeeds; when it fails after
`release_all`, the System's `attached` rows move to `lost` in the transaction that records the
failure, as for a power OFF or CYCLE. The spec's Ledger section states the current behaviour.

### Amendment (2026-11-20): `unplug` and `is_attached` take the device kind (#3737)

This amends the port signatures in "Port", not the decision. `unplug` and `is_attached` gain a
required keyword-only `kind: HotplugDeviceKind`, the ledger row's kind. A device that is absent
from the live domain leaves no XML to read its kind from, and only a `disk` has a backing volume,
so the provider frees the volume only for a `disk`: a NIC un-plug, and a NIC `is_attached` that
finds the device absent, make no volume call. When a disk's volume is freed is unchanged. The
fault-inject port accepts and ignores `kind`.

### Amendment (2026-11-19): the storage-quota deferral is owned by ADR-0827 (#3720)

This qualifies the Consequences bullet that says the backing storage "is not charged to a project
quota" and that "no follow-up exists yet". #3720 is that follow-up, and
[ADR-0827](0827-charge-hot-plugged-scratch-disks-to-a-project-storage-cap.md) proposes the
decision: a server-configured per-project cap, charged from the device ledger. Until it is
implemented, the storage stays uncharged as this record states.

### Amendment (2026-11-19): pre-existing volumes as disks are decided in ADR-0829 (#3722)

This amends the "Deferred" list in "Decision", not the decision: the deferral "pre-existing
volumes as disks (needs a storage-reference model)" now has a proposed decision,
[ADR-0829](0829-hotplug-existing-volumes-attach-an-overlay-of-a-catalog-image.md), which names a
catalog image as the reference and attaches a kdive-owned overlay. The `disk` kind defined above
is unchanged.
