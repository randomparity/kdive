# Spec — Make guest disk real, agent-selectable, bounded, and observable (#985)

- **Status:** Accepted
- **Date:** 2026-07-04
- **Issue:** #985 — "Make guest disk adequate and agent-selectable for debug Systems"
- **ADR:** [0312](../../adr/0312-agent-selectable-guest-disk.md)

## Problem

An agent debugging a kernel installs a tracer toolchain at runtime (`trace-cmd`,
`bpftrace`, `gcc`, headers) as root. That needs guest disk for the packages plus
build artifacts plus a captured vmcore. The acceptance criterion: **an agent
provisions a debug System with enough free disk to install a tracer toolchain and
capture a vmcore, without operator intervention.**

Ground truth (verified in the tree, not the issue text):

- Agents **already** size disk via the ADR-0067 custom `{vcpus, memory_gb,
  disk_gb}` triple at `allocations.request`; no `platform_operator` role is
  needed. The issue's "pick a preset only" premise is outdated.
- `disk_gb` is a **phantom knob** for local-libvirt: the per-System overlay is
  created with `qemu-img create -b base overlay` and no size, so every guest is
  the base image's fixed **6 GB** (`_DEFAULT_IMAGE_SIZE`). The request is stored
  and reconciled but never reaches the disk.
- `disk_gb` has **no ceiling** (`validate_against_resource` checks only
  vcpus/memory) and is not priced.
- Custom-sized Systems report **`NULL` size** in `reports.generate` because the
  `inventory` section reads the shape catalog, not the stamped `requested_*`.

## Goals

1. Make `disk_gb` actually size the local-libvirt guest disk and grow the guest
   filesystem to fill it.
2. Bound a disk request with a host-advertised ceiling, fail-closed, matching the
   existing `≤ resource-caps` discipline.
3. Seed a curated `debug` shape (`4 vcpu / 8 GB / 60 GB`) so the common case is
   one name.
4. Make per-System size honest in the operator report for custom and shaped
   Systems alike.

## Non-goals

- Pricing disk in the kcu cost model (disk stays a capacity bound, not a price).
- The aggregate live/historic utilization view (sum of active disk/cpu/ram vs
   host capacity, windowed trends) — a follow-up on the same stamped columns.
- remote-libvirt disk sizing beyond declaring the ceiling in `systems.toml`
  (remote uses a `disk-image`, not the local overlay path).
- Auto-growing non-debug guests.

## Design

### Part 1 — Grow the overlay to `disk_gb`

`ProvisioningFiles.prepare_overlay(system_id, *, base, disk_gb)` gains `disk_gb`.
On the **create path only** (`created is True`), after `make_overlay`, it grows
the overlay to `disk_gb` with `qemu-img resize`:

- **Grow-only.** Resize runs only when the requested bytes exceed the overlay's
  current virtual size (queried with `qemu-img info --output=json`). A request at
  or below the base size is a no-op; the base size is never shrunk (qcow2 cannot
  shrink below its backing file).
- **Create-path only.** The reuse/retry path (a running QEMU holding the overlay
  open, ADR-0060) never resizes — same guard as `overlay_customizers`.
- **Idempotent.** Resizing to an absolute size is repeatable; a create-path retry
  after a partial provision converges.
- **Failure handling.** A `qemu-img resize` failure is a
  `PROVISIONING_FAILURE`; the just-created overlay is reclaimed via the existing
  `cleanup_overlay_if_created` path (a resize failure is raised before the domain
  is defined).

`disk_gb` reaches `prepare_overlay` from the profile sizing. Admission already
reconciles `requested_disk_gb` onto the profile (`services/systems/admission.py`);
the provisioner reads `profile`'s resolved `disk_gb`. When the profile carries no
`disk_gb` (a size-less profile), `prepare_overlay` is called with `None` and the
overlay keeps the base size (unchanged behavior).

### Part 2 — Grow the filesystem with cloud-init

The kdive baked drop-in (`_fedora_customize.py` `KDIVE_CLOUD_CFG_CONTENT`,
shared by all families) flips:

```
growpart: { mode: "off" }   # unchanged — no partition table (ADR-0030)
resize_rootfs: true          # was false
```

