# ADR 0312 — Make guest disk real, agent-selectable, bounded, and observable

- **Status:** Accepted
- **Date:** 2026-07-04
- **Deciders:** kdive maintainers
- **Issue:** #985 (reframed; `OPUS_REVIEW.md` §5 item I-5, Tier 2)
- **Spec:** [agent-selectable-disk-985](../superpowers/specs/2026-07-04-agent-selectable-disk-985-design.md)
- Extends ADR-0067 (system-shape catalog + custom sizing triple), ADR-0007 §2
  (≤ resource-caps admission check), ADR-0030 (whole-disk ext4 rootfs layout),
  ADR-0060 (overlay reuse-on-retry), ADR-0288 (cloud-init first boot). Supersedes
  nothing. Files the aggregate live/historic utilization view as a follow-up.

## Context

A debug workflow customizes the guest at runtime — the agent installs a tracer
toolchain (`trace-cmd`, `bpftrace`, `gcc`, kernel headers) as root — rather than
shipping more rootfs images. That only works if the guest has disk for those
packages *alongside* build artifacts and a captured vmcore. The filed issue read
the gap as a permission problem: `disk_gb` lives on operator-owned shape presets
and `shapes.set` is `platform_operator`, so "a project agent can only pick a
preset, not size one."

Ground truth is different in two ways:

1. **An agent already can size disk.** ADR-0067 added a full-custom
   `{vcpus, memory_gb, disk_gb}` triple to `allocations.request`, resolved to the
   at-grant `requested_disk_gb` snapshot. No `shapes.set` role is required. The
   "pick a preset only" framing predates that triple.

2. **`disk_gb` is a phantom knob for local-libvirt.** The per-System overlay is
   created with `qemu-img create -b base overlay` and **no size argument**
   (`lifecycle/storage.py`), so the overlay inherits the base image's fixed 6 GB
   virtual size (`rootfs_build.py` `_DEFAULT_IMAGE_SIZE = "6G"`). Whatever
   `disk_gb` an agent requests — shape or custom triple — is stored, priced-past
   (disk is not a cost input), and reconciled onto the profile, but never reaches
   the guest disk. Every local guest is 6 GB.

Two further gaps compound it:

- **No disk ceiling.** `validate_against_resource` (ADR-0007 §2) checks only
  `vcpus` and `memory_mb` against the host's advertised size ceiling. `disk_gb`
  has no ceiling and is not priced, so a real disk knob would be unbounded — an
  agent could request a disk larger than host storage.
- **Custom sizing is invisible in reporting.** The `reports.generate`
  `inventory` section reads size via `LEFT JOIN system_shapes ON name = s.shape`,
  so a custom-triple System (no shape name) reports `NULL` vcpus/memory/disk. An
  operator cannot see what a custom-sized System actually holds.

The acceptance criterion — an agent provisions a debug System with enough free
disk to install a tracer toolchain and capture a vmcore, without operator
intervention — is therefore unreachable today regardless of the knob or the role.

## Decision

Make the existing `disk_gb` knob real end-to-end for local-libvirt, bound it with
a host-advertised ceiling that matches the existing `≤ resource-caps` discipline,
seed a curated `debug` shape so the common case is one name, and make per-System
size honest in the operator report. Four parts.

1. **The overlay is grown to `disk_gb`.** `prepare_overlay` receives the
   resolved `disk_gb` and, **only on the create path** (never the reuse/retry
   path — a running QEMU holds the overlay open, ADR-0060), grows the overlay's
   virtual size to `disk_gb` with `qemu-img resize` after creation. The resize is
   **grow-only**: it runs only when `disk_gb` exceeds the overlay's current
   virtual size, so a request at or below the base size is a no-op and the base
   size is never shrunk (qcow2 cannot shrink below its backing file). Resizing to
   an absolute size is idempotent under a create-path retry.

2. **cloud-init grows the filesystem to fill the disk.** The rootfs is a
   no-partition-table whole-disk ext4 (ADR-0030), so cloud-init's `cc_resizefs`
   (`resize_rootfs`) grows the ext4 across the whole enlarged device at first
   boot; `growpart` stays off (there is no partition to grow, ADR-0288). The
   baked kdive drop-in flips `resize_rootfs: false → true` (keeping
   `growpart: {mode: "off"}`). The change lands in the **build config**
   (`_fedora_customize.py`, shared by every family), so it takes effect when an
   operator rebuilds an image with `kdive build-fs`. The existing
   `verify_cloud_init` build self-check is extended to assert
   `resize_rootfs: true`, so a freshly built image cannot silently ship the disk
   knob disabled. The rebuild requirement is documented; a not-yet-rebuilt image
   grows its virtual disk but leaves free space unformatted (a documented
   operator action, not a silent runtime failure).

