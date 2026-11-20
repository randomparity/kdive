# NIC un-plug makes no volume call (#3737)

Decision record: [ADR-0823](../../adr/0823-provider-device-hotplug-nic-and-scratch-disk.md),
amended by this change.

## Problem

`LibvirtDeviceHotplug.unplug` and `is_attached` free the device's scratch volume whenever the
device is absent from the live domain, without knowing its kind. A NIC has no volume, so each NIC
un-plug issues a removal for a name that never existed: a file unlink on local-libvirt, a
storage-pool lookup and delete attempt over the provider connection on remote-libvirt.

## Scope

In: the `DeviceHotplugger` port, the shared libvirt implementation (both libvirt providers),
the un-plug handler's two call sites, signature-only changes to the fault-inject port, tests, the
ADR-0823 amendment. Out: when a disk's volume is freed; any fault-inject behaviour.

## Contract

- `unplug(domain, device_id, provider_handle, *, kind)` and `is_attached(..., *, kind)` take the
  ledger row's `HotplugDeviceKind`. The parameter is required and keyword-only, so no caller
  defaults it.
- `LibvirtDeviceHotplug` frees the scratch volume through one helper, and only for
  `HotplugDeviceKind.DISK`. A NIC un-plug, a NIC `is_attached` that finds the device absent and
  an `unplug` of an already-absent NIC make no `volumes` call.
- The handler passes `HotplugDeviceKind(row.kind)` to `unplug` and the `is_attached` poll, and
  `spec.kind` to the plug-settle poll.

## Why the port, not the provider

The kind could be read from the device's XML only while the device is present. The two paths that
free a volume most often, the poll after a pending un-plug and the retry of an un-plug whose
device is already gone, run with the device absent. The provider keeps no state across calls (a
connection is opened per call), so nothing else can say what kind an absent id was. The handler
already holds the ledger row, so the kind passes through the port.

## Failure model

- Wrong kind from a caller: a `disk` passed as `nic` leaks its volume until `release_all` at
  teardown or reprovision, which already sweeps every device volume of the domain. The handler
  takes the kind from the ledger row it also uses to build the plug spec, so the two agree.
- Absent device, kind `disk`: unchanged, the volume removal is idempotent.
- Fault-inject: the argument is ignored; its ledger already knows each device.

## Tests

Shared fake-domain tests: NIC un-plug (prompt guest), NIC un-plug then `is_attached`, NIC absent
`unplug` and `is_attached` make zero `volumes.remove` calls; a disk makes one per call. Per
provider with the real volume class: remote counts storage-pool lookups (NIC 0, disk 1 per call),
local checks a leftover file survives for a NIC and is unlinked for a disk. A handler test checks
the row's kind reaches `unplug` and `is_attached`. Controlled fault: removing the kind check turns
the NIC tests red.
