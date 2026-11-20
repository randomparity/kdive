# PCI passthrough and SR-IOV virtual function hot-plug (#3723, #3724)

Decision: [ADR-0830](../../adr/0830-passthrough-hotplug-binds-a-claimed-device-through-the-allocation-and-a-ledger-row.md).
Builds on [ADR-0823](../../adr/0823-provider-device-hotplug-nic-and-scratch-disk.md) and its spec,
[2026-11-18-provider-device-hotplug-design.md](2026-11-18-provider-device-hotplug-design.md), and on
[ADR-0068](../../adr/0068-custom-config-pcie-modeling.md). Epic: #1380.

## Problem

An agent testing a driver for a specific card needs to hand that card, or one virtual function of
it, to a running System and take it back. ADR-0068 lets an allocation reserve the card and
documents that nothing attaches it. ADR-0823 hot-plugs only NICs and scratch disks and defers
both passthrough kinds. This spec holds the contract the implementation issues build against.

## Verified against main (76834c6b0)

| Claim | Evidence |
|---|---|
| The claim lives on the allocation; occupancy is derived from non-terminal allocations | `services/allocation/admission/pcie_claim.py:48-70`; `db/schema/0014_allocations_pcie_claim.sql` |
| Resolve-and-claim runs in the per-Resource lock at admission | `services/allocation/admission/core.py:243`, `:329-345` |
| A System is one per Allocation | `domain/lifecycle/records.py:71-72` |
| `System.accel` is `kvm`, `tcg` or `NULL`, resolved from advertised `guest_arches`; fault-inject advertises none, so it is `NULL` there | `domain/lifecycle/records.py:86`; `services/systems/validation.py:56`; `services/systems/admission.py:795` |
| An allocation can end with its System not torn down (only external-boot history is fenced) | `services/allocation/release.py:223-237`; `reconciler/repairs/allocations.py:216-246` |
| The fleet availability read has its own claim query | `mcp/tools/catalog/availability.py:180-203` |
| Teardown deletes device rows, then destroys the domain | `jobs/handlers/systems.py:709-722`, `:804-805` |
| `delete_devices_for_system` deletes every kind; reclaim callers use it | `db/repositories.py:542`; `jobs/handlers/system_reclaim.py:111`; `reconciler/repairs/jobs.py:117` |
| Availability and the matcher each subtract a claimed-BDF set, and neither knows a descriptor's `pf_bdf` (it does not exist yet) | `mcp/tools/catalog/availability.py:204-208`; `domain/pcie.py:214-215` |
| A successful reprovision deletes rows in its commit; a failed one marks `attached` rows `lost` | `jobs/handlers/systems.py:264`; ADR-0823 Amendment (2026-11-19) |
| Local discovery writes the five-field descriptor from node devices; remote does not enumerate | `providers/local_libvirt/discovery.py:186-228`, `:323-341`; `providers/remote_libvirt/discovery.py:11` |
| The capability view keeps only the five descriptor fields | `domain/catalog/resource_capabilities.py:289-308` |
| `live_devices` finds only `<interface>` and `<disk>` and types every `<interface>` as `nic` | `providers/shared/libvirt_hotplug.py:174-193` |
| Admission limits: 8 rows, unpluggable states, advisory lock | `mcp/tools/lifecycle/systems/devices.py:52`, `:58`, `:196-268` |
| Handlers: settle windows and state edges | `jobs/handlers/devices.py:48-49`, `:136-285` |
| Every provider advertises through one `ADVERTISED_KINDS` | `providers/local_libvirt/composition.py:525`; `providers/remote_libvirt/composition.py:549`; `providers/fault_inject/composition.py:180` |
| q35 System XML renders no `<controller>`; remote defaults to i440fx | `providers/local_libvirt/lifecycle/xml.py` (none; comment at `:436`); `providers/remote_libvirt/config.py:38-47` |

## Design

### Capability and port

