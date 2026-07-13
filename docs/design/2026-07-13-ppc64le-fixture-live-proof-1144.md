# ppc64le profile fixture, seed baseline row, and live TCG boot proof (#1144)

Date: 2026-07-13
Status: approved (design)
Issue: #1144 · Epic: #1139 (full ppc64le support) · ADR: `docs/adr/0342-ppc64le-live-tcg-boot-proof.md`
Depends on: #1142 (ADR-0340, accel-derived domain XML), #1143 (ADR-0341, TCG deadline scaling)

## Problem

The ppc64le provisioning seam is code-complete through #1143 but has **never seen a live
boot**. Two gaps remain before the epic's vertical slice (design §Sequencing, issue 5) is real:

1. **No ppc64le fixture surface.** Only `console-ready_x86_64` exists as a profile fixture, and
   only x86_64 `[[image]]` rows are documented in `systems.toml.example`. An operator (or a live
   test) has nothing to point a ppc64le System at.
2. **PR #1070's pseries defaults are unverified.** `arch_traits["ppc64le"]` ships
   `pin_nic_slot=False`, `console_device="hvc0"`, `machine="pseries"` — the prior spec
   explicitly flagged them "needs live validation" (design §Known unverified, bullet 4). No
   boot has confirmed the SSH NIC attaches without a pinned slot or that the readiness marker
   actually lands on `hvc0` (spapr-vty).

The design (issue 5) resolves both: add the fixture + seed row, then prove one Fedora ppc64le
guest boots end-to-end under TCG on the x86_64 host — readiness marker on `hvc0`, SSH reachable
— and fold any pseries surprises back into `arch_traits` with tests.

## Inputs (already landed)

- ADR-0338 (`guest_arches` discovery): this x86_64 host advertises `ppc64le` with
  `accel="tcg"`, `emulator="/usr/bin/qemu-system-ppc64"` (verified present on the dev host).
- ADR-0339 (admission arch-validation + `systems.accel` persist): a `ppc64le` profile passes
  admission against a host advertising it, and the resolved `tcg` accel is persisted.
- ADR-0340 (accel-derived domain XML): the provisioner renders `<domain type="qemu">`, the
  discovered `<emulator>`, `machine="pseries"`, no `<cpu>`, no `<features>` for a ppc64le-TCG
  guest.
- ADR-0341 (TCG deadline scaling): the boot-readiness deadline scales by
  `KDIVE_LIBVIRT_TCG_DEADLINE_MULTIPLIER` (default 10×) when `system.accel != "kvm"`, so the
  slow TCG boot does not spuriously time out.
- `fedora-kdive-ready-44-ppc64le` already exists as a `rootfs_catalog.toml` row (build-fs
  catalog) with a sha256-pinned Fedora-secondary cloud-image URL.

## Design

### 1. Fixture surface (two sync-required copies)

The profile-fixture data lives in **two** places that must stay identical, both asserted by
tests:

- **File**: `fixtures/local-libvirt/profiles/console-ready_ppc64le.yaml` (the
  `(provider, name, arch)` triple) + a new entry in `fixtures/local-libvirt/manifest.yaml`'s
  `profiles:` list. Resolved by `load_fixture_catalog` and validated by `fixtures.validate`.
- **Embedded**: `src/kdive/admin/default_fixtures.py` — the bundle `install-fixtures` writes to
  disk. Add a `console-ready_ppc64le` profile constant and append it to the manifest
  `profiles` list built by `_manifest_yaml()`.

The profile is exactly `{provider: local-libvirt, name: console-ready_ppc64le, arch: ppc64le}`
— no `requires` block (ADR-0316/0319: profiles carry no kernel-config requirements).

### 2. Seed baseline row

Add an `[[image]]` block for `fedora-kdive-ready-44-ppc64le` to `systems.toml.example`
(reconciled into `image_catalog`), mirroring the `fedora-kdive-ready-44` x86_64 block:
`arch = "ppc64le"`, `format = "qcow2"`, `root_device = "/dev/vda"`, `visibility = "public"`,
`capabilities = ["ssh", "selinux", "kdump", "drgn"]`, `source.kind = "s3"`,
`object_key = "rootfs/local/fedora-kdive-ready-44-ppc64le.qcow2"`, and an `[image.attested]`
block (`boot_kernel_count = 1`, `makedumpfile_version = "1.7.9"`). This is example/template
inventory (the shipped seed), not a migration; `image_catalog` rows come from the operator's
`systems.toml` at reconcile time.

### 3. Rootfs preparation for the proof (arch-safe file-injection)

