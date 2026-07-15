# ADR 0350 — ppc64le catalog parity: rhel-family siblings, documented N/A gaps, versions mirror the sibling

- **Status:** Accepted
- **Date:** 2026-07-14
- **Issue:** #1152
- **Epic:** #1139 (full ppc64le support), sub-issue #13
- **Builds on:** ADR-0251 (declarative rootfs catalog), ADR-0345 (#1147, unified customization
  boot), ADR-0253 (computed kdump capability), ADR-0328 (computed live-drgn capability), ADR-0342
  (#1144, first ppc64le row + live TCG boot)

## Context

The rootfs catalog (`fixtures/local-libvirt/rootfs_catalog.toml`) carries one ppc64le row against
~11 x86_64 rows. The epic's design decision 4 is full parity: every x86_64 row gains a ppc64le
sibling where the distro publishes ppc64le cloud images; gaps are documented N/A, not silently
skipped.

Two facts, established empirically (mirror directory probes, 2026-07-14), shape the decision:

- **Not every distro publishes a ppc64le GenericCloud qcow2.** Fedora (secondary tree), Rocky
  9/10, and CentOS Stream 9/10 do; **Rocky 8 does not** (its `images/ppc64le/` tree is empty — Rocky
  8 is x86_64 + aarch64 only); **Debian** publishes only the `generic`/`nocloud` ppc64el variant,
  not the `genericcloud` variant its x86_64 rows pin.
- **Only the rhel family can be built cross-arch today.** ADR-0345 unified customization on a
  boot-to-self-customize mechanism, but converted only the rhel family (`customize_via = "boot"`);
  the debian family is still `customize_via = "virt_customize"` and the firstboot renderer emits
  `dnf`, so a debian image cannot be customize-booted on the x86_64 host. The debian→boot migration
  is the separate open issue #1167.

The catalog loader (`catalog.py`) already carries `arch`, a `cloud-image` `source`, and the
`makedumpfile_version` / `drgn_version` fields — a ppc64le row needs no schema change. This is a
data + tests + one-live-proof change, not a code-contract change.

## Decision

**Add sha256-pinned ppc64le siblings for the rhel-family rows whose distro publishes a ppc64le
GenericCloud qcow2, mirror the sibling's version fields, and document every gap as N/A (Rocky 8,
Debian) or a scope note (build host) in the catalog itself.**

Concretely:

- **Five new rows**, all `family = "rhel"`, `arch = "ppc64le"`, `source.kind = "cloud-image"`,
  sha256-pinned to the same release serial as the x86_64 sibling: `fedora-kdive-ready-43-cloud`,
  `rocky-kdive-ready-9`, `rocky-kdive-ready-10`, `centos-stream-kdive-ready-9`,
  `centos-stream-kdive-ready-10` each gain a `-ppc64le` sibling. (`fedora-kdive-ready-44-ppc64le`
  already exists.) They ride the existing arch-agnostic boot path unchanged — adding them requires
  **no** family-customizer edit.

- **Version fields mirror the x86_64 sibling.** `makedumpfile_version` / `drgn_version` are
  distro-repo package versions, arch-invariant within a release (one source package builds all
  arches). Each ppc64le row therefore repeats its x86_64 sibling's two values — the same principle
  `fedora-kdive-ready-44-ppc64le` already records. The computed kdump/live-drgn capabilities
  (ADR-0253/0328) fall out identically per arch.

- **N/A gaps are documented and tested.** `rootfs_catalog.toml` gets explicit N/A comments for
  Rocky 8 (no port) and Debian 12/13 (no `genericcloud` ppc64el + family blocked on #1167), plus a
  scope note for the build-host row. A loader test asserts the catalog contains **no** ppc64le row
  for the debian family or Rocky 8, so a future naive addition of an un-buildable row fails CI.

- **The build-host row is not siblinged.** `fedora-kdive-build-44` is a kernel-build toolchain; a
  ppc64le build host only matters for compiling ppc64le kernels, a lane unproven in this epic.
  Shipping it would be a speculative, unusable row — documented as a scope note, not added.

- **One live proof (non-Fedora):** `centos-stream-kdive-ready-9-ppc64le` customize-boots end-to-end
  under TCG on the x86_64 host; the other four rows are build-validated by the loader tests. If the
  EL9 customize boot fails installing `drgn` for want of EPEL (the rhel customizer enables EPEL only
  for EL8 today), the fix is to enable EPEL for **every** EL major that installs `drgn` from EPEL —
  an arch-agnostic correctness fix that also repairs the latent x86_64 EL9 rows, kept in scope as a
  quirk surfaced by the proof.

## Consequences

- The catalog reaches ppc64le parity for every distro release that both ships a ppc64le
  GenericCloud image and belongs to a cross-arch-buildable family; the rest are discoverable N/A,
  not silent omissions.
- The version-parity invariant is executable: a ppc64le row whose versions drift from its x86_64
  sibling fails the loader test.
- The debian ppc64le follow-up is tied to #1167 in the catalog, so the deferral is not lost.
- No migration, no loader-schema change, no change to any x86_64 row.

## Rejected alternatives

- **Add debian ppc64le rows now (build-validated only).** Rejected: the debian family cannot
  customize-boot cross-arch until #1167, and Debian ships only the `generic` variant (not the pinned
  `genericcloud`) — the row would be un-buildable on the only available host and would silently
  diverge the base variant. Deferred to #1167 with an N/A pointer.
- **Pull the debian→boot migration (#1167) into this issue.** Rejected: #1167 is a separate open
  issue with its own ADR surface (apt firstboot rendering, argv-path deletion); folding it in
  couples catalog parity to a mechanism change and doubles the blast radius.
- **Add a ppc64le build-host sibling for strict "every row" parity.** Rejected as speculative: the
  ppc64le build lane is unproven, so the row would be unusable. Documented scope note instead.
- **Silently skip Rocky 8 / Debian.** Rejected: the acceptance criterion requires N/A gaps
  enumerated in the catalog; silent omission is the anti-pattern this issue closes.
- **Independently re-verify each ppc64le row's `makedumpfile`/`drgn` version.** Rejected as
  redundant: distro package versions are arch-invariant within a release, so mirroring the x86_64
  sibling (already verified against package indexes) is correct; the parity test guards it.
- **Prove all five rows live under TCG.** Rejected: one non-Fedora end-to-end proof meets the
  acceptance criterion; TCG boots are slow, and the loader tests build-validate the rest. Additional
  live proofs are gated follow-ups, consistent with the rest of the epic.

## Rollout

Additive and backward compatible: five new catalog rows, catalog comments, tests, and one proof
record. No migration and no change to any existing row or to the loader. A possible narrow EL9 EPEL
fix (if the proof forces it) is arch-agnostic and repairs a latent x86_64 gap.