`HotplugDeviceKind` gains `PCI = "pci"` (item A) and `SRIOV_VF = "sriov_vf"` (item C, so no provider
advertises a kind whose resolution does not exist yet). `ProviderSupport.hotplug_devices` and
`systems.get data.hotplug_devices` carry them like the existing kinds; the capability refusal is
`capability_unsupported` with `capability: hotplug:pci` or `hotplug:sriov_vf`. `DeviceSpec` gets two
trailing fields with defaults, so existing constructors keep working:

```python
host_device: str | None = None  # pci, sriov_vf: the resolved host device (libvirt: BDF)
physical_port: str | None = None  # sriov_vf: the physical port the VF belongs to (libvirt: PF BDF)
```

The server resolves both from the database before it enqueues, because claims and descriptors are
database state; the port only attaches what it is told and checks the host. `PlugOutcome.identity`
is `{"pci_id": "vvvv:dddd"}` for `pci` and `{"mac": ..., "pci_id": ...}` for `sriov_vf` (the
deterministic `nic_mac(device_id)` MAC, set on the VF through libvirt). The `DeviceHotplugger`
methods and their idempotence rules are unchanged. `release_all` has nothing to free for these
kinds. PowerVM's slot or logical-port id travels in the same two strings and `provider_handle`;
what they mean there is #3688's.

### Tools

Both mutating tools keep their ADR-0823 admission order, errors and audit. `systems.plug_device`
gains two parameters:

`systems.plug_device(system_id, kind, network=None, size_gb=None, device=None, port=None,
idempotency_key=None)`; `kind` is now `nic`, `disk`, `pci` or `sriov_vf`.

| Parameter | Applies to | Form |
|---|---|---|
| `device` | `pci` (required) | an ADR-0068 match spec, `vendor:device` or `class=NN`/`class=NNNN`, lowercase |
| `port` | `sriov_vf` (required) | an ADR-0068 match spec naming a physical port the allocation claimed |

Parameter errors are `configuration_error` with `data.reason`; the existing rows keep their order
and `parameter_mismatch` still comes first (`device` or `port` given to the wrong kind, or
`network`/`size_gb` given to a passthrough kind).

| Condition | `data.reason` |
|---|---|
| `device` missing or not a valid match spec for `pci` | `invalid_device` |
| `port` missing or not a valid match spec for `sriov_vf` | `invalid_port` |
| System `accel` is `tcg` (the provider also refuses a non-`kvm` domain) | `accel_unsupported` |
| no device in the allocation's claim matches `device` | `device_not_claimed` |
| no claimed physical port matches `port`, or the match has no VFs | `port_not_claimed` |
| two or more claimed ports match `port` | `ambiguous_port` |

Conflicts, `conflict` with `data.reason`, checked after the ADR-0823 ones (`system_not_ready`,
`device_op_in_flight`, `device_limit`; the limit of 8 is unchanged):

| Condition | `data.reason` |
|---|---|
| the System's allocation is `released`, `expired` or `failed` | `allocation_not_active` |
| every matching claimed device already has a row, or is the port of a row; or the port's pool is exhausted, or a `pci` row names the port | `no_free_device`; for a VF, `data.pool: {total, in_use}` |

Admission, in the existing transaction under the System advisory lock: read the allocation row
`FOR SHARE` and require a non-terminal state, resolve the target from its `pcie_claim` and the
resource's descriptors, insert the row `plugging` with `host_device`, `physical_port`,
`resource_id` and `identity.pci_id`, enqueue `PLUG_DEVICE`.

- `pci`: candidates are the claimed devices that match `device`, hold no row, and are not the
  `physical_port` of any row on this resource; take the lowest BDF. A claimed port is a valid `pci`
  target (the operator decided what is bound to `vfio-pci`) only while none of its VFs is plugged.
