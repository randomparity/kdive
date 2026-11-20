# 0830 — Passthrough hot-plug binds a claimed host device to a System through the allocation's PCIe claim and a ledger row

## Status

Accepted (2026-11-19)

Implementation is tracked by the issues the spec proposes under #3723 and #3724. Nothing in this
record is implemented yet.

## Context

ADR-0823 hot-plugs two device kinds, `nic` and `disk`, and defers "dedicated PCIe slots and PCI
passthrough" (#3723) and "SR-IOV logical ports and VFs" (#3724) because they need the PCIe
allocation state of ADR-0068 and guest-driver handling on un-plug. The two issues overlap in the
guest-driver rules, so this record settles both. It is one record rather than two: the claim
binding, the failed-detach rule and the reclaim rule are the same for a whole device and for a
virtual function, and an SR-IOV record would restate them.

What exists on main, verified at 76834c6b0:

- ADR-0068's claim is on the **Allocation**: `allocations.pcie_claim` is a list of `{bdf, vendor_id,
  device_id}`; occupancy is derived from the claims of non-terminal allocations
  (`services/allocation/admission/pcie_claim.py:48`), and resolve-and-claim runs in the
  per-Resource lock (`services/allocation/admission/core.py:243`, `:329`). No `<hostdev>` is
  emitted anywhere; the one `hostdev` in `src/` is a count in the remote authority projection
  (`providers/remote_libvirt/system_authority.py:555`).
- A System is "one per Allocation" (`domain/lifecycle/records.py:71`), so a claim on the allocation
  is a claim on exactly one System.
- An allocation can reach `released` or `expired` while its System is not torn down: the only
  teardown precondition is for a System with external-boot history
  (`services/allocation/release.py:223`, `reconciler/repairs/allocations.py:235`). Its claim then
  stops counting as occupancy while the domain may still hold the host device, and the System stays
  `READY` until the reaper tears it down.
- Teardown deletes the System's device rows before it destroys the domain
  (`jobs/handlers/systems.py:804-805`); a failed reprovision keeps rows as `lost`.
- Discovery writes the five-field descriptor from libvirt node devices on local-libvirt only
  (`providers/local_libvirt/discovery.py:323`); remote-libvirt defers PCIe enumeration
  (`providers/remote_libvirt/discovery.py:11`). `ResourceCapabilities.pcie_descriptors` keeps only
  those five fields (`domain/catalog/resource_capabilities.py:289`).
- The fleet availability read runs its own claim query
  (`mcp/tools/catalog/availability.py:180-203`), separate from `active_claims`.
- libvirt's `live_devices` recognises only `<interface>` and `<disk>` elements
  (`providers/shared/libvirt_hotplug.py:174-193`); a `<hostdev>` is invisible to it, and an
  `<interface type='hostdev'>` would read as a plain `nic`.
- local-libvirt boots q35 with no spare PCIe root ports (no `<controller>` is rendered under
  `providers/local_libvirt/lifecycle/xml.py`); remote-libvirt defaults to i440fx
  (`providers/remote_libvirt/config.py:38`).

## Decision

**Two kinds.** `HotplugDeviceKind` gains `PCI = "pci"` (a whole host PCI device: a dedicated
PCIe slot's device) and `SRIOV_VF = "sriov_vf"` (one virtual function of a physical port). They
are advertised separately through `ProviderSupport.hotplug_devices`, so a provider can offer one
without the other. `DeviceSpec` gains two trailing optional fields, `host_device` and
`physical_port`, strings the provider interprets (libvirt: a BDF; PowerVM: a slot or port id,
left to #3688). On libvirt a "logical port" is the VF; a PowerVM logical port maps to the same
`sriov_vf` kind under #3688.

**Claim binding: the allocation's claim is the reservation; a ledger row is the binding.** A
passthrough plug draws only from devices the System's own allocation already holds in
`pcie_claim`. The agent reserves the hardware with `allocations.request(pcie_devices=[...])`
exactly as today and plugs it with the same match-spec grammar; `systems.plug_device` resolves the
spec to one claimed device that no ledger row holds. Un-plug deletes the row, which returns the
device to the System's unbound pool and, for a VF, restores the pool count; the reservation
stays with the allocation until it ends, as ADR-0068 says ("the device is held for the
allocation's life"). The issue's "claim released on detach" is therefore read as "the binding is
released". Releasing the reservation itself would satisfy the literal text but adds a second claim
path through the per-Resource lock and the ADR-0069 queue; the pull request lists it as an
operator decision.

**Plug requires a live allocation.** Admission resolves a target only while the System's
allocation is non-terminal (`allocation_not_active` otherwise), and the job checks again before the
provider call. Without it, a System that outlives its expired allocation could plug a device a
second allocation has since claimed.

**Occupancy covers the ledger.** A device is occupied when a non-terminal allocation's claim holds
it **or** a `system_devices` row on that host names it. `active_claims` adds the second source.
This closes the window above: an expired allocation's device stays unavailable until the domain
that may hold it is proven gone. The fleet availability read uses the same predicate and the same
VF-of-claimed-PF expansion (one pure function, `occupied_bdfs` in `domain/pcie.py`, called by both
the admission matcher and availability), so admission and availability agree. A physical port is
held either whole by a `pci` row or by `sriov_vf` rows, never both; admission refuses the second
with `no_free_device`. Passthrough rows are deleted only after the provider has destroyed the
domain (teardown and reprovision) or proved it absent (system-reclaim); they are not deleted with
the `nic` and `disk` rows ahead of the destroy call. Teardown deletes them in `teardown_handler`
immediately after `provisioner.teardown` returns, in any System state, through
`delete_devices_for_system(..., kinds=...)`; the `nic` and `disk` rows still go ahead of the
destroy. Un-plug settle, and `unplug_device` on a `lost` row, delete a row while the domain lives,
after the provider confirms the device absent.

**Hosts must opt devices in; kdive never rebinds host drivers.** libvirt attaches with
`managed='no'`, which requires the device to be bound to `vfio-pci` already (unverified until the
live proof). The operator binds the devices it offers (and a PF's VFs) to `vfio-pci`; a device bound
to anything else is refused with `configuration_error`, `reason: device_not_assignable`. This keeps
a contributor from taking the host's own boot disk or management NIC, which a descriptor list of
every PCI function would otherwise allow, and it removes the host rebind step from the failed-detach
path.

**Guest-driver unbinding is the guest kernel's, not kdive's.** The graceful detach is a PCIe
hot-unplug request: the guest's hotplug driver unbinds the device driver and powers the slot off,
then the hypervisor completes the removal. kdive runs nothing in the guest. This needs a
hot-plug-capable slot: the provisioner reserves 4 spare PCIe root ports on a q35 System whose
allocation holds a claim (unverified until the live proof), and the provider refuses the plug with
`reason: no_hotplug_slot` when none is free; i440fx slots are hot-pluggable already. libvirt
passthrough is x86_64 only in this design; other guest architectures refuse the kinds. vfio needs
KVM: the server refuses a System whose `accel` is `tcg`, and the provider refuses a domain that is
not of type `kvm` (`reason: accel_unsupported`); a `NULL` accel is unknown to the server.

**A detach the guest never acknowledges.** The settle windows are ADR-0823's (60 s plug, 30 s
un-plug); how long a real adapter's driver takes to release is unmeasured, and the live proof
records it. At the deadline the row becomes `failed`, the job fails `settle_timeout`, and the row
keeps the device occupied: the guest may still hold it. Retrying `unplug_device` is safe. Teardown
or reprovision destroys the domain, which is what frees the device; there is no forced path here
(#3721 owns it).

**SR-IOV pool bookkeeping is derived, not stored.** Discovery adds an optional `pf_bdf` to the
descriptor of each virtual function (read from the node device's `phys_function` capability), so
the VFs of a physical port are the descriptors that share its BDF. Capacity is their count;
ownership is the claim-or-row occupancy above. A claim on a physical port is the right to draw its
VFs: at admission the matcher treats a VF as claimed when its PF is held by a claim, so no later
allocation can take a VF from under the pool owner, and the draw needs only the System advisory
lock plus a unique index on `(resource_id, host_device)`. Without that rule, a direct VF claim by
another allocation would race the draw and force plug admission to take the per-Resource lock. A VF
already claimed before the port is skipped by the draw. kdive does not create VFs (`sriov_numvfs`
is host setup).

**Surface.** `systems.plug_device` gains `device` (match spec, `kind=pci`) and `port` (match spec
of a claimed physical port, `kind=sriov_vf`); the existing limit of 8 rows applies. Roles are
unchanged: contributor, no destructive-op gate. The spec holds every parameter, reason, state edge
and column.

## Consequences

- #3723 and #3724 share one claim-binding, occupancy and reclaim implementation; the split in the
  spec puts it in the `pci` issue and makes the VF issue the descriptor field, the pool draw and
  the interface-hostdev rendering.
- `active_claims` reads a second table, so the admission path gains one query; the occupancy
  predicate must stay in one function so the claim, availability and ledger views agree.
- Passthrough works only on hosts whose operator has bound the devices to `vfio-pci`, and only on
  local-libvirt until remote-libvirt enumerates node devices. A host or hardware without an IOMMU
  refuses the attach (`plug_refused`).
- A q35 System that claims a device boots with spare root ports; this changes the rendered domain
  XML for those Systems and needs live proof.
- A guest that never releases a device pins it until teardown or reprovision. Accepted.
- A guest holding a vfio device may not be snapshot-able; the record adds no refusal, so a snapshot
  taken then fails in the provider with its own error until a live proof says otherwise.
- Nothing proves this on hardware yet; the live-proof host class is named in the spec and none is
  assigned.

## Considered & rejected

- **Plug-time claim, released on detach.** judgment: it matches the issue's wording, but it adds a
  mutation of the claim set outside admission, takes the per-Resource lock from a worker, and
  bypasses the ADR-0069 queue; the allocation already carries the reservation the issue asks for.
  It remains open as an operator decision in the pull request.
- **`managed='yes'`, letting libvirt detach and re-attach the host driver.** judgment: it would let
  a contributor unbind a host's boot or management device, and a detach the guest never answers
  would leave a host driver rebind to fail with no kdive recovery.
- **kdive unbinds the driver through guest exec before detach.** judgment: it needs a reachable
  guest, an allowlist entry (ADR-0822 shows the cost of each) and a guest-side script per driver,
  and the guest's own hot-unplug handling already does the unbind. A guest that is not reachable
  is the case where detach matters most.
- **A separate SR-IOV pool table.** judgment: it would duplicate a count the descriptors already
  give and could drift from them after a re-discovery, the failure ADR-0068 avoided with its
  derived occupancy.
- **Draw VFs without a claim on the port.** judgment: any contributor could take VFs another
  allocation relies on, and the draw would need the per-Resource lock.
- **Release rows with `nic` and `disk` rows ahead of the domain destroy.** judgment: a destroy that
  fails would leave a free-looking device inside a live domain.
- **A longer un-plug window for passthrough, a per-System passthrough row limit and a snapshot
  refusal.** judgment: none is evidenced; ADR-0823's windows and limit stand and the live proof
  can justify a change.
- **Two records.** judgment: stated in Context.
