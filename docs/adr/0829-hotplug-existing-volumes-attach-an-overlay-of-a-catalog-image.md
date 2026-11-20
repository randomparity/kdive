# 0829 — Hot-plugging an existing volume attaches a kdive-owned overlay of a registered catalog image

## Status

Proposed

Design only. Implementation is split in the spec,
`docs/workflow/specs/2026-11-19-hotplug-existing-volumes-design.md`; the pull request that
implements the decision flips this status. The issue is #3722; ADR 0823 defers the feature
("pre-existing volumes as disks (needs a storage-reference model)").

## Context

`systems.plug_device(kind="disk")` takes only `size_gb` and attaches a new empty qcow2 that kdive
creates and owns (`src/kdive/mcp/tools/lifecycle/systems/devices.py:104-127`; `DeviceSpec` has no
volume reference, `src/kdive/providers/ports/lifecycle.py:382-392`). The issue asks for an agent to
attach "an existing volume it is authorized to use", for un-plug to leave a volume kdive does not
own alone, and for `release_all` to tell owned from referenced volumes. It names the questions: how
a volume is named and authorized, who owns lifecycle and deletion, how teardown distinguishes the
two, and how snapshots interact.

A search for an existing storage-reference concept finds two, and no third.

- **`image_catalog`** (`src/kdive/db/schema/0023_image_catalog.sql`): a named, qcow2-only
  (`0031_image_catalog_format_check.sql`), digest-pinned row per `(provider, name, arch)` with
  `visibility` `public` or `private` (owner project), a `registered` state, and an optional
  `volume` column naming a staged provider-pool volume (`0030_systems_inventory.sql:18-26`, ADR-0156). Provisioning already
  resolves it by name and attaches a per-System overlay over the shared base, never the base
  (ADR-0060, `_real_make_overlay`, `remote_libvirt/lifecycle/xml.py:58-74`).
- **`provider_components`** (`0009_provider_components.sql`, ADR-0065): kind-tagged references
  (`rootfs`, `kernel`, ...) with `local`, `artifact`, `component-upload` and `catalog` sources. Its
  `artifact` source is not materialized for a rootfs
  (`local_libvirt/lifecycle/provisioning.py:176-186`), and it has no disk-data kind.

There is no project-scoped storage-pool or volume registry, and the hot-plug code never names a
volume it did not create: both volume stores delete by a `kdive-<system>-dev-<device_id>` name or
prefix (`local_libvirt/lifecycle/hotplug.py:38-62`, `remote_libvirt/lifecycle/hotplug.py:98-130`).

Two more facts bound the design. The local catalog fetch resolves only `visibility = 'public'`
images (`images/rootfs/fetch.py:179-209`, wired at `rootfs_catalog_fetch.py:40-52`), so no private
image has a materialization path today. And every registered image passed the guest contract of
ADR-0093 or was operator-staged, so there is no way to register a non-bootable data image.

## Decision

**A volume is named by a catalog image name, never by a path or a pool volume name.**
`systems.plug_device(kind="disk", image="<name>")` names a `registered`, `public` `image_catalog`
row of the System's provider and architecture. The tool never accepts a host path, a storage-pool
volume name, an artifact id or a component id. A raw pool volume name would let a contributor read
any volume on the host (other Systems' overlays, capture and boot-artifact volumes, an operator's
own data); the catalog row is the existing, operator-controlled list of what may be shared.

**Authorization is the catalog's, evaluated for the System's project.** The caller needs the
existing contributor role on the System. The image must satisfy the predicate provisioning uses
to materialize a rootfs: registered, public, same provider, same arch. An image that is absent,
unregistered, of another provider or arch, or not visible all produce one `configuration_error`
with `data.reason: image_not_found` and a fixed detail, so the response is not an existence
oracle. Private images are out of this decision (see Considered & rejected); the contract is
written so widening the predicate for provisioning widens it here with no tool change.

**kdive attaches a copy-on-write overlay, never the referenced volume.** The provider creates a
qcow2 overlay of the image's base (`qemu-img create -f qcow2 -F qcow2 -b <base>` locally; a pool
volume with a `backingStore` of the staged volume remotely), and the domain attaches the overlay.
The overlay is a kdive-created device volume in the same name space as a scratch disk: `<domain>-dev-
<device_id>.qcow2` locally and `kdive-<system>-dev-<device_id>.qcow2` in the remote pool. The name
is derived from `device_id` only, never from the image name. Guest writes land in the overlay and
are discarded at un-plug; nothing writes through to the image.

