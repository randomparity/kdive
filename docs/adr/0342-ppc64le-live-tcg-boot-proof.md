# ADR 0342 — ppc64le profile fixture, seed baseline row, and live TCG boot proof

- **Status:** Accepted <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-07-13
- **Issue:** #1144
- **Epic:** #1139 (full ppc64le support)
- **Builds on:** ADR-0338 (`guest_arches` discovery), ADR-0339 (admission arch-validation +
  `systems.accel` persist), ADR-0340 (accel-derived domain XML), ADR-0341 (TCG deadline scaling),
  ADR-0272 (bootloader-less baseline-kernel direct boot), ADR-0112 (`systems.toml` inventory)

## Context

The ppc64le provisioning seam is code-complete through #1143 but unproven: no ppc64le guest has
ever booted, and PR #1070's pseries `arch_traits` defaults (`pin_nic_slot=False`,
`console_device="hvc0"`, `machine="pseries"`) were shipped flagged "needs live validation." The
epic's vertical-slice midpoint (design issue 5) requires one Fedora ppc64le guest to boot
end-to-end under TCG on the x86_64 dev host, retiring those unverified defaults.

Two facts constrain how the proof is built:

1. **#8 (cross-arch package-installing customization boot) comes after this issue.** The real
   `build-fs` foreign-arch image path does not exist yet, so the proof cannot depend on it to
   produce a "kdive-ready" ppc64le rootfs.
2. **The pseries defaults are assumptions, not proofs.** The boot must be allowed to *falsify*
   them, not merely pass — a NIC that needs a pinned slot, a marker that lands off `hvc0`, or an
   ISA-baseline SIGILL under TCG (the risk ADR-0340 deferred here) are the expected discoveries.

## Decision

**Fixture surface — mirror x86_64 across both copies.** Add
`console-ready_ppc64le.yaml` (`{provider: local-libvirt, name: console-ready_ppc64le, arch:
ppc64le}`, no `requires` block per ADR-0316/0319) as (a) a file under
`fixtures/local-libvirt/profiles/` listed in `manifest.yaml`, and (b) an embedded constant in
`admin/default_fixtures.py` appended to the `_manifest_yaml()` profiles list. Both are asserted
identical by test, so the `install-fixtures` bundle and the file-based catalog cannot drift.

**Seed baseline row — example inventory, not a migration.** Add a
`fedora-kdive-ready-44-ppc64le` `[[image]]` block to `systems.toml.example`, mirroring the
x86_64 `fedora-kdive-ready-44` block (`arch = "ppc64le"`, `s3` source
`rootfs/local/fedora-kdive-ready-44-ppc64le.qcow2`, `[image.attested]` `boot_kernel_count = 1`,
`makedumpfile_version = "1.7.9"`). `image_catalog` rows are reconciled from the operator's
`systems.toml` (ADR-0112); the example file is the shipped seed template, so no schema change.

**Proof rootfs — arch-safe file-injection scaffold.** The proof prepares the ppc64le rootfs
from the raw Fedora ppc64le GenericCloud qcow2 using only **arch-neutral** operations
(libguestfs file injection of the readiness unit + SSH bootstrap/cloud-init), publishes it under
the seed row's `object_key`, and boots it via the existing ADR-0272 baseline-kernel direct boot.
This is a documented, reproducible scaffold for the proof only — **not** a product image path;
#8 replaces it with the real customization boot. Executing guest code is arch-unsafe on a
foreign host, so no packages are installed; file writes are arch-safe, which is all the
readiness marker + SSH key need.

**Proof test — reuse `live_vm`.** A `live_vm`-marked test provisions and boots the ppc64le row
and asserts: `ready` under the TCG-scaled deadline, readiness marker on `hvc0` (spapr-vty), SSH
reachable. It skips cleanly without `qemu-system-ppc64` or the published rootfs, matching
existing `live_vm` gating. The distinct `live_vm_tcg` marker is issue 15's scope, not here.

**Falsification of the PR #1070 defaults.** The boot is a falsification gate. If
`pin_nic_slot=False`, `console_device="hvc0"`, or the no-`<cpu>`-for-TCG rendering proves wrong
against the real boot, the fix is folded into `arch_traits` **with a test** and recorded in the
committed proof note; a CPU/ISA correction that needs a rendered-XML change is raised as a
follow-up against ADR-0340's "no `<cpu>` for TCG" decision rather than silently pinning one.
If the defaults hold, the "unverified"/"needs live validation" language is removed from the
`arch_traits` docstring and the epic design's §Known-unverified bullet 4, cited to this proof.

## Consequences

- The epic's vertical slice is real: one ppc64le guest boots under TCG on the x86_64 host, and
  the three PR #1070 pseries defaults are either confirmed or corrected with a test, not carried
  as unproven assumptions.
- An operator gains a documented ppc64le profile + image seed to copy into their `systems.toml`.
- The proof rootfs is a scaffold; until #8 lands, a ppc64le System still cannot be produced by
  the normal `build-fs` customization path — the scaffold's steps are documented so the proof is
  reproducible, and the limitation is named, not hidden.
- No migration, no schema change; the fixture and seed are file/embedded/example artifacts.

## Alternatives considered

- **Drive the real `build-fs` foreign-arch customization for the proof rootfs.** Rejected: that
  path is #8, which depends on this issue — using it here inverts the epic sequencing and pulls
  package-installing customization-boot machinery into a fixture/proof PR.
- **Introduce the `live_vm_tcg` marker now.** Rejected: marker/suite wiring is issue 15's
  explicit scope; splitting it here would leave a half-wired marker matrix.
- **Assert the pseries defaults are correct without a live boot (unit test only).** Rejected:
  that is exactly the unverified-assumption posture PR #1070 shipped and this issue exists to
  retire — only a real boot falsifies `pin_nic_slot` / `hvc0` / the TCG CPU default.
- **Ship the ppc64le seed row as a migration / in-code default.** Rejected: ADR-0112 moved all
  image definitions out of code into `systems.toml`; the example file is the correct seed
  surface, and a migration would reintroduce the in-code inventory ADR-0112 removed.
- **Pin a TCG `<cpu>` model pre-emptively to dodge the ISA-baseline SIGILL.** Rejected here:
  ADR-0340 decided no `<cpu>` for TCG; the proof tries the QEMU default first and only revisits
  that decision (as a cited follow-up) if the boot actually SIGILLs — evidence before change.