cloud-init's `cc_resizefs` grows the whole-disk ext4 across the enlarged device
at first boot. This is a **build-config** change: it takes effect when an operator
rebuilds an image with `kdive build-fs`.

Guard against a silently-disabled knob: `verify_cloud_init`'s offline self-check
(`rootfs_build.py`) asserts the baked drop-in contains `resize_rootfs: true`, so a
freshly built image cannot ship the knob off. The existing test asserting
`resize_rootfs: false` is updated to assert `true`.

A not-yet-rebuilt image grows its virtual disk (Part 1 is host-side and always
runs) but leaves the extra space unformatted until rebuilt — a documented
operator action, surfaced in the local-libvirt image/rebuild docs.

**Load-bearing assumption, to be verified before build.** The feature succeeds
only if `cc_resizefs` actually grows a *no-partition-table whole-disk ext4* under
the *fixed NoCloud `instance-id: kdive-rootfs`* and *direct-kernel* boot. Two
sub-risks: (a) cc_resizefs must handle a partition-less whole-disk device (resize
the block device directly, not a partition); (b) cc_resizefs is a per-instance
module, so it runs only if cloud-init treats the per-System guest as a new
instance — which is the *same* per-instance pass ADR-0288's host-key and DHCP
modules already depend on, so if those work, cc_resizefs runs. The first plan
task is a live spike that boots a rebuilt image on a grown overlay and records
`df` evidence that the root fs grew; that evidence is pasted into this spec before
the rest of the build proceeds. The `live_vm` growth assertion (below) is the
standing regression guard for it.

### Part 3 — Host-advertised disk ceiling

`domain/catalog/resource_capabilities.py` gains `DISK_GB_KEY = "disk_gb"`, a
`disk_ceiling()` reader (reusing `_non_negative_int`), and a
`require_disk_ceiling(resource_id, resource_name)` that fails closed with the same
host-registration-gap message as `require_size_ceiling` when absent.

