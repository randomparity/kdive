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
applies, and it returns `(id, name, digest, volume)`. The `provider` is the System's, and `arch` is the System's
provisioning profile `arch` (`System.requested_arch` is nullable, `domain/lifecycle/records.py:67`,
so it is not the source; the admission code reads the stored profile and a System whose profile
carries no arch is `image_not_found`); neither is ever the caller's. Absent, unregistered, wrong-provider, wrong-arch and any future not-visible
case return the same `image_not_found` with one fixed detail; the tool never says which. The role
denial for a caller without contributor on the System precedes all of it, so an unauthorized caller
learns nothing about the catalog.

The row's source columns differ by provider, and a row that lacks the one its provider needs is
`image_not_staged` at staging: local-libvirt uses the staged `path` or the `object_key` with its
`digest`; remote-libvirt requires `volume` (the remote provider has no object-store
materialization, ADR-0759). A replayed `idempotency_key` returns the first envelope whatever
`image` it names, as for every keyed mutation (`_idempotency.py:134-175`).

The tool response adds `data.image` and, for an image disk, `stage_timeout_s: 300`. The audit
`args` gain `image` and the resolved `digest`. `suggested_next_actions` are unchanged.

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

5. `stage_disk_source` refuses a base whose path or volume name matches the exact sweep patterns
   (`<domain>-dev-*.qcow2` locally, `kdive-<system>-dev-` remotely), so an operator
   mistake cannot place a catalog base inside a prefix `remove_all` sweeps.

Invariant tests (implementation PRs): the base file and staged volume exist, byte-identical, after
un-plug, `release_all` and a failed plug; no delete call receives a path outside the overlay
prefix.

### Port contract

```python
@dataclass(frozen=True, slots=True)
class DiskSource:
    # The resolved catalog row; the worker builds it, never the tool input.
    image_id: UUID
    # Catalog image name, for messages and the audit trail.
    name: str
    # The row's content digest when present, else None (a staged-path row may carry none).
    digest: str | None
    # The row's staged pool volume (remote); None for a local object or staged path.
    volume: str | None


@dataclass(frozen=True, slots=True)
class StagedDisk:
    # Local: the validated base path. Remote: the staged volume's path in the pool.
    base_path: str
    # Virtual size in bytes, read from the base; at most 32 GB.
    capacity_bytes: int


# DeviceSpec gains: disk only; set exactly when size_gb is None.
#     disk_source: DiskSource | None


class DeviceHotplugger(Protocol):
    # Idempotent; needs no domain. Raises CategorizedError on any refusal below.
    def stage_disk_source(self, source: DiskSource) -> StagedDisk: ...
```

The worker, not the tool input, builds `DiskSource` from the re-resolved row, and `plug` takes the
`StagedDisk` the same job obtained (`DeviceHotplugger.plug(domain_name, spec, staged=None)`), so
the file that was checked is the file the overlay is built on. `DeviceVolumes` gains
`create_overlay(conn, domain_name, device_id, staged) -> str` (idempotent, replaces a stale overlay
the same way `create` does) and returns the host path `render_disk_xml` already takes. The
ADR-0823 port rules hold: a kind outside the advertised set is refused, `plug` is idempotent on
`device_id` (an already-attached device ignores `staged`), and `unplug` never forces. A
`disk_source` with `staged=None`, or `staged` on a scratch disk or NIC, is a
`configuration_error`. Item 1b updates every `DeviceHotplugger` implementer: both libvirt ports,
fault-inject and the test fakes.

Worker-side refusals, all raised by the port or the handler before any attach:

| `data.reason` | Category | Row outcome |
|---|---|---|
| `image_not_found` (re-resolve miss) | `configuration_error` | deleted (nothing attached) |
| `image_changed` (digest differs from the ledger or from the fetch) | `configuration_error` | deleted |
| `image_not_staged` (no usable path, object or volume for this provider) | `configuration_error` | deleted |
| `invalid_image_format`, `image_has_backing_file`, `image_unsafe`, `image_too_large` | `configuration_error` | deleted |
| `image_unsupported` (provider has not implemented image disks; items 1b to 3 only) | `configuration_error` | deleted |
| `stage_timeout` | `control_failure` | `failed`, no handle |
| provider fault while fetching or creating the overlay | `infrastructure_failure` | `failed` |

The admission errors are in the table under Reference and authorization.

`stage_disk_source` makes the base available and checks it, before any domain call:

- **local-libvirt:** a new id-keyed fetch beside `fetch_public_provisioning_rootfs`
  (`fetch_registered_public_rootfs_by_id(conn, image_id, ...) -> (Path, digest)`), so the lookup
  the worker did and the lookup the port does are the same row, not two by-name resolutions that a
  re-publish can split. The existing fetch cannot serve: it returns only a `Path` and resolves by
  `(provider, name, arch)` (`images/rootfs/fetch.py:179-230`). When `source.digest` is not `None`
  the port requires the observed digest to equal it, else `configuration_error` `image_changed`.
  It then runs `qemu-img info -f qcow2 --output=json` (format forced, not probed) and requires no
  `backing-filename`, no `full-backing-filename`, no `data-file` or `data-file-raw` in
  `format-specific`, `encrypted` and `corrupt` false, and `virtual-size` of at most 32 GB, else
  `configuration_error` with `invalid_image_format`, `image_has_backing_file`, `image_unsafe` or
  `image_too_large`.
- **remote-libvirt:** reuses the lookup and `XMLDesc` path of `ensure_named_overlay`
  (`remote_libvirt/lifecycle/storage.py:138`, `:225`), not `lookup_volume_staged`, which reports
  only staged, absent or pool-absent (`storage.py:95`). It requires the volume present
  (`image_not_staged`), qcow2, no `<backingStore>` of its own (`volume_backing_path`,
  `remote_libvirt/lifecycle/xml.py:77`) and a capacity of at most 32 GB. libvirt volume XML does not
  expose a qcow2 `data-file`, so the content of a staged volume is operator-attested and is not
  re-hashed at plug time (Accepted, below).

`plug` then calls `create_overlay`, which is idempotent. Local: `qemu-img create -f qcow2 -F qcow2
-b <base> <overlay>` with the virtual size inherited from the base, then `chmod` to the shared
libvirt mode as `create_scratch_qcow2` does, then `qemu-img info -f qcow2` on the overlay requiring
exactly the one backing file and no `data-file`. Remote: `pool.createXML` of a qcow2 volume with an
explicit `capacity` equal to `staged.capacity_bytes` (volume XML requires one, unlike `qemu-img
create`) and a `backingStore` path of the staged volume, with the ownership of the System overlay
(`_overlay_identity`). The device XML is the scratch disk's plus an explicit `<backingStore>` built
with `append_qcow2_backing_store(..., relabel=False)` (`providers/shared/libvirt_xml.py:39-53`,
ADR-0623): without it libvirt walks the backing chain at attach and relabels the shared base, which
changes the base's label and ctime for every System that boots from it. The serial and alias are the
scratch disk's, and `identity` is `{"serial": ...}`. If the domain attach fails, the existing
cleanup removes the overlay (`libvirt_hotplug.py:406-411`).

fault-inject: `stage_disk_source` is a no-op that returns a fixed `StagedDisk`; `plug` records the
source in its in-memory table. No new resource capability key.

### Ledger and tools

Migration (number assigned by the implementation PR): `ALTER TABLE system_devices ADD COLUMN image
text, ADD COLUMN source_image_id uuid, ADD COLUMN source_digest text`, no foreign key (a dangling id
must not block image deletion; the name and digest keep the record readable after it), and CHECKs:
the three are all null or `image` and `source_image_id` are both set; they are null for
`kind = 'nic'`; and for `kind = 'disk'` exactly one of `size_gb` and `image` is set. `source_digest`
is the row's digest when it has one and null otherwise. The migration and the `SystemDevice` model
(`extra="forbid"`, read by `SELECT *`) ship in one PR and the migration must not run ahead of the
code on a rolling deploy. `size_gb` stays null for an image
disk. `SystemDevice` gains `image: str | None`, `source_image_id: UUID | None` and
`source_digest: str | None`.

`list_devices` adds `image` to each device (null for a scratch disk and a NIC). `unplug_device` and
the job payloads other than the plug-time spec are unchanged; `PlugDevicePayload` is unchanged
because the worker reads the row.

`plug_device_handler`, in order: read the row; re-check `READY`; if `image` is set, re-resolve
`source_image_id` with the same predicate (a miss is `image_not_found`, which the existing path
turns into a deleted row because no handle was returned) and, when the ledger's `source_digest` is
non-null, require the re-resolved digest to equal it, else `image_changed`; stamp `settle_deadline` at job start
plus 300 s, so `list_devices` never shows a started `plugging` row with a null deadline; call
`stage_disk_source` in a thread under a 300 s window; re-stamp `settle_deadline` to now plus 60 s
(`start_device_settle_window`) and run the existing plug and poll with the `StagedDisk`. A
`stage_timeout` fails the job `CONTROL_FAILURE` with `data.reason: stage_timeout`, `waited_s`, and
the row becomes `failed` with no handle, which `unplug_device` clears. The blocking fetch runs in a
thread, as every provider call does, so the worker's heartbeat keeps running through it. Both job
kinds keep `max_attempts=1`, so a dead worker is not redelivered; the existing
`repair_stalled_device_ops` fails the stranded row (`reconciler/repairs/systems.py:749`).

