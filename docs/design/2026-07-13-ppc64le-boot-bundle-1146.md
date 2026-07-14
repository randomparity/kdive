# Direct-kernel-boot ppc64le kernel bundles, with live proof (#1146)

Date: 2026-07-13
Status: approved (design)
Issue: #1146 · Epic: #1139 (full ppc64le support) · ADR: `docs/adr/0344-ppc64le-boot-bundle-direct-kernel.md`
Depends on: #1144 (live TCG boot proof of the Fedora ppc64le row, ADR-0342), #1145 (arch-aware upload contract, ADR-0343)

## Problem

The local-libvirt install/boot path — `lifecycle/boot/kernel_bundle.py`,
`lifecycle/boot/guest_kernel_writer.py`, `lifecycle/install.py` — has only ever staged and
booted x86 bzImage payloads. #1145 made the *upload* contract arch-aware (a ppc64le ELF
`vmlinux` at `boot/vmlinuz` now validates), but the *install* plane that extracts that member,
injects its module tree, and hands it to libvirt's direct-kernel `<kernel>` element was never
audited or exercised for a ppc64le bundle. The epic's "Known unverified" list flags it directly:

> SLOF direct-kernel boot of the uploaded ELF payload as packaged by the contract (issue 7).

Two concrete gaps:

1. **Unproven.** No test feeds an ELF boot member through `extract_boot_vmlinuz` /
   `repack_modules_subtree` / the guest kernel writer, and no live run has direct-kernel-booted an
   uploaded ppc64le bundle on pseries. The local provider always direct-kernel-boots
   (`<kernel>`/`<initrd>`, ADR-0272/0030), and QEMU/SLOF boots an ELF kernel via `-kernel`, but
   this has never been packaged-and-booted end-to-end.
2. **x86-literal documentation.** `extract_boot_vmlinuz`'s docstring asserts libvirt's
   `<kernel>` element "needs a raw **bzImage** path" — false for ppc64le, where the bootable image
   is an ELF `vmlinux`. The code is byte-agnostic; the prose is not. That prose is exactly the
   tribal-knowledge trap the acceptance criteria forbids.

## Inputs (already landed)

- **ADR-0343 / #1145**: `BuildProfile.arch`, `BOOT_MEMBER_FORMATS`, arch-keyed payload validation
  at `runs.complete_build`. A ppc64le combined tar (ELF64-LE `EM_PPC64` at `boot/vmlinuz` +
  `lib/modules/<ver>/`) is *validated at upload*, so the install plane can trust the member.
- **ADR-0342 / #1144**: the Fedora ppc64le row + fixture + the live TCG boot proof of the
  *baseline* (rootfs-own) ppc64le kernel. Retired `pin_nic_slot=False`, `console_device="hvc0"`,
  `machine="pseries"`, no-`<cpu>`-for-TCG. The `KDIVE_GUEST_IMAGE_PPC64LE` scaffold and the
  `test_ppc64le_guest_is_ssh_reachable_over_the_wire` `live_stack` proof exist to build on.
- **ADR-0339/0340/0341**: admission persists `accel` (`tcg` for a ppc64le guest on the x86_64
  host), the provisioner renders a pseries/qemu domain, and the boot handler applies the TCG-scaled
  readiness deadline (`tcg_deadline_multiplier`). The boot window already scales for a slow
  emulated boot; this issue adds no new deadline machinery.
- `domain/platform/arch_traits.py`: `console_device="hvc0"` for ppc64le, flowing into the boot
  cmdline via `services/runs/steps.py`. The writer's `depmod -a <ver>` is arch-neutral.

## Design

### 1. Audit verdict: the boot path is arch-opaque, and that is the contract

The install/boot mechanics are **byte-agnostic** and stay that way:

- `extract_boot_vmlinuz` copies whatever bytes sit at the `boot/vmlinuz` tar member to a host
  file for the `<kernel>` element. It reads no magic; an ELF `vmlinux` round-trips identically to a
  bzImage.