- `sriov_vf`: the claimed ports matching `port` must be exactly one, and no `pci` row on this
  resource may name that port (else `no_free_device`); candidates are the VF descriptors whose
  `pf_bdf` is that port, minus every BDF `active_claims` returns (other allocations' claims, this
  allocation's own direct claims, ledger rows); take the lowest BDF. `data.pool.total` is the VF
  descriptor count, `in_use` the number removed.

A physical port is therefore held either whole (one `pci` row) or by VF draws (`sriov_vf` rows),
never both. Both refusals are `no_free_device`; the first needs the `physical_port` column, so
both ship with item C.

A unique-index violation on the insert is `no_free_device` and the caller retries. The plug job
re-reads the allocation before the provider call; a terminal allocation fails the job `conflict`,
`reason: allocation_not_active`, and moves the row to `failed`, as `system_not_ready` does.

`systems.unplug_device` is unchanged. `systems.list_devices` items gain `pci_id` inside `identity`
and nothing else: the host BDF and the physical port stay in the ledger and are not returned.
`systems.snapshot` and `systems.restore` are unchanged: restore moves `attached` rows to `lost`, and
a snapshot of a guest holding a vfio device surfaces the provider's own error if QEMU cannot save
its state (unverified).

### Limit contract

| Limit | Unit | Clock | Scope | On violation | Recovery |
|---|---|---|---|---|---|
| Settle window: 60 s plug, 30 s un-plug, all kinds | seconds | worker monotonic clock from job start, as ADR-0823 | one job | `control_failure`, `reason: settle_timeout`; row `failed`; the guest may still hold the device | `systems.unplug_device` again, or teardown/reprovision |
| 4 spare hot-plug root ports (libvirt q35) | ports | none | per System | `configuration_error`, `no_hotplug_slot` | un-plug a device, or reprovision |

The windows and the limit of 8 are ADR-0823's and no handler change is needed; the real adapters'
release time is unmeasured, and the live proof records it.

### Ledger and schema

Item A's migration (number assigned by the implementing PR, under the migration-number scan):

```sql
ALTER TABLE system_devices
    ADD COLUMN resource_id  uuid REFERENCES resources (id),
    ADD COLUMN host_device  text;
-- drop and recreate system_devices_kind_check to admit ('nic','disk','pci')
ALTER TABLE system_devices
    ADD CONSTRAINT system_devices_passthrough_check CHECK (
        (resource_id IS NULL) = (host_device IS NULL)
        AND ((kind = 'pci') = (host_device IS NOT NULL)));
CREATE UNIQUE INDEX system_devices_host_device_idx
    ON system_devices (resource_id, host_device) WHERE host_device IS NOT NULL;
```

Item C's migration adds `physical_port text`, widens the kind check with `sriov_vf` and the
passthrough check to `kind IN ('pci', 'sriov_vf')`, and constrains `physical_port` to `sriov_vf`
rows. The state set and the transition table are ADR-0823's, unchanged. The migration touches no job
kind. The window tests that list the newest migrations take the new row, as the repository's
migration notes require.

### Occupancy

`active_claims(conn, resource_id)` returns the union of the non-terminal allocations' claims and
one claim per `system_devices` row of that resource with a `host_device`, whatever its state, so
a `failed` or `lost` row keeps the device occupied. A `sriov_vf` row also yields a claim on its
`physical_port`, so a port is not offered to another allocation while any of its VFs is bound. The ids on a row-derived claim come from the
host's descriptor list; occupancy depends only on the BDF, so a row whose BDF no longer has a
descriptor still occupies it. The fleet availability read (`availability._claims_by_resource`) is
rewritten onto one shared fleet-wide union function with `active_claims`.

The VF-of-claimed-PF expansion has one home: `occupied_bdfs(descriptors, claims)` in
`domain/pcie.py`, a pure function returning the claimed BDFs plus every descriptor BDF whose
`pf_bdf` is in that set. Both `resolve_multiset` (so `resolve_union` at admission, where every claim
belongs to another allocation) and the availability free-descriptor read (`_free_descriptors`) call
it, so a claimed port's VFs are unavailable in both places; neither re-implements the rule. The plug
draw uses the raw `active_claims` list, not the expansion, so the PF owner's own pool stays open. A
VF claimed before its PF is skipped by the draw; an allocation resolves its claim once, so the owner
never requests a VF of its own port.

The agreement test builds one host with a PF claim and its VFs, a direct VF claim and an
`sriov_vf` row, and asserts that the free descriptors availability reports equal what
`resolve_union` can hand out and that both claim sources match; it fails if either path drops the
expansion.

### Reclaim

| Event | Rule |
|---|---|
| un-plug settles | row deleted; the device is unbound again; the reservation stays |
| teardown | `release_all` best effort; `delete_devices_for_system(conn, system_id, kinds=...)` takes an optional kind set, and `None` (the default, which every existing caller keeps) deletes every kind. `_reclaim_devices` passes `kinds=(nic, disk)` and still runs before the destroy; `teardown_handler` calls it with `kinds=(pci, sriov_vf)` in its own transaction right after `provisioner.teardown` returns, whatever the System's state (`_finalize_teardown` returns early unless the System is `TEARING_DOWN`, so it is not the place); a failed destroy leaves them, so the device stays occupied |
| reprovision | success: `delete_devices_for_system(conn, system_id)` (every kind; the existing call at `systems.py:264`, unchanged) in the commit that returns the System to `READY` (the provider call has recreated the domain); failure: `attached` rows become `lost` and stay occupying |
| system-reclaim (`reclaim_device_ledger`: `system_reclaim.py:111`, and its callers in `system_authority.py:228`, `external_boot/lifecycle.py:1159`, `repairs/jobs.py:117`) | `delete_devices_for_system(conn, system_id)`, every kind, unchanged. These callers run only after the authority's proof of absence (`retained_quarantine` excluded: `lifecycle.py:1152`; `AuthoritySystemPreactivationAbsentV1`: `system_authority.py:222`), so deleting passthrough rows is correct for them; the implementing PR verifies the proof at each of the three callers |
| power OFF/CYCLE, snapshot restore | `attached` rows become `lost`; occupancy unchanged; `unplug_device` on a `lost` row deletes it after the provider finds the device absent |
| allocation released or expired with a live System | claim stops counting; the rows keep the device occupied until the reaper's teardown deletes them; plug admission refuses (`allocation_not_active`) |

### Providers

fault-inject: advertise `pci` (item A) and `sriov_vf` (item C); the in-memory table stores the two
new fields; `hotplug_behaviour` and the per-plane fault rate apply unchanged; a resource registered
with `pcie_devices` descriptors (including `pf_bdf`) serves allocation claims. libvirt (local;
remote only after node-device enumeration, see the split):

- `pci`: attach `<hostdev mode='subsystem' type='pci' managed='no'><source><address .../></source>
  <alias name='ua-kdive-<device_id>'/></hostdev>` with the live flag only. Before attaching, read the
  node device and refuse unless its driver is `vfio-pci` (`device_not_assignable`). A free
  hot-plug slot must exist: on q35 a `pcie-root-port` controller whose `index` is no other device's
  `<address bus=...>` in the live XML, else `no_hotplug_slot`; i440fx uses a free slot on `pci.0`.
  The provider also reads the live domain `type` and refuses a non-`kvm` domain
  (`accel_unsupported`); other guest architectures refuse the kind. libvirt's own refusal (no IOMMU,
  group peers not bound) is `plug_refused`.