**Ownership follows from that, so teardown needs no new rule.** kdive owns exactly what it
created: the overlay. The referenced image and its base file or staged volume are never opened for
write, never named in a delete call and never inside the prefix `remove` and `remove_all` match.
Un-plug, teardown, reprovision and `release_all` therefore keep their ADR-0823 behaviour and cannot
delete a referenced volume, because the volume is not attached. "Owned scratch" and "referenced"
differ only in the ledger, which records the source for display and audit.

**One port field, one port step.** `DeviceSpec` gains `disk_source: DiskSource | None`
(`name`, `digest`, `volume`; built by the worker from the resolved row, never from tool input);
`size_gb` is `None` when it is set. `DeviceVolumes` gains `create_overlay`. `DeviceHotplugger` gains
`stage_disk_source(source)`, called by the plug worker before the settle window opens, so a cold
multi-GB local fetch does not consume the 60 s settle window. It has its own 300 s limit and fails
`CONTROL_FAILURE` with `data.reason: stage_timeout`.

**The ledger records the reference.** `system_devices` gains `image text` and `source_image_id uuid`
(both null for a scratch disk and a NIC), set at admission and immutable. The worker re-resolves
`source_image_id` at job start; a row that is no longer registered and public fails the plug with
`image_not_found` and the existing "nothing attached" path deletes the row. `list_devices` reports
`image`.

**Snapshots.** An overlay is a qcow2 file, so internal snapshots (ADR-0378) keep working exactly as
they do for the System's own rootfs overlay. A snapshot taken with an overlay attached is allowed;
a restore moves the row to `lost` (ADR-0823), and un-plugging a `lost` row frees the overlay.

Tool parameters, error table, limits, the port and provider contract, the failure model and the
proposed implementation split are in the spec.

## Consequences

- No new table and no registry: the reference model is the existing image catalog plus two ledger
  columns and one migration. A deployment already controls what is shareable through
  `images.publish` and the operator staging procedure.
- The attached disk is a private writable copy. An agent cannot persist data to an existing volume
  through this surface, and cannot attach one volume to two Systems with shared writes.
- Because the catalog accepts only bootable rootfs images, the first release attaches a bootable
  image's filesystem as a second disk. A prepared non-bootable data image needs a separate
  registration path (unowned; the spec's split lists it as optional follow-on).
- Attaching the image a System booted from gives the guest two filesystems with the same UUID; the
  tool docstring says so.
- The local cache file backing an overlay is digest-keyed and not evicted by kdive code
  (`images/rootfs/fetch.py:37-49`, `:72-126` unlinks only partial downloads), so a replaced image
  does not break an attached overlay. A staged-path row or remote staged volume that an operator
  deletes or rewrites in place breaks the backing chain; that is the same exposure the System's
  own rootfs overlay has (ADR-0060) and is not new here.
- An overlay's growth is bounded by the image's virtual size, capped at 32 GB per device like a
  scratch disk, and is not charged to a quota (ADR-0823).

## Considered & rejected

- **Name a storage-pool volume (or a host path) directly.** judgment: authorization would have to
  decide per volume which of a host's volumes a project may read, with no registry to ask; the
  pool also holds other Systems' overlays and capture volumes. Name-only references to a vetted row
  remove the question.
- **Attach the referenced volume itself, with an `owned` flag in the ledger.** judgment: it makes
  `release_all` and un-plug depend on a flag staying correct across worker death, reprovision and
  restore, and a shared writable base would corrupt every other user. An overlay makes the unsafe
  state unrepresentable.
- **A new `storage_volumes` registry (project-scoped, with its own publish and expiry).** judgment:
  it duplicates `image_catalog` (visibility, owner, digest, state, retention) for a feature with no
  consumer beyond this one; filing it is the right route if non-bootable data images are wanted.
- **`provider_components` or artifacts as the reference.** judgment: the artifact source is not
  materialized for a rootfs today, and neither carries a staged remote volume.
- **Private images in the first release.** judgment: the only local fetch is public-only, so an
  authorization rule for private rows would have nothing to enforce it. When private
  materialization lands, `image_referenced_by_live_system`
  (`images/cataloging/read_model.py:20-52`) must also count ledger rows, so TTL expiry of a private
  image defers while an overlay depends on it.
- **A read-only attach of the base.** judgment: it needs a base that is never garbage collected
  while attached and a second ledger shape; the overlay is the shape provisioning already trusts.
- **Fetch the image inside `plug`.** judgment: a cold object-store download can exceed the 60 s
  settle window, which would turn a slow network into `settle_timeout` and a `failed` row.