A timed-out stage call abandons its thread, which can still finish the download. Two plugs of one
cold image, or a retry racing the abandoned thread, would share the temp name
`<digest>.qcow2.partial` and a short file could be renamed into the cache under the full digest
(`images/rootfs/fetch.py:121-123`; the cache hit at `:101` is not re-verified, and the object is
read whole into memory at `:112`). That defect already exists for concurrent provisioning, but this
feature makes it reachable by a contributor, so item 2 includes a unique temp name per call, a
per-digest single-flight lock and a size check on a cache hit.

`systems.get` and the capability model are unchanged: the `disk` kind already advertises the
feature, and a provider whose port lacks `stage_disk_source` cannot be built (the port is one
Protocol), so no new capability flag exists. The tool docstring states: the disk is a private copy
of the image; writes are discarded at un-plug; attaching the image the System booted from gives the
guest two filesystems with one UUID.

### Limit contract

| Limit | Unit | Clock | Scope | On violation | Recovery |
|---|---|---|---|---|---|
| Stage window: 300 s | seconds | worker monotonic clock from the start of the job, before the settle window opens | one plug job, covering fetch and format checks; the wait is bounded, the abandoned fetch thread is not | job fails `control_failure`, `reason: stage_timeout`, `data.waited_s`; row `failed`; the fetch may complete and warm the cache | retry the plug; `unplug_device` clears the row |
| Image virtual size: 32 GB | gigabytes | none | per device | `configuration_error`, `image_too_large`, row deleted | choose a smaller image |
| 8 devices, 60 s settle, 30 s un-plug | as ADR-0823 | as ADR-0823 | as ADR-0823 | as ADR-0823 | as ADR-0823 |

The 300 s value is chosen here because the issue asks for the tool's limit contract; the
implementation PR may tune it with evidence from a live cold fetch.

### Snapshots, restore, power, reprovision

- Snapshot with an overlay attached is allowed (ADR-0823). The overlay is a qcow2 with a read-only
  backing file, the same shape as the System rootfs overlay that internal snapshots already
  cover; the backing file is never part of a snapshot and is never written. As for a scratch disk,
  this relies on a snapshot covering every qcow2 disk of the live domain; the live proof for item 2
  must take a snapshot and restore with an overlay attached, and assert the base is unchanged.
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
   operator-attested, so its content digest is not re-hashed at plug time; an overlay is charged to
   the ADR-0827 project cap at its image's virtual size (item 1a), not at its actual growth; the guest may see duplicate filesystem identifiers;
   a cold local fetch holds a worker thread to completion even after the 300 s wait gives up, and
   there is no per-project cap on concurrent staging beyond one plug job per System; deleting or
   re-publishing a catalog row does not check ledger rows, so only the guard on private-image expiry
   (ADR-0829) will ever count them; the remote staged volume's qcow2 header cannot be inspected for
   a `data-file` through libvirt; attaching the image a System booted from gives the guest two
   filesystems with one UUID, which the docstring warns about and the tool does not refuse.