- `repack_modules_subtree` and the guest kernel writer's `depmod -a <ver>` operate on
  `lib/modules/<ver>/` names and module blobs — no arch assumption. `_read_release` parses the
  version from the module path (e.g. `6.19.10-300.fc44.ppc64le`), which is already arch-general.
- `_render_direct_kernel_xml` points `<kernel>`/`<initrd>` at host file paths and sets `<cmdline>`
  from the request. The machine type (`pseries`), console (`hvc0`), and CPU/accel rendering are
  the provisioner's job (ADR-0340), already correct from #1144; install only *redefines the `<os>`*
  on the existing domain, so it inherits them.

**Decision (ADR-0344): the boot path trusts the upload contract and does not re-validate the
payload arch.** The uploaded bundle was already arch-validated at `runs.complete_build`
(ADR-0343); re-checking the boot member's magic at install time would duplicate that gate for no
new safety (the same bytes, now trusted) and would *re-introduce* the x86-literalness this issue
removes. So the install plane stays arch-opaque. The alternative — mirror ADR-0343's
`BOOT_MEMBER_FORMATS` check into `extract_boot_vmlinuz` — is rejected in the ADR.

The code change this yields is therefore small and correctness-preserving:

- De-x86 the `extract_boot_vmlinuz` docstring: `<kernel>` needs a raw kernel image — a bzImage on
  x86_64, an ELF `vmlinux` on ppc64le (powerpc has no bzImage) — extracted host-side. No behavior
  change.
- Any adjacent x86-literal example prose in `install.py` ("a bzImage with an embedded initramfs")
  is generalized to name the arch-opaque case (an embedded-initramfs kernel), not deleted — the
  initrd-optional path is real on both arches.

### 2. Arch-parameterized regression tests (the durable guard)

Because the value of "byte-agnostic" is that it *stays* byte-agnostic, the tests are the real
deliverable: they fail the instant someone re-adds a bzImage assumption to the boot path.

- **`extract_boot_vmlinuz` round-trip, both arches.** Feed a combined tar whose `boot/vmlinuz` is
  (a) bzImage-magic bytes and (b) ELF64-LE `EM_PPC64` bytes; assert the extracted host file is
  **byte-identical** to the member in both cases. A new direct test file for `kernel_bundle.py`
  (it has none today — it is covered only implicitly through install tests using `b"bzImage-bytes"`).
- **`repack_modules_subtree` + `_read_release` at a ppc64le uname.** Repack a `lib/modules/
  6.19.10-300.fc44.ppc64le/` subtree; assert the modules-only tar is produced and the version is
  read back. Confirms the arch suffix in the version string is handled.
- **Install flow, arch-parameterized.** Extend `tests/providers/local_libvirt/test_install.py`'s
  combined-tar helper to take an arch (bzImage vs. ELF boot member + arch-suffixed module version)
  and parameterize the install/inject assertions over both, so the whole
  extract → repack → inject → `<kernel>`-render path is exercised for a ppc64le bundle with the
  injected fake writer. The staged `<kernel>` file's bytes equal the ELF member; the modules tar
  carries the ppc64le version; `<cmdline>` is passed through verbatim.

These are unit/contract tests (no host) — the fakes already exist. The x86 assertions stay
byte-identical.

### 3. Live proof: upload → install → direct-kernel-boot a ppc64le bundle under TCG

A documented `live_stack` run on the x86_64 host (this is the repo's only end-to-end
provision→install→boot path; the distinct `live_vm`/`live_vm_tcg` marker split is epic issue 15,
not here), reusing the #1144 preflight idiom (skip cleanly without `qemu-system-ppc64` /
`KDIVE_GUEST_IMAGE_PPC64LE`):

1. Provision a `arch=ppc64le` System (admission persists `accel=tcg`; the provisioner renders the
   pseries/qemu domain; the baseline rootfs boots to `ready`).