- `sriov_vf`: attach `<interface type='hostdev' managed='no'><mac address=.../><source><address
  type='pci' .../></source><alias .../></interface>`. libvirt sets the VF's MAC through the physical
  port and restores it on detach (confirm in the live proof). If `managed='no'` or the MAC step is
  not valid for an interface-hostdev with a pre-bound VF, item D falls back to a plain `<hostdev>`
  with no MAC control and `identity` loses `mac`.
- `live_devices` learns the two shapes: a `<hostdev type='pci'>` with a kdive alias is `pci`, an
  `<interface type='hostdev'>` with one is `sriov_vf`; today the second would be typed `nic`, so the
  change is needed before either kind is advertised. The alias is the handle, as for `nic`.
- Provisioning: when the allocation's `pcie_claim` is non-empty and the machine is q35, the
  provisioner renders 4 spare `pcie-root-port` controllers; i440fx needs none. Systems without a
  claim render as today.
- Discovery adds `pf_bdf` to a descriptor whose node device has a `phys_function` capability. The
  nodedev address attributes are hex strings (`0x3b`), unlike the decimal elements `_compose_bdf`
  reads, so the BDF composer for it is separate. `resource_capabilities.pcie_descriptors` keeps the
  field only when it is a canonical BDF and drops it otherwise (never the entry).