`domain/accounting/cost.py` gains `validate_disk_against_resource(disk_gb,
resource)` (kept off the pricing `Selector`, since disk is not a kcu input): it
reads the ceiling and raises `configuration_error` (reusing `_caps_error`) when
`disk_gb > ceiling`. Admission (`services/allocation/admission/core.py`) calls it
right after `validate_against_resource`, using the resolved `disk_gb`. A request
with no `disk_gb` (impossible after ADR-0067's XOR rule, but defended) skips the
check.

Ceiling sources:
- **local-libvirt** — `discovery.py` advertises `disk_gb` from a **live source**:
  `shutil.disk_usage(ROOTFS_DIR).total // (1024**3)` (the storage that backs the
  per-System overlays, `/var/lib/kdive/rootfs`). This mirrors how `vcpus`/
  `memory_mb` come from live `getInfo()` — always present, so an existing local
  deployment keeps working on upgrade with no new operator action. A `ROOTFS_DIR`
  that cannot be stat-ed is a genuine host fault (`infrastructure_failure` at
  discovery), not a routine unset. **No new required env is introduced.**
- **remote-libvirt / fault-inject** — declared in `systems.toml` capabilities
  beside `vcpus`/`memory_mb`.

**Upgrade contract (non-breaking).** Because the local ceiling is live-derived
and always advertised, adding the fail-closed check does not turn a
previously-working local `allocations.request` into a hard failure on upgrade —
only a request whose `disk_gb` genuinely exceeds host storage is denied. For
remote/fault-inject, the ceiling is a `systems.toml` key the operator adds like
the existing `vcpus`/`memory_mb` keys; a host missing it fails closed with the
host-registration-gap message (matching `require_size_ceiling`), which is the
pre-existing discipline for those providers, called out in the operator docs.

### Part 4 — Honest per-System size in the report

`services/reports/sections.py` `InventorySection` reports the stamped
`requested_vcpus`/`requested_memory_gb`/`requested_disk_gb` from the System's
allocation. The `allocations` table is already joined (for `resource_kind`), so
this adds no join.

**Column contract (pinned).** The report keeps its existing column names and
units — `vcpus`, `memory_mb`, `disk_gb` — so no consumer contract changes.
`memory_mb` is computed as `requested_memory_gb * 1024`.

**Legacy NULL rows.** `requested_disk_gb` is nullable and was added in migration
0015; `requested_vcpus`/`requested_memory_gb` in 0002. Allocations granted before
those migrations carry `NULL`. To avoid trading one NULL population (custom
systems) for another (legacy systems), the columns `COALESCE` onto the
`system_shapes` catalog as a fallback: `COALESCE(a.requested_vcpus, sh.vcpus)`,
`COALESCE(a.requested_memory_gb * 1024, sh.memory_mb)`,
`COALESCE(a.requested_disk_gb, sh.disk_gb)`. The `system_shapes` `LEFT JOIN` is
retained only as this fallback. A custom-sized System (stamped, no shape) now
reports its real size; a legacy shaped System still resolves through the catalog;
a legacy custom System with no stamp and no shape reports `NULL` (unchanged, and
unavoidable — the size was never recorded).

### Part 5 — Seed the `debug` shape

Forward-only migration `0061_debug_system_shape.sql` inserts
`('debug', 4, 8192, 60)` into `system_shapes` (whole-GB memory per the 0013
`memory_mb % 1024 = 0` check). The migration-runner golden version lists gain
`0061`. Documented in the shapes/agent docs so an agent can pick `debug` by name.

## Contract / docs surface

- `allocations.request` wrapper docstring / `Field` text: note that `disk_gb`
  (custom triple or via a shape) now sizes the guest disk, bounded by the host
  disk ceiling; name the `debug` shape as the ready-sized debug preset.
- `shapes.list` docs: the `debug` preset and what it is for.
- local-libvirt operator docs: the `KDIVE_LIBVIRT_DISK_CEILING_GB` env and the
  rebuild-to-enable-resize requirement.
- Regenerate the agent-facing MCP docs (`just docs`) after wrapper/`Field`
  changes.

## Testing

- **Overlay resize** (unit, mocked `qemu-img`): grow when `disk_gb >` current;
  no-op at/below base; never on the reuse path; resize failure reclaims the
  overlay and raises `PROVISIONING_FAILURE`; idempotent create-path retry.
- **Plumbing seam** (service/integration, default gate): provision through the
  real admission→profile→provisioner seam (mock only `qemu-img` at the boundary)
  and assert `prepare_overlay` receives the `disk_gb` stamped on the allocation
  snapshot — so a wiring regression that reverts to the base size fails in the
  default gate, not only under `live_vm`.
- **Discovery ceiling** (unit): local discovery advertises `disk_gb` from a
  stubbed `disk_usage`; an un-stat-able `ROOTFS_DIR` raises
  `infrastructure_failure`.
- **cloud-init config**: `KDIVE_CLOUD_CFG_CONTENT` contains `resize_rootfs: true`
  and `growpart: {mode: "off"}`; `verify_cloud_init` self-check asserts
  `resize_rootfs: true` (update the existing `false` assertion).
- **Ceiling** (unit): `disk_gb == ceiling` admits; `disk_gb > ceiling` →
  `configuration_error` naming value + ceiling; missing ceiling →
  host-registration-gap `configuration_error`; local discovery reads the env,
  invalid env → `configuration_error`.
- **Report** (service): custom-triple System shows real vcpus/memory/disk (was
  `NULL`); shaped System shows its size via the catalog fallback; a legacy
  allocation with `NULL requested_*` and no shape reports `NULL` (documented,
  unchanged); scope/cap behavior unchanged.
- **Migration**: `0061` applies; `debug` row present with `(4, 8192, 60)`;
  golden version lists updated.
- **Live (`live_vm`, manual)**: provision a System with `disk_gb=60` on a
  rebuilt image; assert the guest root filesystem is grown (`df` ≫ 6 GB) and a
  tracer package install + a vmcore capture fit. Marked, not in the default gate.

## Rollout / rollback

- Forward-only migration (ADR-0015); no down-migration. Rollback is reverting the
  code — the `debug` row is inert if unused (shape name is a label, not an FK,
  ADR-0067).
- The overlay resize and ceiling are additive; a host with no `disk_gb` ceiling
  fails closed (operator adds the env / `systems.toml` key), which is the
  intended fail-closed behavior, called out in the operator docs.
