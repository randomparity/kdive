# Full ppc64le support in the local-libvirt provider

Date: 2026-07-13
Status: approved (design)
Epic: tracked as a GitHub epic with one sub-issue per PR (numbers recorded on the epic).

## Goal

The local-libvirt provider runs both x86_64 and ppc64le guests on either host
architecture: native guests under KVM, foreign-arch guests under TCG emulation. The full
System lifecycle works for a ppc64le guest — provision, boot an uploaded kernel,
force-crash, kdump capture and retrieve, drgn/gdb debug — with live proof under TCG on
the existing x86_64 host. Native validation on a POWER10 host is a gated follow-up.

## Prior work

PR #1070 (`feat/ppc64le-enablement`) made `just ci` green on a ppc64le Ubuntu host and
added the first provisioning seams:

- `domain/platform/arch_traits.py` — per-arch table (machine `q35`/`pseries`, console
  `ttyS0`/`hvc0`, NIC slot pinning), consumed by the domain-XML renderer, the required
  cmdline in `services/runs/steps.py`, and the readiness unit in
  `images/families/_fedora_customize.py`.
- One sha256-pinned ppc64le catalog row (`fedora-kdive-ready-44-ppc64le`).
- Per-arch qemu hints in `scripts/check-setup-deps.sh`.

Its spec (`docs/archive/superpowers/specs/2026-07-09-ppc64le-disk-image-arch-seam-design.md`)
deferred: cross-arch execution, the kernel-artifact contract, image customization for
ppc64le, kdump on POWER, the debug plane, and any live proof. This design covers that
remainder.

## Decisions

1. **Guest-arch capability: auto-discover, auto-allow.** Discovery probes libvirt for
   bootable guest arches; every discovered arch is schedulable. No operator opt-in for
   TCG.
2. **Crash capture: kdump and fadump.** kdump is the spine (works under KVM and TCG);
   fadump is a profile opt-in with a QEMU feature floor.
3. **Validation hardware: x86_64 host only for now.** Live proof means ppc64le guests
   under TCG on the x86_64 host. POWER10-native proof is a gated follow-up sub-issue.
4. **Image scope: full parity with the x86_64 catalog.** Every x86_64 row gains a
   ppc64le sibling where the distro publishes ppc64le cloud images; gaps are documented
   as N/A, not silently skipped.
