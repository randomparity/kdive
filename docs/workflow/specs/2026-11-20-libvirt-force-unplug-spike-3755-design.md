# Can libvirt force a guest-refused NIC or disk out of the live domain? (#3755)

Decision record: [ADR-0828](../../adr/0828-forced-device-unplug-behind-the-destructive-operation-gate.md),
amended by this change. Result record: [proof](../proofs/2026-11-20-libvirt-force-unplug-3755.md).

## Problem

ADR-0828 leaves local-libvirt and remote-libvirt without a forced kind until a mechanism is
proven on a live daemon. F14 (the QEMU driver rejects the force flag on detach) was read from the
v12.0.0 source only. This spike answers, per kind, whether any libvirt call makes `is_attached`
`False` for a device the guest refuses to release.

## Scope

In: an experiment on one x86_64 lab guest host (libvirt 12.0.0, QEMU 10.2.2) with a refusing
guest, per kind; the F14 re-check; the proof record; an append-only ADR-0828 amendment.
Out: the held items (port, job kind, migration, fault-inject, tool, libvirt advertising),
PowerVM (#3688), any change to the `DeviceHotplugger` protocol, the gate or advertised
capabilities. No prototype ships: the only call that removes the device resets the whole guest.
No ownership change is needed.

### Failure model

A candidate counts only if the device leaves the live domain XML (`is_attached` `False`). A
backend removal, a link-down or a call that returns success while the device stays is a failure.
A candidate run after another can leave libvirt's XML and QEMU's device list disagreeing, so each
candidate that ends the run is measured on a fresh guest start.

## Success

Each kind has a recorded result with versions, calls and the live postcondition; F14 is stated
for the running daemon; each kind ends in an amendment; no stale-XML outcome is counted as forced.

## Validation

- ADR-0828 amendment: `task-test-not-applicable` (documentation; `just adr-status-check` and the
  docs gates cover it).
- Proof record: `task-test-not-applicable` (evidence document; the live run is the proof).
