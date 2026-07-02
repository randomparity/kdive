# ADR 0295 — Direct-kernel provisionability as a computed capability signal

- **Status:** Accepted
- **Date:** 2026-07-02
- **Deciders:** kdive maintainers

## Context

Direct-kernel provisioning boots a rootfs's own `/boot` kernel, extracted host-side
(ADR-0030/0272). `select_kernel_and_initrd` picks the baseline kernel fail-closed: it excludes
rescue images and raises `CONFIGURATION_ERROR` on zero or more-than-one non-rescue `vmlinuz-*`
rather than guessing a version order (a silent wrong pick boots a dead guest that still reports
`ready` — the #905 symptom). That fail-closed behavior is correct and stays.

The problem (#954) is discoverability: nothing in the catalog tells a caller which fixtures are
direct-kernel-provisionable before they try. `fixtures.list` / `images.list` expose only
`{provider, name, arch, volume}`; `images.describe` surfaces the kdump capability signal
(ADR-0286) but not kernel count. The multi-kernel `fedora-kdive-ready-43` `virt-builder` debug
image fails at provision, while single-kernel cloud images provision fine — and because a failed
provision consumes the Allocation (one-System-per-Allocation, ADR-0149), fixture selection is
destructive trial-and-error. ADR-0286 already recorded `direct_kernel_bootable` as a
`PLANNED_SIGNAL` (#954) precisely because "direct-kernel provisionability is discovered only by
failure" — no honest per-image operand existed yet.

See `docs/superpowers/specs/2026-07-02-direct-kernel-provisionable-signal-954.md`.

## Decision

Register a `direct_kernel` computed capability signal over a new build-recorded operand, following
the ADR-0286 framework and its degrade-to-unverified invariant.

- **Operand: `provenance["boot_kernel_count"]`.** The build enumerates the built image's `/boot`
  and records the number of non-rescue `vmlinuz-*` kernels. It is captured in
  `LocalLibvirtRootfsBuildPlane.build` as an advisory step beside `makedumpfile_version` /
  `package_versions`: a generic read-only `guestfish ... ls /boot` seam
  (`_build_common.probe_boot_entries`) lists `/boot`, and `_capture_boot_kernel_count` classifies
  it. Any capture failure degrades to `None` and the key is omitted, so a degraded build's row is
  byte-identical to a pre-feature one. The key is written when the count `is not None` — a count
  of `0` is a meaningful "no bootable kernel" operand and is recorded, not dropped as falsy.

- **One classifier, no drift.** `baseline_kernel.py` exposes a pure
  `baseline_kernel_names(boot_entries)` — the non-rescue `vmlinuz-*` basenames. Both
  `select_kernel_and_initrd` (provision) and the build-time count classify with it, so the
  recorded count predicts the provision outcome: exactly one is the only provisionable case.

- **Signal: `direct_kernel`.** A `CapabilitySignal` (operand `("boot_kernel_count",)`) whose
  render reads the operand and returns
  `{"boot_kernel_count": <int|null>, "status": <str>, "note": <str>}`: `provisionable` for a count
  of 1, `not_provisionable` (with an actionable note) for 0 or >1, and `unverified` (with a
  rebuild note, `boot_kernel_count: null`) when the operand is absent or not an int. The render is
  kernel-agnostic — direct-kernel-provisionability is a static image property — but takes the
  uniform `SignalRender(entry, target_kernel)` signature and ignores the kernel. Notes carry no
  ADR reference (agent-facing, ADR-0270).

- **Surfaced by `images.describe`.** `data.capability_signals` iterates `REGISTERED_SIGNALS`, so
  the block appears automatically once registered; the `images_describe` wrapper docstring and the
  generated tool reference name it. `direct_kernel_bootable` is removed from `PLANNED_SIGNALS`.

- **Placement mirrors kdump.** The signal lives only in `images.describe`, not `fixtures.list` —
  the same placement the kdump signal already has. `describe` is the established pre-provision
  detail check (ADR-0252) an agent consults before consuming a grant.

No new MCP tool, RBAC change, schema/migration, or config change. Tool visibility is unchanged.

## Consequences

- An agent can read `images.describe` `data.capability_signals["direct_kernel"]` to pick a
  direct-kernel-provisionable fixture up front instead of burning an Allocation to discover the
  failure.
- Every image built before this feature reads `unverified` (no operand) until rebuilt — identical
  to how `kdump` degraded before `makedumpfile_version` was recorded. Un-refreshed metadata is
  honestly non-confident, never confidently wrong (the ADR-0286 invariant). The catalog's staged
  fixtures gain a confident answer as they are rebuilt.
- The build gains one advisory libguestfs read; it degrades to omitting the operand on any
  failure, so it never fails a build. Unit tests inject the seam and cover the
  orchestration/provenance contract; a live build recording the operand end-to-end is the
  operator-run live-stack path (the ADR-0285 stance).
- `select_kernel_and_initrd` and the build share one baseline-kernel classifier, so the recorded
  count cannot drift from the provision-time selection rule.

## Alternatives considered

- **Annotate `fixtures.list` (and/or `images.list`) with the flag.** Rejected: those are bare
  presence listings; the kdump signal is `describe`-only for the same reason. A per-row capability
  answer is computed and would read `unverified` for every un-rebuilt row today; putting it in the
  list duplicates the computation and invites the static-vs-computed drift ADR-0286 removed.
- **A hand-curated static column in `rootfs_catalog.toml`.** Rejected: a write-only bit is exactly
  what ADR-0253 replaced for kdump; it drifts from the built image. The count is derived from the
  image the build produced.
- **Compute provisionability from a correlation (kind/source).** Rejected: "virt-builder debug
  images have multiple kernels" is a correlation, not a guarantee (a debug cloud image could add a
  second kernel; a virt-builder image could ship one). The honest operand is the actual count.
- **Record the whole `/boot` kernel list.** Rejected: the predicate needs only `count == 1`; the
  list adds bytes and a redaction surface for no decision value.
- **Probe the published whole-disk qcow2 rather than the scratch disk.** Rejected: `virt-tar-out
  /` copies `/boot` verbatim, so the kernel sets are identical, and the scratch disk is the one the
  existing captures already inspect — one proven inspection path.