- The remote authority projection counts `hostdev` (`system_authority.py:555`) and lists unknown
  device tags as `unexpected_devices`; the implementing PR confirms the comparison never runs
  against a live System that holds ledger rows, or teaches it the kdive alias.

### Errors from the port

ADR-0823's table plus `CONFIGURATION_ERROR` + `device_not_assignable`, `no_hotplug_slot` and
`accel_unsupported` (a plug the port rejects with a configuration error deletes its row only when it
returned no handle, as today). `allocation_not_active` at job start fails the job `conflict` and
moves the row to `failed`, like `system_not_ready`.

## Decisions answered

| Question (issue) | Answer | Why the alternative loses |
|---|---|---|
| How does an ADR-0068 claim bind to a System? (#3723) | The allocation's claim is the reservation; the ledger row binds a claimed device to the System; occupancy counts both | Plug-time claim needs a second claim path and the per-Resource lock from a worker |
| Who unbinds the guest driver? (#3723) | The guest kernel, on the PCIe hot-unplug request; kdive runs nothing in the guest | guest exec needs a reachable guest and a per-driver script |
| What does a failed or unacknowledged detach do to the claim? (#3723) | Row `failed`, device stays occupied; teardown or reprovision frees it | freeing on failure puts a device a guest holds into the free pool |
| Which providers advertise? (#3723) | fault-inject; local-libvirt once proven; remote-libvirt after enumeration; PowerVM #3688 | one boolean would over-claim |
| Where do per-port VF capacity and ownership live? (#3724) | Derived from descriptors with `pf_bdf` plus claim-or-row occupancy; no table | a pool table drifts from re-discovery |
| How is a free VF chosen and claimed under concurrency? (#3724) | The PF claim makes the allocation the only drawer; the System advisory lock serialises draws; a unique index backstops | drawing without a PF claim needs the per-Resource lock |
| Un-plug and reclaim when the guest holds the VF? (#3724) | The passthrough rules above; the VF stays occupied while its row exists | same as #3723 |

## Failure model

1. Actors and deployments: an MCP client holding contributor on a `READY` System whose allocation
   claimed devices; local-libvirt x86_64 on a host whose operator bound devices to `vfio-pci`;
   fault-inject; one worker per job; the reconciler and the reaper that tears down expired
   allocations.
2. Invariants: at most one ledger row names a host device, and a device is never offered to a second
   allocation while a live claim or any row names it; a passthrough row exists until the domain that
   may hold the device is destroyed or proven absent, or the provider confirms the device absent
   (an un-plug settling, or `unplug_device` on a `lost` row); a physical port is held either whole by
   one `pci` row or by `sriov_vf` rows, never both; kdive never unbinds a host driver; only claimed
   devices and the VFs of claimed ports can be plugged, and only while the allocation is live; no
   plug or un-plug overlaps power, snapshot, restore or reprovision (ADR-0823).
   A teardown that races an in-flight plug job is safe: plug needs `READY`, a late settle hits a
   deleted row, and an attach to a destroyed domain fails.
3. Accepted: a worker that dies after the provider call and before the commit leaves a `failed` row
   with no handle, found by `device_id` on retry; a device in a domain whose destroy fails stays
   occupied until an operator tears the System down; devices do not survive a power cycle; an
   allocation's own reservation can sit unused while its row is `lost`; a host without an IOMMU or
   unbound devices cannot use the feature; a VF created or removed on the host after discovery shows
   only after re-discovery; guest-side loss of a device without a hypervisor event shows as a stale
   `attached` row until the next `is_attached` poll; a `FAILED` System (for example after a failed
   reprovision) holding `lost` passthrough rows keeps the device occupied until an operator runs
   `systems.teardown`, because the orphan-teardown lane skips terminal Systems
   (`reconciler/repairs/systems.py:54-72`) and the allocation can end meanwhile.
4. Covered elsewhere: forced un-plug (#3721); PowerVM slots and logical ports (#3688); disk quota
   for scratch disks (#3720); creating VFs, binding devices to `vfio-pci` and IOMMU enablement (host
   setup, unowned until an issue names it); VF VLAN and rate settings (unowned); live migration and
   snapshot of a guest holding a device (unowned).

## Non-goals

| Not in scope | Owner |
|---|---|
| PowerVM dedicated slots and logical ports | #3688 |
| Forced un-plug | #3721 |
| Creating VFs (`sriov_numvfs`), `vfio-pci` binding, IOMMU setup | host setup; unowned until an issue names it |
| VF VLAN, MAC choice and rate limits | unowned |
| Plug-time claim of an unreserved device | operator decision on the PR; unowned if declined |
| A refusal of snapshot while a passthrough device is attached | unowned; added if the live proof shows QEMU cannot save such a guest |
| aarch64 and ppc64le passthrough on libvirt | unowned |
| Real-hardware proof on hosts this repository does not name | each implementing issue |

## Proposed implementation split

Not filed; each is one PR. A belongs to #3723, C and D to #3724, B to #3723, E to both.

| # | Title | Scope | Issue | Order | Effort | Live proof |
|---|---|---|---|---|---|---|
| A | Passthrough ledger, occupancy and tools | `pci` kind, `DeviceSpec` fields, migration, `active_claims` and availability ledger source, `allocation_not_active` check, tool parameters and refusals, reclaim split (`delete_devices_for_system` gains the `kinds` parameter; `_reclaim_devices` and `teardown_handler` pass the two kind sets; reclaim and reprovision keep the default; the implementer verifies the proof of absence at each reclaim caller), fault-inject provider, guide and RBAC docs; at the upper size bound, so the implementer may cut it into schema-and-occupancy, then tools-and-fault-inject | #3723 (shared by #3724) | 1 | L | none; fault-inject |
| B | libvirt `pci` hostdev attach | `live_devices` shapes, hostdev XML, vfio-pci driver check, free-slot check, spare root ports on q35, advertise `pci` on local-libvirt | #3723 | 2 (after A) | L | local-libvirt on an x86_64 host with an IOMMU and a device bound to `vfio-pci` |
| C | SR-IOV descriptor field and pool draw | `sriov_vf` kind and its migration, `pf_bdf` in discovery and the capability view, `occupied_bdfs` in `domain/pcie.py` called by `resolve_multiset` and availability `_free_descriptors` (with the agreement test), `sriov_vf` resolution and pool counts, the whole-port and VF mutual-exclusion refusals with their tests (a `pci` plug of a port with a VF row, an `sriov_vf` plug on a port with a `pci` row, both `no_free_device`), fault-inject VFs | #3724 | 2 (after A, parallel with B) | M | none; fault-inject |
| D | libvirt `interface type=hostdev` VF attach | interface-hostdev XML, MAC set and restore, advertise `sriov_vf` on local-libvirt | #3724 | 3 (after B and C) | M | local-libvirt on a host with an SR-IOV NIC whose VFs are bound to `vfio-pci` |
| E | remote-libvirt node-device enumeration and advertise (optional: neither Expected section needs it, and the issues carry the remote label) | node-device inventory over the TLS transport, advertise both kinds | #3723 and #3724 | 4 (after B and D) | M | remote-libvirt host with an assignable device |

Unverified until the live proof: that libvirt refuses an unbound device as stated under
`managed='no'`; that `managed` and a MAC are accepted on an interface-hostdev with a pre-bound VF;
that a hostdev detach completes through the guest's hot-unplug request; that spare `pcie-root-port`
controllers make a q35 hostdev hot-plug succeed on the QEMU version kdive boots; that vfio assignment
blocks internal snapshots; the effect of the root-port change on the NIC-slot pin at `xml.py:436`;
that a fault-inject resource accepts `pcie_devices` descriptors without a fixture change; that the
IOMMU-group rule (every peer bound to `vfio-pci` and held by one domain) is met by the operator's
setup, since no group-peer occupancy is modelled and a violation surfaces as `plug_refused` after the
row exists; and that libvirt raises the memlock limit for a hot-plugged hostdev. No lab host
with an IOMMU and an SR-IOV adapter is named; the orchestrator schedules it.
