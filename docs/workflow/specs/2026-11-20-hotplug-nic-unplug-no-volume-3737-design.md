# NIC un-plug makes no volume call (#3737)

Decision record: [ADR-0823](../../adr/0823-provider-device-hotplug-nic-and-scratch-disk.md),
amended by this change.

## Problem

`LibvirtDeviceHotplug.unplug` and `is_attached` free the device's scratch volume whenever the
device is absent from the live domain, without knowing its kind. A NIC has no volume, so each NIC
un-plug issues a removal for a name that never existed: a file unlink on local-libvirt, a
storage-pool lookup and delete attempt on remote-libvirt. An absent device leaves no XML to read
the kind from, and the provider keeps no state across calls, so the kind must come from the caller.

## Scope

In: the `DeviceHotplugger` port, the shared libvirt implementation (both libvirt providers), the
un-plug handler's three call sites, signature-only changes to the fault-inject port, tests, the
ADR-0823 amendment. Out: when a disk's volume is freed; any fault-inject behaviour.

## Success

- `unplug(domain, device_id, provider_handle, *, kind)` and `is_attached(..., *, kind)` take the
  ledger row's `HotplugDeviceKind`; the parameter is required and keyword-only.
- The shared implementation frees a scratch volume through one helper, only for `DISK`. A NIC
  un-plug, a NIC `is_attached` that finds the device absent and an `unplug` of an absent NIC make
  no `volumes` call. A disk makes one call per freeing look, as before.
- The handler passes `HotplugDeviceKind(row.kind)` to `unplug` and its `is_attached` poll, and
  `spec.kind` to the plug-settle poll. Fault-inject accepts and ignores the argument.
- Failure model: a disk passed as `nic` leaks its volume only until `release_all` at teardown or
  reprovision, which already sweeps every device volume; the handler takes the kind from the same
  ledger row it uses to build the plug spec.

## Validation

- `tests/providers/shared/test_libvirt_hotplug.py`: NIC prompt un-plug, NIC un-plug then
  `is_attached`, and NIC absent `unplug`/`is_attached` record zero `remove` calls; disk absent
  calls record one each; red when the kind check is removed.
  Green: `just test-verbose tests/providers/shared/test_libvirt_hotplug.py`.
- `tests/providers/remote_libvirt/lifecycle/test_hotplug.py`: storage-pool lookups are 0 for a NIC
  and 1 per call for a disk. Green: `just test-verbose` on that file.
- `tests/providers/local_libvirt/test_hotplug.py`: a leftover scratch file survives a NIC and is
  unlinked for a disk. Green: `just test-verbose` on that file.
- `tests/jobs/handlers/test_device_handlers.py`: the row's kind reaches `unplug` and
  `is_attached`; red when the handler passes a fixed kind. Green: `just test-verbose` on that file.
- Port signatures, fault-inject and the ADR amendment: checked by `just type` and the fault-inject
  suite; the ADR text is prose, `task-test-not-applicable`.
