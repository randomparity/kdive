# Libvirt forced un-plug of a guest-refused NIC or disk (#3755)

Date: 2026-11-20. Hosts are anonymised: `lab-guest-A` is one x86_64 Fedora 44 guest (KVM, 8 vCPU)
running libvirt and QEMU directly; no kdive stack was involved, because the question is about the
daemon.

## Versions

| Component | Version |
|---|---|
| libvirt daemon, library, API | 12.0.0 (`virsh version`: library 12.0.0, API QEMU 12.0.0) |
| QEMU | 10.2.2 (`qemu-kvm-10.2.2-1.fc44`) |
| Guest kernel (direct boot, 1 GiB, 2 vCPU) | 6.19.10 |
| Python binding used for flag-level calls | `python3-libvirt-12.0.0-1.fc44` |

The project pins `libvirt-python==12.9.0`; the daemon is what rejects or accepts a flag, and it is
12.0.0 here.

## Method

Per run: define a test domain (direct kernel boot, host kernel and initramfs, no
disk), hot-plug one device with `attachDeviceFlags(LIVE)` and the adapter's alias
`ua-kdive-<id>` (NIC: virtio on the `default` network; disk: 64 MiB qcow2 on virtio), send the
graceful `detachDeviceFlags(LIVE)`, then record the live domain XML and QEMU's
`/machine/peripheral` after every candidate. A run counts as a refusal only if the graceful
detach leaves the device in the live XML after libvirt's wait (5.0 s).

Refusal methods, each run for both kinds:

1. i440fx (`pc`) machine with `acpiphp.disable=1` on the guest command line: the guest kernel is
   alive but never acts on the ACPI eject request.
2. Paused vCPUs (`virDomainSuspend`) on q35.
3. Paused vCPUs on i440fx.

Not a refusal, found while building the matrix: `pciehp.pciehp_disable=1` on q35 does not stop
removal. QEMU completes the un-plug at once when the guest never powered the slot. Controls
(guest left alone, q35 and i440fx, both kinds) removed the device in under 0.1 s, so the matrix
does distinguish a refusing guest from a prompt one.

## Results

Every refusal arm showed the graceful detach returning success and leaving the device in the
live XML and in QEMU. Candidates, identical outcome in all six arms (3 methods x 2 kinds), except
where noted:

| Candidate | Outcome | Device left live XML? |
|---|---|---|
| `detachDeviceFlags(LIVE\|FORCE)` (F14) | `unsupported flags (0x4) in function qemuDomainDetachDeviceLiveAndConfig` | no |
| `detachDeviceAlias(LIVE)` | returns 0, same pending request as graceful | no |
| `detachDeviceAlias(LIVE\|FORCE)` | `unsupported flags (0x4) in function qemuDomainDetachDeviceAliasLiveAndConfig` | no |
| `updateDeviceFlags(LIVE\|FORCE)` | returns 0, no-op for a NIC or disk | no |
| QMP `device_del` x3, spaced 8 s | each returns `{"return":{}}`, request stays pending | no |
| NIC: `domif-setlink down` | link flag only | no |
| NIC: QMP `netdev_del` of the tap backend | returns `{}`; `info network` still lists the tap and the NIC stays in the XML (a first run named the wrong backend id and got `DeviceNotFound`) | no |
| disk: QMP `blockdev-del`, HMP `drive_del` of the format and storage nodes | `Node ... is in use` | no |
| `virDomainReset` run alone on a fresh guest start | removes the device in all six arms | yes |

F14 is confirmed on the running 12.0.0 daemon for both `detachDeviceFlags` and
`detachDeviceAlias`.

`virDomainReset` is not an un-plug. It resets the machine, and QEMU finishes the pending removal
as part of that reset. In the i440fx arm the serial log shows a second kernel boot after the call; a paused
guest stays paused. It was run with one pending device, so
whether it completes several was not tried. It reboots the guest.

Side finding: in the run that executed the whole candidate sequence in order (device_del x3,
`netdev_del`, then `virDomainReset`), the NIC left QEMU but stayed in libvirt's live XML
(`is_attached` `True`) in all three NIC arms. That sequence was not split to isolate `netdev_del`
as the cause. Whatever the cause, libvirt and QEMU disagreed, which is the ADR-0828 forbidden
case, so the reset was re-run alone on a fresh guest start for the table above.

## Conclusion

Per kind, `nic` and `disk`, on libvirt 12.0.0 over QEMU 10.2.2 (x86_64): no tested call removes
one refused device. Every per-device candidate either rejects the flag, repeats the pending request, or
changes only a backend or link. The one call that satisfies the postcondition is a whole-guest
reset. No prototype `force_unplug` was written and no kind is advertised.

## Not proven

- Some observations were made in the live session and are not in the retained raw output: the
  `pciehp_disable` non-refusal, the second kernel boot after the reset, the guest kernel and
  sizing, and the package versions.
- Behaviour on remote-libvirt, on a ppc64le or s390x guest, or with a different QEMU.
- A guest that refuses by keeping a mounted file system busy: PCI eject does not consult the file
  system, so this was not tried as a separate refusal.
- Whether `virDomainReset` is acceptable as a forced un-plug. That is an operator decision.

## End state

The test domains, their images and their logs were removed from `lab-guest-A`, and the guest was
then stopped; its snapshot chain is unchanged.
