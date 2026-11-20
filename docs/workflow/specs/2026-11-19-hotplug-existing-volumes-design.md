# Hot-plug of pre-existing volumes as disks (#3722)

Decision: [ADR-0829](../../adr/0829-hotplug-existing-volumes-attach-an-overlay-of-a-catalog-image.md).
Builds on [ADR-0823](../../adr/0823-provider-device-hotplug-nic-and-scratch-disk.md) and its spec
`2026-11-18-provider-device-hotplug-design.md`; where this spec is silent, that spec governs.
Epic: #1380. Design only: no code, migration or test is part of this change.

## Problem

`systems.plug_device(kind="disk", size_gb=N)` attaches a new empty disk. An agent cannot give a
System a disk that already holds data (a prepared filesystem, a second distro's image). The issue
asks for a storage-reference model: how a volume is named and authorized, who owns deletion, how
`release_all` tells owned from referenced volumes, and how snapshots interact.

## Verified current behaviour

| Claim | Evidence |
|---|---|
| The only disk parameter is `size_gb`, 1 to 32 | `src/kdive/mcp/tools/lifecycle/systems/devices.py:104-127` |
| `DeviceSpec` carries `kind`, `device_id`, `network`, `size_gb` | `src/kdive/providers/ports/lifecycle.py:382-392` |
| The worker builds `DeviceSpec` from the ledger row alone | `src/kdive/jobs/handlers/devices.py:201` |
| Plug admission inserts the row and audits the arguments in one transaction | `devices.py:215-262` |
| Volume stores delete only by `<domain>-dev-<device_id>.qcow2` (local) or `kdive-<system>-dev-<device_id>.qcow2` (remote), by exact name or prefix | `src/kdive/providers/local_libvirt/lifecycle/hotplug.py:38-62`, `src/kdive/providers/remote_libvirt/lifecycle/hotplug.py:93-130` |
| The shared port treats the volume store as the `DeviceVolumes` seam (`create`, `remove`, `remove_all`) | `src/kdive/providers/shared/libvirt_hotplug.py:69-86` |
| `image_catalog` is qcow2-only, digest-pinned, `public` or owner-`private`, with an optional staged `volume` | `src/kdive/db/schema/0023_image_catalog.sql`, `0030_systems_inventory.sql:18-26`, `0031_image_catalog_format_check.sql` |
| The local catalog fetch resolves a **public** registered image only, from a staged path or a digest-keyed cache | `src/kdive/images/rootfs/fetch.py:179-230`, `src/kdive/providers/local_libvirt/lifecycle/rootfs/rootfs_catalog_fetch.py:27-52` |
| The digest-keyed cache is never evicted by kdive code | `src/kdive/images/rootfs/fetch.py:37-49`; the only `unlink` is the partial-download cleanup at `:72-126` |
| Provisioning attaches an overlay of the base, with explicit backing format | `src/kdive/providers/local_libvirt/lifecycle/storage.py:117-137` (`-f qcow2 -F qcow2 -b`), `src/kdive/providers/remote_libvirt/lifecycle/xml.py:58-74` |
| Private-image expiry defers while a live System's profile references the image; nothing else counts | `src/kdive/images/cataloging/read_model.py:20-52` |
| A private image has no local materialization path; `provider_components` artifact rootfs is "not wired yet" | `rootfs_catalog_fetch.py:40-52`, `src/kdive/providers/local_libvirt/lifecycle/provisioning.py:176-186` |

## Design

### Reference and authorization

`systems.plug_device(system_id, kind, network=None, size_gb=None, image=None, idempotency_key=None)`.

`image` is a catalog image name: 1 to 128 characters of `[A-Za-z0-9._-]`. It applies to
`kind=disk` only and is mutually exclusive with `size_gb`.

| Condition | Error | `data.reason` |
|---|---|---|
| `kind=nic` with `image` | `configuration_error` | `parameter_mismatch` (checked first, as today) |
| `kind=disk` with both `size_gb` and `image` | `configuration_error` | `parameter_mismatch` |
| `kind=disk` with neither | `configuration_error` | `invalid_size` (unchanged) |
| `image` fails the pattern | `configuration_error` | `invalid_image` |
| no registered public image of this System's provider and arch has that name | `configuration_error` | `image_not_found` |

Admission order, inside the existing per-System advisory lock and after the capability check:
role, `system_not_ready`, external-boot admission, `device_op_in_flight`, `device_limit` (all as
ADR-0823), then image resolution. Resolution is one query in a new helper
(`resolve_hotplug_image(conn, provider, name, arch)`) over `image_catalog` with
`state = 'registered' AND visibility = 'public'`, the predicate `fetch_public_provisioning_rootfs`
applies, and it returns `(id, name, digest, volume)`. The `provider` and `arch` are the System's,
never the caller's. Absent, unregistered, wrong-provider, wrong-arch and any future not-visible
case return the same `image_not_found` with one fixed detail; the tool never says which. The role
denial for a caller without contributor on the System precedes all of it, so an unauthorized caller
learns nothing about the catalog.

The tool response adds `data.image` and, for an image disk, `stage_timeout_s: 300`. The audit
`args` gain `image`. `suggested_next_actions` are unchanged.

The authorization decision is two checks: the caller may mutate this System (contributor, ADR-0823)
and the catalog offers this image to this System's project (today: public). Widening the second
check to private images is a change to the helper's predicate and to the materialization seam, not
to the tool, and is a non-goal here (below).

### Ownership and teardown

kdive attaches an overlay; the image, its base file and its staged volume are never attached. The
rules that follow, each stated so a test can check it:

1. The overlay is the only volume `plug` creates. Its name is `<domain>-dev-<device_id>.qcow2`
   (local) or `kdive-<system>-dev-<device_id>.qcow2` (remote), the existing scratch names. It is
   never derived from the image name.
2. No call kdive makes with an image-backed device opens the base or the staged volume for write or
   passes it to a delete. `remove` and `remove_all` match the overlay name and prefix only.
3. `release_all`, un-plug (including of a `lost` or `failed` row, with or without a handle),
   teardown and reprovision are unchanged from ADR-0823 and remove the overlay.
4. The ledger row is the only place that records which image an overlay came from.

Invariant tests (implementation PRs): the base file and staged volume exist, byte-identical, after
un-plug, `release_all` and a failed plug; no delete call receives a path outside the overlay
prefix.

### Port contract

```python
@dataclass(frozen=True, slots=True)
class DiskSource:
    # Catalog image name, for messages and the audit trail.
    name: str
    # The row's content digest, checked against what the provider materialized.
    digest: str
    # The row's staged pool volume (remote); None for a local object or staged path.
    volume: str | None


# DeviceSpec gains: disk only; set exactly when size_gb is None.
#     disk_source: DiskSource | None


class DeviceHotplugger(Protocol):
    # Idempotent; needs no domain.
    def stage_disk_source(self, source: DiskSource) -> None: ...
```

The worker, not the tool input, builds `DiskSource` from the re-resolved row. `DeviceVolumes` gains
`create_overlay(conn, domain_name, device_id, source) -> str` (idempotent, replaces a stale overlay
the same way `create` does) and returns the host path that `render_disk_xml` already takes. The
ADR-0823 port rules hold: a kind outside the advertised set is refused, `plug` is idempotent on
`device_id`, and `unplug` never forces.

`stage_disk_source` makes the base available and checks it, before any domain call:

- **local-libvirt:** the injected `CatalogFetch` (`rootfs_catalog_fetch_from_env`) resolves the
  public row and returns a path (a staged path validated against allowed roots, or a digest-keyed
  cache file). The port requires the row's digest to equal `source.digest`, else `configuration_error`
  `image_changed` (an operator re-published the name between admission and job). It then runs
  `qemu-img info --output=json` with the resolved tool path and requires `format = qcow2`, no
  `backing-filename`, and `virtual-size` of at most 32 GB, else `configuration_error` with
  `invalid_image_format`, `image_has_backing_file` or `image_too_large`.
- **remote-libvirt:** looks up `source.volume` in the configured pool (the `lookup_volume_staged`
  helper, ADR-0150) and requires it present (`image_not_staged`, `configuration_error`), qcow2,
  with no `backingStore` of its own (`volume_backing_path`, `remote_libvirt/lifecycle/xml.py:77`),
  and a capacity of at most 32 GB. A row with `volume is None` is `image_not_staged`: the remote
  provider has no object-store materialization (ADR-0759).

`plug` then calls `create_overlay`, which is idempotent. Local: `qemu-img create -f qcow2 -F qcow2
-b <base> <overlay>` with the virtual size inherited from the base, then `chmod` to the shared
libvirt mode as `create_scratch_qcow2` does. Remote: `pool.createXML` of a qcow2 volume whose
`backingStore` path is the staged volume's path, with the ownership of the System overlay
(`_overlay_identity`). The device XML, serial and alias are the scratch disk's: `identity` is
`{"serial": ...}`. If the domain attach fails, the existing cleanup removes the overlay
(`libvirt_hotplug.py:406-411`).

fault-inject: `stage_disk_source` is a no-op that honours `hotplug_behaviour=hang` as a
`stage_timeout`; `plug` records the source in its in-memory table; `image_not_staged` is injectable
through the existing `fault_rate` plane. No new resource capability key.

### Ledger and tools

Migration (number assigned by the implementation PR): `ALTER TABLE system_devices ADD COLUMN image
text, ADD COLUMN source_image_id uuid`, no foreign key (the row outlives a pruned catalog row only
until teardown, and a dangling id must not block image deletion), and a CHECK that
`(image IS NULL) = (source_image_id IS NULL)` and that both are null for `kind = 'nic'`. `size_gb`
stays null for an image disk; the capacity is the image's virtual size, shown as
`identity.capacity_gb` set by the worker after `plug` (a decimal string, as `identity` is a string
map). `SystemDevice` gains `image: str | None` and `source_image_id: UUID | None`.

`list_devices` adds `image` to each device (null for a scratch disk and a NIC). `unplug_device` and
the job payloads other than the plug-time spec are unchanged; `PlugDevicePayload` is unchanged
because the worker reads the row.

`plug_device_handler`, in order: read the row; re-check `READY`; if `image` is set, re-resolve
`source_image_id` with the same predicate (a miss is `image_not_found`, which the existing path
turns into a deleted row because no handle was returned); call `stage_disk_source` under its own
300 s window; then `start_device_settle_window` and the existing plug and poll. A `stage_timeout`
fails the job `CONTROL_FAILURE` with `data.reason: stage_timeout`, `waited_s`, and the row becomes
`failed` with no handle, which `unplug_device` clears.

`systems.get` and the capability model are unchanged: the `disk` kind already advertises the
feature, and a provider whose port lacks `stage_disk_source` cannot be built (the port is one
Protocol), so no new capability flag exists. The tool docstring states: the disk is a private copy
of the image; writes are discarded at un-plug; attaching the image the System booted from gives the
guest two filesystems with one UUID.

### Limit contract

| Limit | Unit | Clock | Scope | On violation | Recovery |
|---|---|---|---|---|---|
| Stage window: 300 s | seconds | worker monotonic clock from the start of the job, before the settle window opens | one plug job, covering fetch and format checks | job fails `control_failure`, `reason: stage_timeout`, `data.waited_s`; row `failed`; the fetch may complete and warm the cache | retry the plug; `unplug_device` clears the row |
| Image virtual size: 32 GB | gigabytes | none | per device | `configuration_error`, `image_too_large`, row deleted | choose a smaller image |
| 8 devices, 60 s settle, 30 s un-plug | as ADR-0823 | as ADR-0823 | as ADR-0823 | as ADR-0823 | as ADR-0823 |

The 300 s value is chosen here because the issue asks for the tool's limit contract; the
implementation PR may tune it with evidence from a live cold fetch.

### Snapshots, restore, power, reprovision

- Snapshot with an overlay attached is allowed (ADR-0823). The overlay is a qcow2 with a read-only
  backing file, the same shape as the System rootfs overlay that internal snapshots already
  cover; the backing file is never part of a snapshot and is never written.
- Restore and power OFF or CYCLE move `attached` rows to `lost`; un-plug of a `lost` row removes
  the overlay. A restore can bring back a device that was un-plugged; the overlay is then gone and
  the guest sees a missing disk. This is the ADR-0823 accepted case, unchanged.
- Reprovision and teardown call `release_all` and delete rows; the reclaim path deletes rows only
  (ADR-0823 amendment). An overlay stranded by the reclaim path, or by a worker that died between
  `create_overlay` and the handle commit, is an orphan with the scratch-disk name prefix and is
  found by the same prefix sweeps; this design adds none.

## Failure model

1. Actors and deployments: an MCP client holding contributor on a `READY` System; local-libvirt and
   remote-libvirt (and fault-inject) with a public catalog image staged or cached; x86_64 and
   ppc64le images as the catalog holds them; one worker per job; operators who publish, stage and
   remove images.
2. Invariants: the image, its base and its staged volume are never written or deleted by any plug,
   un-plug, teardown, reprovision or reclaim step; an overlay is named from `device_id` only; a
   ledger row exists exactly while an overlay may exist (ADR-0823); the tool never accepts a path,
   a pool volume name or an artifact id; an absent and an invisible image are indistinguishable to
   the caller.
3. Accepted: an operator who deletes or rewrites a staged path or a staged remote volume in place
   breaks the backing chain of overlays that use it, as for the System rootfs overlay (ADR-0060);
   a replaced catalog row does not (the local cache is digest-keyed and kept, and a remote
   operator replacing a staged volume is the same in-place rewrite); the remote staged volume is
   operator-attested, so its content digest is not re-hashed at plug time; an overlay is not
   charged to a quota (ADR-0823); the guest may see duplicate filesystem identifiers;
   a cold local fetch holds a worker for up to 300 s.
4. Covered elsewhere: forced un-plug, PCIe, SR-IOV and vFC (ADR-0823 deferrals); PowerVM (#3688).

## Trust boundary and security pass (design-time)

| Threat | Control in this design |
|---|---|
| Caller names a host path, pool volume or other System's overlay to read it | The tool accepts only a catalog name matching a fixed pattern; the name selects a DB row, and only the row's `volume` or the fetch seam's validated path reaches the provider. No caller string reaches `qemu-img` argv, XML or a libvirt lookup. |
| Cross-project read of a private image | Public images only; the predicate is evaluated for the System's provider and arch, not the caller's other projects. |
| Existence oracle for private or unregistered images | One `image_not_found` envelope and fixed detail for every miss; role denial precedes resolution. |
| Write-through corrupts a shared image | Only an overlay is attached; the base is opened read-only by the backing chain. `-F qcow2` is explicit, so qemu does not probe the base's format. |
| A base qcow2 whose header names a host file as backing, leaking it through the overlay | `stage_disk_source` rejects any base with a backing file (`image_has_backing_file`); the remote staged volume is checked the same way. Publishing is operator-controlled (ADR-0759, ADR-0093), so this is defence in depth. |
| Name injection through the overlay or serial | Overlay name and serial derive from `device_id` (a UUID), as for scratch. |
| Un-plug or `release_all` deleting a referenced volume | Unreachable by construction: invariants 1 and 2 above, with the invariant tests. |
| Time-of-check to time-of-use on image state | Admission resolves; the worker re-resolves by id and checks the digest after the fetch. A change fails the plug before any attach. |
| Resource exhaustion by repeated overlays | 8 devices per System, 32 GB virtual size per device, as scratch. Quota charging stays an unowned deferral (ADR-0823). |
| Audit gap | The audit `args` carry `image`; the ledger keeps `image` and `source_image_id` until teardown. |

## Non-goals and owners

- Private (project-owned) images as a disk source: unowned. Needs a project-aware materialization
  seam first; ADR-0829 states the retention guard change it must carry.
- Registering a non-bootable data image: unowned; needs a registration role that skips the
  guest-contract check of ADR-0093. The split lists it as optional follow-on work.
- Attaching a volume with write-through persistence, shared across Systems, or from an arbitrary
  pool volume: unowned; requires a volume registry and an ownership model this design avoids.
- Read-only attach, resize of an image disk, and boot-order or `bus` selection: unowned.
- PowerVM: #3688. The contract leaves room: `DiskSource` is provider-neutral and a provider may
  materialize it as a logical-unit clone; the PowerVM mapping is not designed here.
- Forced un-plug, PCIe, SR-IOV, vFC, CPU and memory hot-plug: ADR-0823 deferrals.

## Proposed implementation split (not filed)

| # | Title | Scope | Order | Effort | Live proof |
|---|---|---|---|---|---|
| 1 | Hot-plug image disks: contract, ledger, tool and worker | `DiskSource`, `DeviceSpec.disk_source`, `stage_disk_source`, `create_overlay` on the port and fault-inject; migration (two columns, CHECK, `SystemDevice`); `image` parameter, errors, audit, `list_devices.image`; `resolve_hotplug_image`; handler staging step; limit and docstring; invariant tests; ADR status flip | first | L | no (fault-inject proves the contract) |
| 2 | local-libvirt image-disk overlay | `LocalDeviceVolumes.create_overlay`, `stage_disk_source` over `CatalogFetch`, format and backing checks | after 1 | M | yes, local-libvirt on x86_64: plug an image disk, read data in the guest, un-plug, assert the base file unchanged; cold-cache fetch within the stage window |
| 3 | remote-libvirt image-disk overlay | `RemoteDeviceVolumes.create_overlay`, staged-volume checks, ownership copy | after 1, parallel with 2 | M | yes, remote-libvirt: plug against an operator-staged volume, un-plug, assert the staged volume intact |
| 4 | Data-image registration (optional) | a registration role for non-bootable data images skipping the guest contract; separate decision | after 2 and 3, if wanted | L | provider-dependent |

Item 1 can merge with fault-inject alone because both libvirt providers advertise the `disk` kind
with the `image` parameter refused by the port (`configuration_error`, `image_unsupported`) until 2
and 3 land; that refusal is part of item 1.