4. Covered elsewhere: forced un-plug, PCIe, SR-IOV and vFC (ADR-0823 deferrals); PowerVM (#3688).

## Trust boundary and security pass (design-time)

| Threat | Control in this design |
|---|---|
| Caller names a host path, pool volume or other System's overlay to read it | The tool accepts only a catalog name matching a fixed pattern; the name selects a DB row, and only the row's `volume` or the fetch seam's validated path reaches the provider. No caller string reaches `qemu-img` argv, XML or a libvirt lookup. |
| Cross-project read of a private image | Public images only; the predicate is evaluated for the System's provider and arch, not the caller's other projects. |
| Existence oracle for private or unregistered images | One `image_not_found` envelope and fixed detail for every miss; role denial precedes resolution. |
| Write-through corrupts a shared image | Only an overlay is attached; the base is opened read-only by the backing chain. `-F qcow2` is explicit, so qemu does not probe the base's format. |
| A base qcow2 whose header names a host file as backing or as an external data file, leaking it through the overlay | Local `stage_disk_source` forces `-f qcow2` and rejects a backing file, a `data-file`, encryption and the corrupt flag, and `create_overlay` re-inspects the new overlay for exactly one backing file. Remote can check for a backing file only; its staged volume is operator-attested. Publishing is operator-controlled (ADR-0759, ADR-0093), so this is defence in depth. |
| Shared base relabelled or touched by a hot-plug | The remote image-disk XML carries an explicit `<backingStore>` with `relabel=no` (ADR-0623); the live proof asserts the base's label and ctime are unchanged. |
| Catalog base placed in a prefix `remove_all` sweeps | `stage_disk_source` refuses a base whose name matches the device-overlay pattern (invariant 5). |
| Cache poisoning through a shared temp name or a truncated cache file | Item 2 adds a unique temp name, a per-digest single-flight lock and a size check on a cache hit. |
| Name injection through the overlay or serial | Overlay name and serial derive from `device_id` (a UUID), as for scratch. |
| Un-plug or `release_all` deleting a referenced volume | Unreachable by construction: invariants 1 and 2 above, with the invariant tests. |
| Time-of-check to time-of-use on image state | Admission resolves; the worker re-resolves by id; the id-keyed fetch returns the observed digest, compared to the row's; `plug` builds the overlay on the `StagedDisk` the same job validated. A change fails the plug before any attach. A staged-path file rewritten between the checks and the attach is operator action and is accepted. |
| Resource exhaustion by repeated overlays | 8 devices per System, 32 GB virtual size per device, as scratch. The ADR-0827 project cap charges an image disk at its image's virtual size (item 1a). |
| Audit gap | The audit `args` carry `image` and the resolved digest; the ledger keeps `image`, `source_image_id` and `source_digest` until teardown, so a later re-publish does not erase which bytes a System attached. Refused admissions are not audited, as for every refusal in these tools. |

## Non-goals and owners

- Private (project-owned) images as a disk source: unowned. Needs a project-aware materialization
  seam first; ADR-0829 states the retention guard change it must carry.
- Registering a non-bootable data image: unowned; needs a registration role that skips the
  guest-contract check of ADR-0093. It is not in the split below.
- Attaching a volume with write-through persistence, shared across Systems, or from an arbitrary
  pool volume: unowned; requires a volume registry and an ownership model this design avoids.
- Read-only attach, resize of an image disk, and boot-order or `bus` selection: unowned.
- PowerVM: #3688. The contract leaves room: `DiskSource` is provider-neutral and a provider may
  materialize it as a logical-unit clone; the PowerVM mapping is not designed here.
- Forced un-plug, PCIe, SR-IOV, vFC, CPU and memory hot-plug: ADR-0823 deferrals.

## Proposed implementation split (not filed)

| # | Title | Scope | Order | Effort | Live proof |
|---|---|---|---|---|---|
| 1a | Hot-plug image disks: ledger, tool and admission | migration (three columns, CHECKs, `SystemDevice`); `image` parameter, errors, audit, `list_devices.image`, docstring; `resolve_hotplug_image`; the ADR-0827 project-cap charge for an image disk at the image's virtual size; tests | first | M | no |
| 1b | Hot-plug image disks: port, worker and fault-inject | `DiskSource`, `StagedDisk`, `DeviceSpec.disk_source`, `stage_disk_source`, `plug(..., staged)`, `create_overlay`; handler staging step and deadline stamps; fault-inject; libvirt providers refuse a `disk_source` (`configuration_error`, `image_unsupported`) until 2 and 3 land; invariant tests | after 1a | M | no (fault-inject proves the contract) |
| 2 | local-libvirt image-disk overlay | `fetch_registered_public_rootfs_by_id`; fetch hardening (unique temp name, per-digest single-flight, size check on a cache hit); `LocalDeviceVolumes.create_overlay`; `stage_disk_source`; format checks; removes the local `image_unsupported` refusal | after 1b | M | yes, local-libvirt on x86_64: plug an image disk, read data in the guest, snapshot and restore with it attached, un-plug, assert the base file byte-identical; cold-cache fetch inside the stage window |
| 3 | remote-libvirt image-disk overlay | `RemoteDeviceVolumes.create_overlay`, staged-volume checks, ownership copy, `relabel=no` backing store in the device XML; removes the remote refusal | after 1b, parallel with 2 | M | yes, remote-libvirt: plug against an operator-staged volume, un-plug, assert the staged volume, its label and its ctime unchanged |

Registering a non-bootable data image is deliberately not an item: it is a separate decision
(Non-goals), and the first release attaches a registered image's contents.