#8 (cross-arch package-installing customization boot) comes **after** this issue, so the full
`build-fs` foreign-arch path does not exist yet. The proof therefore prepares a bootable
"ready" ppc64le rootfs using only **arch-neutral** operations (design §Image pipeline: "file
operations are arch-safe; only executing guest code is not"):

- Start from the raw Fedora ppc64le GenericCloud qcow2 (the catalog row's pinned source).
- Inject the readiness marker unit + its script **file-level** via libguestfs (no guest-code
  execution). The readiness unit is the same one `_fedora_customize.py` renders; it writes the
  marker to `/dev/hvc0` on ppc64le (console device from `arch_traits`).
- Inject the per-System SSH bootstrap key / cloud-init for SSH reachability (arch-neutral).
- Publish the qcow2 as `rootfs/local/fedora-kdive-ready-44-ppc64le.qcow2` so the seed row
  resolves.

This is a proof scaffold, not the product image path — #8 replaces it with the real
customization boot. The scaffold steps are captured in the proof record (§5) so the run is
reproducible.

### 4. Live proof (`live_vm`)

A `live_vm`-marked test provisions and boots the ppc64le row end-to-end on the x86_64 host and
asserts:

- the System reaches `ready` under the TCG-scaled deadline;
- the readiness marker is observed on `hvc0` (spapr-vty console), **not** `ttyS0`;
- SSH is reachable to the booted guest (`ssh_reachable` probe, #972).

Marker choice: reuse the existing **`live_vm`** marker. The design assigns a distinct
`live_vm_tcg` marker to issue 15 (#15); introducing it here would be out-of-sequence scope.
The test skips cleanly when the host lacks `qemu-system-ppc64` / the published rootfs, matching
the existing `live_vm` gating.

### 5. Documented proof record

Record the actual run (console tail showing the `hvc0` marker, `ssh_reachable` result, the
resolved `accel="tcg"` + emulator, and the rootfs-prep steps) in a markdown proof note under
`docs/design/` (sibling to this spec). This is the AC's "documented live TCG boot".

### 6. Retiring the PR #1070 unverified defaults

The boot is a **falsification** step, not a rubber stamp. After the run:

- **`pin_nic_slot=False`** — confirmed if the SSH NIC attaches and the guest is reachable
  without a pinned PCI slot. If the spapr-pci-host-bridge assignment collides or SSH is
  unreachable for a NIC-address reason, that is a "pseries surprise": fix `arch_traits`
  (e.g. flip `pin_nic_slot` or add a pseries-specific slot rule) **with a test**.
- **`console_device="hvc0"`** — confirmed if the marker lands on `hvc0`. If it lands elsewhere
  (or the `console=hvc0` cmdline token is wrong for spapr-vty), correct `arch_traits` with a
  test.
- **ISA-baseline SIGILL risk (inherited from ADR-0340).** Fedora 44 ppc64le needs POWER9/ISA
  3.0; a TCG pseries guest whose default CPU model is below that baseline SIGILLs in `ld.so`
  before userspace. The design mandates **no `<cpu>` for TCG**, so the proof first tries the
  QEMU default. If it SIGILLs, the correction is folded here (documented in the proof record
  and, if it requires a rendered-XML change, a follow-up against ADR-0340's "no `<cpu>` for
  TCG" decision rather than silently pinning one) — this is the named risk #1142 deferred to
  #1144.
- Once confirmed, **drop the "unverified"/"needs live validation" language** from the
  `arch_traits` docstring and the epic design doc's §Known-unverified bullet 4, replacing it
  with a citation to this proof.

## Acceptance criteria

1. `console-ready_ppc64le.yaml` loads through `load_fixture_catalog`; `fixtures.validate`
   reports the ppc64le triple. Unit tests mirror the x86_64 fixture coverage
   (`test_default_fixtures.py`, `test_fixtures_validate.py`, `test_catalog.py`).
2. The two fixture surfaces (file bundle + embedded `default_fixtures.py`) agree, asserted by
   test.
3. `systems.toml.example` carries the `fedora-kdive-ready-44-ppc64le` `[[image]]` row; existing
   `systems.toml.example` parse/validate tests still pass with it present.
4. A documented live TCG boot of the ppc64le row passes on the x86_64 host: `ready`, `hvc0`
   marker, SSH reachable. Proof record committed.
5. Any pseries surprise (NIC, console, or CPU/ISA) is folded into `arch_traits` **with a test**;
   if none surface, the `pin_nic_slot=False` / `hvc0` defaults are confirmed and the
   "unverified" language is removed with a citation to the proof.

## Scope / non-goals

- No migration, no schema change (the seed row is example inventory; the fixture is file/embedded).
- No `build-fs` foreign-arch customization boot (#8) — the proof uses arch-safe file-injection.
- No uploaded-kernel boot / kernel-artifact contract (#6/#7) — the proof boots the rootfs's own
  baseline kernel (ADR-0272 direct boot).
- No new `live_vm_tcg` marker (#15); reuse `live_vm`.
- No kdump/fadump/gdb/drgn proof on ppc64le (issues 9/11/12) — boot + SSH only.
- No catalog parity for other families (#13).