5. **Cross-arch image customization: customization boot under TCG.** Package-installing
   customization of a foreign-arch image runs inside the image itself via a one-shot
   firstboot payload, booted once under TCG. ~~virt-customize remains the native-arch
   path.~~ **Superseded by [ADR-0345](../adr/0345-unified-customization-boot.md) (#1147):**
   the boot method is unified for *every* arch and family and `virt-customize` execution is
   retired — a foreign-only path beside `virt-customize` left two customization methods to
   maintain, and the boot method extends to bare-metal installs and developer custom setup.
6. **Sequencing: vertical slice first.** Issues 1–9 prove one ppc64le guest end-to-end
   under TCG; later issues broaden (fadump, catalog parity, gdb/drgn, host symmetry,
   POWER10).

## Design

### Arch capability and admission

- `providers/local_libvirt/discovery.py` currently advertises only the host arch
  (`parse_capabilities_arch`). It gains a parser for the capabilities XML `<guest>`
  blocks producing, per bootable guest arch: the best accelerator (`kvm` when the arch
  is native and KVM is present, else `tcg`) and the emulator path libvirt reports.
  Stored in the Resource capabilities as a `guest_arches` mapping and flowed through
  the existing inventory writeback.
- Systems admission validates `profile.arch ∈ resource.guest_arches`, failing with
  `CONFIGURATION_ERROR` naming the supported set (same fail-fast rule as
  `arch_traits()` — never a silent x86 fallback).
- The resolved accelerator is persisted on the System row at provision time and surfaced
  in `systems.get`, so timeout scaling, cost accounting, and tests key off a recorded
  fact rather than re-deriving host state.

### Provisioning

- `lifecycle/xml.py` derives `<domain type=…>` from the resolved accelerator (`kvm` /
  `qemu`) and emits `<emulator>` from the discovered path instead of relying on the
  libvirt default.
- CPU element by arch × accel: the existing x86-under-KVM block is unchanged;
  pseries-under-KVM uses `host-model`; TCG domains emit no CPU element (QEMU's
  per-machine default is correct and pinning a model couples us to QEMU versions).
- The ACPI `<features>` block stays x86-only (routed through arch traits). The pseries
  fw_cfg/VMCOREINFO behavior — deliberately left unverified by the prior spec — is
  proven or corrected empirically in the kdump sub-issue.
- Provision/boot/install readiness deadlines gain one accel multiplier in the
  local-libvirt provider settings (default ~10× for TCG, operator-tunable), applied
  where deadlines are computed.

### Image pipeline

- Native-arch builds keep the virt-customize path in
  `providers/local_libvirt/rootfs_build.py` unchanged.
- Foreign-arch builds use a customization boot: family customization is rendered as a
  one-shot firstboot unit and injected file-level via libguestfs (file operations are
  arch-safe; only executing guest code is not). kdive boots the image once under TCG
  through its own provisioning machinery — the existing bootloader-less baseline-kernel
  direct boot (ADR-0272), so no bootloader or firmware work exists anywhere in this
  epic — waits for a completion marker on the console under the TCG-scaled deadline,
  powers off, and seals. Failures surface the console tail through the normal evidence
  path.
- Family customizers (`images/families/_fedora_customize.py`, `rhel.py`) refactor their
  customization into a form renderable as either virt-customize argv or a firstboot
  script, so per-family logic exists once.
- Catalog parity: ppc64le siblings for every x86_64 row whose distro publishes ppc64le
  cloud images (Fedora, CentOS Stream/RHEL, Debian, Ubuntu do), sha256-pinned, with
  makedumpfile/drgn version fields. Fixture surface completed by
  `fixtures/local-libvirt/profiles/console-ready_ppc64le.yaml` and seed-data baseline
  rows.

### Kernel-artifact contract

- `build_artifacts/validation.py` is x86-literal today: the combined kernel tar's
  `boot/vmlinuz` member must be a bzImage. Uploads gain an explicit arch declaration
  validated against the target profile; the payload check becomes arch-keyed — bzImage
  magic for x86_64, ELF kernel for ppc64le (powerpc has no bzImage; the bootable image
  is `vmlinux`). The member name stays `boot/vmlinuz` for contract stability, matching
  what Fedora/RHEL install on ppc64le.
- The agent-facing contract (tool wrapper docstrings and the
  `external-build-upload.md` resource) updates in the same PR as the behavior.
- The boot path (`lifecycle/boot/kernel_bundle.py`, `guest_kernel_writer.py`) is
  verified to direct-kernel-boot the ppc64le payload on pseries (SLOF boots ELF kernels
  via `-kernel`).

### Crash capture

- kdump: per-arch `crashkernel` defaults move into the arch-traits table and the
  ADR-0300 tunable seam; ppc64le reserves more than x86 (distro defaults there are
  roughly double). Capture and retrieve reuse the existing pipeline; live proof under
  TCG.
- fadump: profile opt-in adding `fadump=on` and the reservation. Two gates: discovery
  and doctor record whether the host QEMU implements pseries fadump
  (`ibm,configure-kernel-dump` RTAS — recent QEMU only), and a fadump-requesting
  provision on an unsupporting host fails admission with a clear category. If fadump
  proves unusable under TCG, the sub-issue degrades to POWER10-gated without blocking
  the epic spine. fadump still yields an ELF vmcore through the kdump initramfs, so
  retrieve is shared.

### Debug plane

- gdb: register handling in the shared gdb/MI layer is already dynamic (names resolved
  via `-data-list-register-names`, not hardcoded x86). What remains is host-side binary
  selection — a multiarch-capable gdb (`gdb-multiarch` on distros that split it) when
  guest ≠ host — plus a doctor check for the prerequisite.
- drgn's live path runs in-guest over SSH (`debug/live_introspect.py`) and is
  arch-neutral by construction; the vmcore-analysis path is the cross-arch
  verification target. drgn supports ppc64le targets, so the work is verification plus
  arch-parameterized tests, not new machinery.
- Console and sysrq paths are already arch-clean via arch traits.

### Diagnostics, docs, tests

- `scripts/check-local-libvirt.sh`, `diagnostics/provider_checks.py`, and the
  dep-checker learn per-arch qemu probes and a "TCG-only for arch X" advisory.
  `docs/operating/install.md` and the image-lifecycle runbook document the cross-arch
  story.
- Unit/contract tests arch-parameterize every seam above. `live_vm` gains a guest-arch
  dimension; TCG runs get a separate `live_vm_tcg` marker so the native suite stays
  fast. The epic midpoint proof: provision → boot → force-crash → kdump retrieve →
  drgn open, for a ppc64le guest under TCG on the x86_64 host.

## Sub-issues (one PR each)

| # | Sub-issue | Depends on |
|---|-----------|------------|
| 1 | Discovery: advertise bootable guest arches + accel + emulator per Resource | — |
| 2 | Admission: validate profile arch against `guest_arches`; persist accel on System | 1 |
| 3 | Domain XML: accel-derived domain type, `<emulator>`, per-arch CPU element | 1 |
| 4 | TCG deadline scaling in provider settings | 2, 3 |
| 5 | ppc64le profile fixture + seed data; live TCG boot proof of the Fedora ppc64le row | 3, 4 |
| 6 | Kernel-artifact contract: arch declaration + per-arch payload validation + docs | — |
| 7 | Boot path: direct-kernel-boot ppc64le bundle, with live proof | 5, 6 |
| 8 | Unify customization on the boot method + retire virt-customize (ADR-0345): rhel first (#1147), debian + argv-path deletion next | 5 |
| 9 | kdump on ppc64le: per-arch crashkernel defaults, pseries VMCOREINFO proof, capture/retrieve | 7, 8 |
| 10 | gdb multiarch support in the debug plane + doctor check | 5 |
| 11 | drgn on ppc64le: vmcore + live verification, arch-parameterized tests | 9 |
| 12 | fadump: profile opt-in, QEMU feature gate, capture proof (or documented POWER gating) | 9 |
| 13 | Catalog parity: ppc64le rows for remaining families + family customizer quirks | 8 |
| 14 | Diagnostics/dep-checker/scripts + install.md + runbook updates | 9 |
| 15 | `live_vm` arch matrix + `live_vm_tcg` marker wiring | 9 |
| 16 | x86_64-guest symmetry audit for a ppc64le host (unit-level; live proof gated) | 2, 3 |
| 17 | POWER10 host bring-up runbook + native KVM-HV validation (gated on hardware) | 16 |

ADRs (accelerator selection, artifact-contract change, fadump) are written inside their
sub-issues per repo convention.

## Known unverified

Flagged, not asserted; each is retired inside the named sub-issue:

- pseries fw_cfg/VMCOREINFO device behavior (issue 9).
- QEMU version floor for pseries fadump, and whether fadump works under TCG at all
  (issue 12).
- ~~SLOF direct-kernel boot of the uploaded ELF payload as packaged by the contract
  (issue 7).~~ **Retired in #1146** — a documented `live_stack` TCG boot on the x86_64 host
  installs and direct-kernel-boots an *uploaded* ppc64le kernel bundle (packaged per the ADR-0343
  contract) on pseries; the install/boot path is confirmed arch-opaque and the pseries
  initrd-addressing behavior is recorded. See ADR-0344 and
  `2026-07-13-ppc64le-boot-bundle-proof-record-1146.md`.
- ~~`pin_nic_slot=False` and other pseries runtime defaults from PR #1070 that have never
  seen a live boot (issue 5).~~ **Retired in #1144** — a live TCG boot on the x86_64 host
  proved `pin_nic_slot=False`, `console_device="hvc0"`, `machine="pseries"`, and the
  no-`<cpu>`-for-TCG rendering (POWER9 default, no ISA SIGILL). See
  `2026-07-13-ppc64le-tcg-boot-proof-record-1144.md`.

## Out of scope

- remote-libvirt arch work (its renderer shares seams but is a separate provider epic).
- PowerVM / bare-metal POWER providers (fadump groundwork here feeds them later).
- ppc64le CI runners in GitHub Actions.
- Big-endian ppc64.