2. Package the guest's own baseline ppc64le kernel as a combined tar per the ADR-0343 contract
   (`boot/vmlinuz` = the stripped bootable ELF, `lib/modules/<ver>/`) — sourced from the same
   Fedora ppc64le scaffold #1144 already publishes, so no cross-compile toolchain is required on
   the host. Upload it and `runs.complete_build` (which validates it as a ppc64le ELF, ADR-0343)
   to obtain a `kernel_ref`.
3. `runs.install` (extracts the ELF via `extract_boot_vmlinuz`, injects the module tree, redefines
   the `<os>` for direct-kernel boot) → `runs.boot`.
4. **Assert `runs.boot` reaches readiness** (`runs.get` install/boot ledger; SSH reachable), the
   load-bearing proof that QEMU/SLOF direct-kernel-booted the *uploaded* ELF bundle on pseries.

**Initrd addressing is the empirical unknown this proof resolves.** A modular distro kernel needs
an initramfs to mount root; the ADR-0272 baseline is a monolithic `root=/dev/vda` boot with no
initrd. Whether the uploaded ppc64le bundle boots with no initrd (monolithic), with a staged
`<initrd>`, or requires a pseries-specific initrd-addressing accommodation is discovered here and
**captured in ADR-0344**, not left as tribal knowledge (the acceptance criterion). If SLOF/QEMU
needs no special addressing (the expected outcome — QEMU loads `-initrd` and the kernel finds it
via the device tree), the ADR records "no quirk"; if it does, the accommodation lands in code with
a test and the ADR documents why.

The console record and any quirk are written to
`docs/design/2026-07-13-ppc64le-boot-bundle-proof-record-1146.md`, mirroring #1144's proof record.

## Acceptance criteria

1. **x86_64 boot path unchanged.** Every existing `kernel_bundle`/`install` test passes
   unmodified; the staged `<kernel>` bytes and rendered `<os>` for an x86 bundle are byte-identical
   to today. "Byte-agnostic" is asserted, not assumed.
2. **ppc64le bundle exercised (unit).** `extract_boot_vmlinuz` extracts an ELF64-LE `EM_PPC64`
   boot member byte-identically; `repack_modules_subtree`/`_read_release` handle a
   `…​.ppc64le` module version; the arch-parameterized install flow injects the ppc64le module
   tree and renders the ELF `<kernel>` with the request cmdline.
3. **Live proof recorded.** A documented `live_stack` run boots an *uploaded* ppc64le kernel
   bundle on pseries under TCG on the x86_64 host and observes `runs.boot` readiness; the proof
   record doc captures the console evidence and the initrd-addressing finding.
4. **No tribal knowledge.** Any pseries direct-boot quirk (initrd addressing or its absence) is
   recorded in ADR-0344 (and in code + a test if it needs an accommodation), and the epic's
   "SLOF direct-kernel boot … (issue 7)" Known-unverified item is retired.
5. **De-x86-ed prose.** `extract_boot_vmlinuz` (and adjacent install docstrings) no longer assert
   a bzImage-only `<kernel>`; the arch-opaque contract is stated where a reader meets it.

## Scope / non-goals

- **No behavior change to the boot mechanics.** The path is already arch-opaque; this issue proves
  it, guards it with tests, corrects the prose, and (only if the live proof forces it) adds a
  narrowly-scoped pseries accommodation. No new deadline, XML, or fetch machinery.
- **No re-validation of the payload arch at install** (ADR-0344 rationale) — the upload contract
  (ADR-0343) owns that gate.
- **No cross-compile toolchain requirement.** The proof repackages the guest's own baseline
  ppc64le kernel as a contract bundle; an operator's genuinely cross-compiled upload rides the same
  arch-opaque path.
- **remote-libvirt is out of scope** — its `inject` seam is bootstrap-SSH-key injection, not the
  kernel bundle; arch work there is a separate provider epic (epic non-goal).
- **No kdump/debug-plane work** — capture on ppc64le is issue 9; gdb/drgn are 10/11. This issue is
  the boot of an uploaded kernel only.
- **No `live_vm_tcg` marker** (issue 15) and no big-endian ppc64 (epic non-goal).
