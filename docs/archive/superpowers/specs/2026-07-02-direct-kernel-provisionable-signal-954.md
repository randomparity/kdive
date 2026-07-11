# Direct-kernel provisionability capability signal (#954)

**Status:** Draft for review
**Date:** 2026-07-02
**ADR:** [0295](../../adr/0295-direct-kernel-provisionable-signal.md)
**Depends on:** #957 / ADR-0286 (the `Capability` enum and `capability_signals` framework);
ADR-0272 (fail-closed baseline-kernel selection)

## Problem

Direct-kernel provisioning fails closed when a rootfs `/boot` holds more than one non-rescue
kernel:

> `configuration_error`: *"rootfs /boot has multiple kernels; cannot select a baseline kernel
> unambiguously"*

(`providers/local_libvirt/lifecycle/baseline_kernel.py`, `select_kernel_and_initrd`). This is
deliberate (ADR-0272): a silent wrong pick boots a dead guest that still reports `ready` — the
#905 symptom. The catalog fixture `fedora-kdive-ready-43` (a `virt-builder` debug image) trips
it; single-kernel cloud-image fixtures like `debian-kdive-ready-13` provision fine.

The failure is not the bug. The bug is that **nothing in the catalog tells a caller which
fixtures are direct-kernel-provisionable before they try.** `fixtures.list` / `images.list`
expose only `{provider, name, arch, volume}`; `images.describe` (ADR-0253/0286) surfaces the
kdump capability signal but not kernel count or direct-kernel-provisionability. Because a failed
provision consumes the Allocation (one-System-per-Allocation, ADR-0149), fixture selection is
destructive trial-and-error against a resource a wrong guess burns (the broader
allocation-consumption concern is #560, out of scope here).

## Scope

In scope:

- A build-recorded operand `provenance["boot_kernel_count"]` — the number of non-rescue
  `vmlinuz-*` kernels the built image's `/boot` holds, captured at build time with the **same**
  classification `select_kernel_and_initrd` uses.
- A `direct_kernel` `CapabilitySignal` (ADR-0286) computing provisionability from that operand
  and degrading to `unverified` when it is absent.
- Surfacing it in `images.describe` `data.capability_signals["direct_kernel"]` (automatic once
  registered) and naming it in the wrapper docstring + regenerated tool reference.
- Moving `direct_kernel_bootable` from `PLANNED_SIGNALS` to a registered `direct_kernel` signal.

Out of scope:

- Changing the fail-closed selection (ADR-0272 — this is not a "pick newest" request).
- The allocation-consumption-on-failure concern (#560).
- Annotating `fixtures.list` (see Considered & rejected).
- Any change to `images.list` (a keyset presence listing; the detail view is `describe`).

## Design

### The operand: `provenance["boot_kernel_count"]`

The honest operand is the count of provisionable kernels the image actually carries. It is
captured at build time in `LocalLibvirtRootfsBuildPlane.build`, alongside the existing advisory
captures (`package_versions`, `makedumpfile_version`):

1. A new generic seam `probe_boot_entries(qcow2) -> list[str] | None` in
   `images/planes/_build_common.py` runs a read-only `guestfish --ro -a <qcow2> -i ls /boot`
   and returns the `/boot` basenames (mirrors `probe_makedumpfile_marker`: `MISSING_DEPENDENCY`
   when guestfish is absent, `INFRASTRUCTURE_FAILURE` on timeout).
2. The plane's `_capture_boot_kernel_count(scratch)` calls the seam on the **customized scratch
   disk** (the same image the other captures inspect; its `/boot` is copied verbatim into the
   published whole-disk-ext4 qcow2 by `virt-tar-out /` → `virt-make-fs`, so its kernel set equals
   the one provision reads), classifies with `baseline_kernel_names`, and returns the count.
   Any `CategorizedError` degrades to `None` (advisory, like the makedumpfile capture).
3. `_provenance` records `record["boot_kernel_count"] = count` **only when `count is not None`**
   (an `is not None` test, not truthiness — a count of `0` is a meaningful "no bootable kernel"
   operand and must be recorded, not dropped). A degraded build's row stays byte-identical to a
   pre-feature one.

### Where the operand lands (precondition — read this before believing the value claim)

`boot_kernel_count` is only useful once it reaches the `image_catalog.provenance` column an
`images.describe` reader sees. There are two catalog-population paths, and **only one carries
provenance**:

- **`publish_image` (S3-backed) — carries provenance.** `jobs/handlers/image_build.py` (the
  `IMAGE_BUILD` job) and the private-upload path (`services/images/upload.py`) call
  `services/images/publish.py:publish_image` with `RootfsBuildOutput.provenance`, so the operand
  lands. This is where the `kdump` signal's `makedumpfile_version` already lands and computes a
  confident answer.
- **Inventory reconcile (local staged path) — drops provenance.** Local-libvirt fixtures such as
  `fedora-kdive-ready-43` are staged qcow2s registered from `systems.toml` by
  `inventory/reconcile/images.py`; `_create_entry`/`_update_entry` set
  `capabilities`/`object_key`/`volume`/`path`/`digest`/`state` but **not** `provenance`, so the row
  keeps the default `{}`. Separately, the operator CLI `images/rootfs_command.py:run_build_fs`
  computes `output.provenance` and then discards it — it moves the qcow2 to a path and prints
  `KDIVE_GUEST_IMAGE`, never persisting provenance.

**Consequence:** for a locally-staged image, `boot_kernel_count` is absent and `direct_kernel`
reads `unverified` — permanently, regardless of rebuilds. This is **identical to the existing
`kdump` signal**, which is `unverified` on the same rows for the same reason (its
`makedumpfile_version` operand also only lands on the publish/upload path). Persisting build
provenance on the local staged/reconcile path is a larger, separate change that would also refresh
`kdump`; it is out of scope here (see Considered & rejected) and worth a follow-up. This spec does
not claim to make the motivating local fixture confident today — it registers the signal and its
operand honestly, so that any provenance-carrying row (and the local path once a follow-up
persists provenance) reports a confident answer, and every other row degrades to `unverified`
rather than lying.

### Anti-drift: one classifier

`baseline_kernel.py` grows a pure `baseline_kernel_names(boot_entries) -> list[str]` — the
non-rescue `vmlinuz-*` basenames, accepting either full paths or basenames. Both
`select_kernel_and_initrd` (provision) and `_capture_boot_kernel_count` (build) classify with it,
so the recorded count predicts the provision outcome: **exactly one → provisionable** is the only
success case (zero raises "no bootable kernel", more-than-one raises "multiple kernels").

### The signal: `direct_kernel`

`images/capability_signals.py` adds `render_direct_kernel_signal(entry, target_kernel)` and
`DIRECT_KERNEL_SIGNAL` (operand `("boot_kernel_count",)`), appended to `REGISTERED_SIGNALS`. The
render is kernel-agnostic (direct-kernel-provisionability is a static image property; the
`target_kernel` argument is accepted for the uniform `SignalRender` signature and ignored). It
reads `provenance["boot_kernel_count"]`, treating a missing/non-int value (bool excluded — `bool`
is an `int` subclass) as absent:

| `boot_kernel_count` | `status` | `note` |
|---|---|---|
| absent / non-int | `unverified` | "boot kernel count is not recorded; rebuild the image to characterize direct-kernel provisionability" |
| `1` | `provisionable` | `""` |
| `0` | `not_provisionable` | "rootfs /boot has no bootable non-rescue kernel" |
| `>1` | `not_provisionable` | "rootfs /boot has N non-rescue kernels; direct-kernel selection is ambiguous and fails closed at provision" |

Block shape: `{"boot_kernel_count": <int|null>, "status": <str>, "note": <str>}`. Notes carry no
ADR reference (agent-facing surface, ADR-0270/#880).

`direct_kernel_bootable` is removed from `PLANNED_SIGNALS`; the guard
`test_planned_disjoint_from_registered_and_not_capabilities` keeps the two sets disjoint.

### Surface

`images.describe` already renders `data.capability_signals` by iterating `REGISTERED_SIGNALS`, so
the new block appears automatically. The `images_describe` wrapper docstring and the
`_capability_signals` / `_describe_envelope` docstrings are updated to name `direct_kernel`
(they currently say "today only `kdump`"), and `just docs` regenerates
`docs/guide/reference/images.md`.

## Honesty tradeoff

`direct_kernel` reads `unverified` on any row whose `provenance` lacks `boot_kernel_count` —
identical to how `kdump` reads `unverified` on rows lacking `makedumpfile_version`. This is the
ADR-0286 invariant: an un-refreshed signal is honestly non-confident, never confidently wrong. A
provenance-carrying row (built through the `publish_image` / upload path — see "Where the operand
lands") reports a confident `provisionable` / `not_provisionable`, which is what lets an agent pick
a valid fixture up front on that path. A locally-staged row reads `unverified` until a follow-up
persists build provenance on the reconcile path (the same gap `kdump` has today) — so on the local
provider this ships the honest `unverified`-not-a-lie behavior now and the confident answer when
that follow-up lands, not a false confident answer in the interim.

## Success criteria

Render-level (given a row's `provenance`):

- `images.describe` on an image whose `provenance` records `boot_kernel_count: 1` returns
  `data.capability_signals["direct_kernel"].status == "provisionable"`.
- `boot_kernel_count: 2` (or `0`) returns `not_provisionable` with an actionable note.
- A row without the operand (which is **every local-libvirt staged row today**, and any row not
  built through the publish/upload path) returns `unverified` with `boot_kernel_count: null` — the
  honest, non-lying observable the motivating local fixture reports until the reconcile-path
  follow-up lands.

Build-level (operand capture):

- A build given a `/boot` probe seam yielding two non-rescue kernels records
  `provenance["boot_kernel_count"] == 2`; a seam yielding one records `1`; a seam raising
  `CategorizedError` omits the key entirely (byte-identical to a pre-feature row).
- `baseline_kernel_names` and `select_kernel_and_initrd` agree on the same `/boot` listing (the
  recorded count equals the number of provision-time baseline candidates), so a recorded
  `provisionable` predicts a successful `select_kernel_and_initrd`.

Non-goal (explicitly not a criterion): making the locally-staged `fedora-kdive-ready-43` row report
a confident answer — that requires persisting provenance on the reconcile/build-fs path (a separate
change that also refreshes `kdump`).

## Considered & rejected

- **Annotate `fixtures.list` with the flag.** Rejected: `fixtures.list` is a bare presence
  listing (`{provider, name, arch, volume}`); the kdump capability already lives only in
  `images.describe`, and capability answers are computed per image (and would read `unverified`
  for every un-rebuilt row today). A static list annotation would duplicate the per-image
  computation and mislead. `images.describe` is the established pre-provision detail check
  (ADR-0252), and an agent describes a candidate before consuming a grant.
- **A static catalog column (`rootfs_catalog.toml`) declaring provisionability.** Rejected: a
  hand-curated write-only bit is exactly what ADR-0253 replaced for kdump — it drifts from the
  built image. The count is derived from the image the build produced.
- **Record the full `/boot` kernel list, not just the count.** Rejected: the predicate needs only
  the count (`== 1`); the list adds bytes and a redaction surface for no decision value. The
  provision-time error already enumerates candidates when it fails.
- **Probe the published whole-disk qcow2 instead of the scratch disk.** Rejected: `virt-tar-out /`
  copies `/boot` verbatim, so the scratch and published kernel sets are identical, and the scratch
  is the disk the existing `makedumpfile`/package captures already inspect — one inspection point,
  one proven code path.
- **Persist build provenance on the local staged/reconcile path in this change** (so the local
  fixture reports a confident answer immediately). Deferred, not done here: the reconcile INSERT
  (`inventory/reconcile/images.py`) and the `build-fs` CLI (`run_build_fs`) both drop provenance
  today, and closing that gap must also carry `makedumpfile_version` (else `kdump` and
  `direct_kernel` would diverge on the same rows) and decide where the operand is computed for a
  staged qcow2 the server never built. That is a larger, provider-registration change of its own;
  this spec keeps to #954's "annotate `images.describe`" ask and ships the honest `unverified`
  degrade for local rows until that follow-up lands.
- **Compute `boot_kernel_count` at reconcile time by probing the staged qcow2.** Rejected here:
  it couples the server-side reconcile loop to libguestfs and re-derives an operand at
  registration rather than at build — the same objection as the static-column alternative, one
  layer up. It belongs in the deferred reconcile-provenance follow-up, evaluated against the
  publish path, not bolted onto this signal.