3. **A host-advertised `disk_gb` ceiling bounds the request.** Resource
   capabilities gain a `disk_gb` key alongside `vcpus`/`memory_mb`; admission's
   `≤ resource-caps` check rejects `disk_gb > ceiling` as a `configuration_error`
   naming the requested value and the ceiling, exactly like the vcpus/memory
   over-cap path. local-libvirt advertises the ceiling from a new
   `KDIVE_LIBVIRT_DISK_CEILING_GB` operator env (mirroring
   `KDIVE_LIBVIRT_ALLOCATION_CAP`); remote-libvirt and fault-inject declare it in
   `systems.toml` like the existing size keys. A host that advertises no disk
   ceiling fails closed with a host-registration-gap message, matching
   `require_size_ceiling`.

4. **The operator report shows real per-System size.** The `inventory` section
   reads the authoritative stamped `requested_vcpus`/`requested_memory_gb`/
   `requested_disk_gb` from the System's allocation instead of the shape catalog,
   so every System — custom or shaped — reports its true size. This is the
   per-System-current slice of the usage-observability ask; the aggregate
   live/historic utilization view (sum of active disk/cpu/ram vs host capacity,
   windowed trends) is a separate follow-up built on the same stamped columns.

A curated `debug` shape (`4 vcpu / 8 GB / 60 GB`) is seeded by migration 0061 and
documented, so the common debug case is one name rather than a computed triple.

## Consequences

- The existing `disk_gb` knob (shape or custom triple) stops being a phantom: a
  local guest boots with a disk sized to the request and a filesystem grown to
  fill it. The acceptance case (toolchain install + vmcore capture, no operator
  intervention) is reachable through the already-authorized `allocations.request`
  path.
- disk is bounded per-request by a host-advertised ceiling; an over-ceiling
  request fails closed at admission with a diagnostic, and a host missing the
  ceiling fails closed rather than admitting an unbounded disk.
- Operators see real per-System size in `reports.generate`, including
  custom-sized Systems that previously reported `NULL`.
- Rebuilt images gain the disk knob; the build self-check guarantees a fresh
  image ships it. An operator running a stale image sees a grown virtual disk
  with unformatted free space until they rebuild — documented, and guarded
  against on the build path so new images are always correct.
- disk remains outside the kcu cost model (unchanged); the ceiling is a capacity
  bound, not a price. Pricing disk is out of scope.

## Considered & rejected

- **Seed a generous debug shape only (issue option 1), no plumbing.** Does not
  work: the shape's `disk_gb` is the same phantom knob, so the guest stays 6 GB.
  Rejected — it documents a capability that is not implemented.
- **Give the guest FS-grow its own in-guest step (offline `resize2fs` at
  provision, or a bespoke first-boot unit).** cloud-init's `cc_resizefs` already
  does exactly this and already runs at first boot (ADR-0288); adding a second
  mechanism is redundant and a second thing to keep correct. Rejected — re-enable
  the one already present.
- **Write the `resize_rootfs` flip into the per-System overlay at provision (via
  the `overlay_customizers` virt-customize seam) so existing images work without
  a rebuild.** Viable, but adds a per-provision `virt-customize` pass on the hot
  path and a second place the resize policy lives. The build config is the single
  source of truth for image first-boot behavior (ADR-0288); a rebuild is the
  operator's existing image-lifecycle action. Rejected in favor of the build-time
  flip plus the self-check guard.
- **A fixed global `MAX_REQUESTABLE_DISK_GB` constant instead of a host ceiling.**
  Not host-aware — over-commits a small host and under-serves a large one, and
  diverges from the existing per-host `≤ resource-caps` model. Rejected.
- **Add `disk_gb` to the pricing `Selector` to enforce the ceiling there.**
  Pollutes the cost model with a non-priced dimension (disk is not a kcu input).
  Rejected — the ceiling check takes `disk_gb` as a separate argument beside the
  priced selector.
- **Ship the aggregate live/historic utilization view in this change.** A
  distinct observability feature (a new windowed report section aggregating
  active consumption per host/project); the stamped `requested_*` columns already
  hold the data. Rejected here to keep #985 scoped; filed as a follow-up.
- **Auto-grow every guest to a large default.** Wastes host storage for
  non-debug Systems and hides the sizing decision. Rejected — sizing stays an
  explicit request (shape or triple).
