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
  cmdline via `services/runs/steps.py`.

## Design

### 1. Audit verdict: the boot path is arch-opaque, and that is the contract

The **host-side staging + `<os>` rendering** mechanics are byte-agnostic and stay that way:

- `extract_boot_vmlinuz` copies whatever bytes sit at the `boot/vmlinuz` tar member to a host
  file for the `<kernel>` element. It reads no magic; an ELF `vmlinux` round-trips identically to a
  bzImage.
- `repack_modules_subtree` and `_read_release` operate on `lib/modules/<ver>/` tar member *names*
  (host-side tar I/O, no guest execution): repack copies the subtree, and `_read_release` parses the
  version from the path (e.g. `6.19.10-300.fc44.ppc64le`) — already arch-general.
- `_render_direct_kernel_xml` points `<kernel>`/`<initrd>` at host file paths and sets `<cmdline>`
  from the request. The machine type (`pseries`), console (`hvc0`), and CPU/accel rendering are
  the provisioner's job (ADR-0340), already correct from #1144; install only *redefines the `<os>`*
  on the existing domain, so it inherits them.

**The guest kernel writer is not fully arch-neutral — it runs a guest binary in a host-arch
appliance.** `_RealGuestKernelWriter.inject` fires only when `request.method is KDUMP or
debuginfo_ref is not None` (install.py:339) and, inside libguestfs, runs
`guest.command(["depmod", "-a", version])` (guest_kernel_writer.py:133) — i.e. the *guest's own*
ppc64le `depmod` ELF executed inside libguestfs's **x86_64** appliance. What `depmod` *computes*
(a `modules.dep` from module names) is arch-general, but *executing* a ppc64le binary on an x86_64
appliance requires `qemu-user` + `binfmt_misc` registered in the appliance, which stock libguestfs
appliances do not carry. So the writer's in-guest `depmod` on a ppc64le overlay is a **live-only
cross-arch question**, not a settled "arch-neutral" fact (see §3, live-verified or flagged
UNVERIFIED and deferred to the kdump sub-issue). A **plain** direct-kernel boot of an uploaded
bundle does not inject modules at all, so it is unaffected — the writer is only load-bearing for the
kdump/debug paths.

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
byte-identical. **Scope caveat:** the fake writer proves the *orchestration* (which artifacts are
staged, in what order, into which paths) is arch-opaque; it does **not** prove the *real*
`_RealGuestKernelWriter`'s in-guest `depmod` runs on a ppc64le overlay (the libguestfs cross-arch
constraint of §1). That is a live-only question, addressed in §3.

### 3. Live proof: upload → install → direct-kernel-boot a ppc64le bundle under TCG

A documented `live_stack` run on the x86_64 host (this is the repo's only end-to-end
provision→install→boot path; the distinct `live_vm`/`live_vm_tcg` marker split is epic issue 15,
not here), reusing the #1144 preflight idiom (skip cleanly without `qemu-system-ppc64` /
`KDIVE_GUEST_IMAGE_PPC64LE`):

1. Provision a `arch=ppc64le` System (admission persists `accel=tcg`; the provisioner renders the
   pseries/qemu domain; the baseline rootfs boots to `ready`).
2. Package the guest's own baseline ppc64le kernel **and its matching initramfs** (see the initrd
   note below) as the ADR-0343 combined tar (`boot/vmlinuz` = the stripped bootable ELF,
   `lib/modules/<ver>/`) plus a separate `initrd` artifact — sourced from the same Fedora ppc64le
   scaffold #1144 already publishes (`select_kernel_and_initrd`, ADR-0272), so no cross-compile
   toolchain is required. Upload both and `runs.complete_build` (which validates the boot member as
   a ppc64le ELF, ADR-0343) to obtain `kernel_ref` **and** `initrd_ref`.
3. `runs.install` (extracts the ELF via `extract_boot_vmlinuz`, stages the `initrd`, redefines the
   `<os>` with `<kernel>`/`<initrd>`/`<cmdline>` for direct-kernel boot) → `runs.boot`. **No module
   injection** — a plain boot (`method != KDUMP`, no `debuginfo_ref`) does not invoke the guest
   kernel writer (§1); this proof exercises the host-side staging + `<os>`-render + SLOF boot path,
   which is exactly issue 7's headline.
4. **Assert readiness *and* attribute it to the installed bundle (discriminating).** The proof does
   not merely assert `runs.boot` reaches readiness — the uploaded payload is derived from the same
   baseline #1144 already boots, so a bare readiness signal cannot distinguish an install-plane boot
   from pre-existing baseline state. So it also asserts, over the live spine:
   - the running domain's `<kernel>`/`<initrd>` XML resolve to the **per-Run staged paths**
     (`{staging}/{system_id}/{run_id}/kernel`, `…/initrd`), not the provision-time baseline dir; and
   - a **unique cmdline token** passed at `runs.install` is present in the booted guest's
     `/proc/cmdline` (read over SSH), proving the install-plane `<cmdline>` reached the running
     kernel.

   Together these make "QEMU/SLOF direct-kernel-booted the *uploaded* bundle via the install plane"
   falsifiable, not confounded with #1144's baseline boot.

**Initrd addressing is the empirical unknown this proof resolves — and it needs an initrd to exist.**
The Fedora ppc64le baseline kernel is **modular**: ADR-0272 extracts the rootfs's kernel *and* its
`initramfs-<ver>.img` and boots them as a unit precisely because "a modular kernel cannot boot
without its initramfs." So the uploaded bundle proof **must** stage an `<initrd>` (step 2/3); a
no-initrd boot of this modular kernel would simply fail to mount root and is not attempted. The real
unknown is therefore whether pseries/SLOF direct-kernel boot needs any special `<initrd>` *addressing*
(load address / device-tree `linux,initrd-start`) beyond what QEMU's `-initrd` supplies. The expected
outcome is none — QEMU sets the device-tree initrd properties and the kernel reads them.

**The finding must be falsifiable in both directions — readiness alone is not the signal.** A modular
ppc64le kernel can miss readiness for reasons unrelated to initrd addressing (TCG slowness beyond the
scaled deadline → `BOOT_TIMEOUT`; the SSH readiness probe failing → `READINESS_FAILURE`; a bad `root=`
cmdline; SLOF declining the ELF; a mismatched module tree). So the initrd-addressing conclusion is
tied to **pre-registered `hvc0` console tokens**, not to overall readiness. The tokens are named
here (and in ADR-0344) *before* the run, so the verdict is not a post-hoc judgment call:
- **"No addressing quirk"** requires the console to show (a) the kernel `Linux version …ppc64le`
  banner (kernel started) **and** (b) the **`kdive-ready` marker on `hvc0`**. The `kdive-ready` unit
  runs in the *real* root (post-pivot, ADR-0342/#1144), so its appearance is positive proof the
  staged initramfs unpacked, mounted root, and pivoted — exactly what an initrd-addressing failure
  would prevent. Only the `kdive-ready`-on-`hvc0` token retires the issue-7 item (criterion 4); a
  bare "reaches readiness" (SSH) without it does not.
- **"Addressing quirk found"** requires the console to show the kernel banner (b above absent) plus a
  pre-registered *initramfs-stage* failure token — one of `Kernel panic … VFS: Unable to mount root
  fs`, a `dracut:` FATAL / emergency-shell line, or `Cannot open root device` — i.e. started but
  stalled at the initramfs, distinct from a panic *after* root mount. The accommodation then lands in
  code + a test, and ADR-0344 documents it. **Attribution on this branch is the offline domain-XML
  check**, not the SSH `/proc/cmdline` token (unavailable on a boot that never reaches readiness):
  the running domain's `<kernel>`/`<initrd>` (readable via `virsh dumpxml` without a booted guest)
  must resolve to the per-Run staged paths, so the failing console is provably the *installed* bundle,
  not a coincidental baseline failure.
- **Indeterminate** — a boot failure with the kernel banner present but *no* `kdive-ready` and *no*
  initramfs-stage failure token, or no early-boot output at all — is recorded indeterminate and
  **does not** retire issue 7; the proof is iterated (more deadline, console capture) until it yields
  a definitive token. "Reaches readiness" is necessary but not sufficient for the initrd verdict.

**Prerequisite the proof demonstrates first: `hvc0` is captured regardless of boot outcome.** The
above depends on the pseries `hvc0` console being teed from *domain start* and persisted even for a
non-ready boot — otherwise "no early-boot output" cannot be told from "console not captured." The
proof confirms this up front (the kernel banner is present in the captured console for a
deliberately-failed or still-booting run) before trusting the indeterminate classification; if
early-boot `hvc0` is not persisted on a failed boot, that capture gap is fixed first (it is a
prerequisite of any honest boot-failure verdict).

**Guest kernel writer (module injection) — live-verified with a stub, or deferred only if unrunnable.**
The plain boot above injects no modules (§1), so the libguestfs cross-arch `depmod` question is
answered by a *second* `runs.install` on the same ppc64le System with a `debuginfo_ref` set and
`method != KDUMP` (no kdump preflight): that triggers `_RealGuestKernelWriter.inject`, whose
`_extract_and_index` runs the guest's ppc64le `depmod` inside libguestfs's x86_64 appliance **before**
and independently of vmlinux staging (`_stage_vmlinux` only uploads bytes and checks `size>0`). So the
cross-arch `depmod` verdict needs only a **stub** (any non-empty) `debuginfo_ref` — a real ppc64le
DWARF `vmlinux` is *not* required (that matters only for drgn usefulness, issues 10/11, out of scope).
- If the stub-triggered inject completes, ADR-0344 records the writer's in-guest `depmod`
  **verified** for ppc64le under an x86_64 appliance.
- If it fails with an exec-format / binfmt error, that *is* the discovered libguestfs same-arch
  constraint — captured in ADR-0344, with the `qemu-user`/`binfmt` appliance accommodation scoped to
  issue 9 (where module injection is load-bearing for kdump). **Classifying this needs the chained
  cause:** `_extract_and_index` collapses any `guest.command(["depmod", …])` fault into one
  `INFRASTRUCTURE_FAILURE` whose `details` carry only the exception *type name* (guest_kernel_writer
  .py:134-137, 192-198) — the libguestfs message survives only on `__cause__`. So the proof step
  records the chained cause / libguestfs log and confirms the pre-registered substring (`exec format
  error` / binfmt) before concluding "cross-arch constraint," rather than trusting the categorized
  type name alone (which also fires for a `tar_in` fault or the missing-`modules.dep` check).
- **UNVERIFIED/defer** applies only if the stub inject cannot even run on the proof host (e.g.
  libguestfs absent) — not merely because production-grade debuginfo is unavailable. The writer is
  never claimed arch-neutral on the strength of the fake-writer unit tests.

The console record, the initrd-addressing finding, and the writer verdict are written to
`docs/design/2026-07-13-ppc64le-boot-bundle-proof-record-1146.md`, mirroring #1144's proof record.

## Acceptance criteria

1. **x86_64 boot path unchanged.** Every existing `kernel_bundle`/`install` test passes
   unmodified; the staged `<kernel>` bytes and rendered `<os>` for an x86 bundle are byte-identical
   to today. "Byte-agnostic" is asserted, not assumed.
2. **ppc64le bundle exercised (unit).** `extract_boot_vmlinuz` extracts an ELF64-LE `EM_PPC64`
   boot member byte-identically; `repack_modules_subtree`/`_read_release` handle a
   `…​.ppc64le` module version; the arch-parameterized install flow (with the injected fake writer)
   renders the ELF `<kernel>` + staged `<initrd>` with the request cmdline and, on the injection
   path, hands the ppc64le module tree to the writer. (The *real* writer's in-guest `depmod` on a
   ppc64le overlay is a live question — criterion 5 — not a unit claim.)
3. **Live proof recorded (discriminating).** A documented `live_stack` run installs and
   direct-kernel-boots an *uploaded* ppc64le kernel+initrd bundle on pseries under TCG on the x86_64
   host, reaches readiness, **and** attributes it to the install plane: the running domain's
   `<kernel>`/`<initrd>` resolve to the per-Run staged paths and a unique install cmdline token
   appears in the guest's `/proc/cmdline`. The proof record captures the console evidence and the
   initrd-addressing finding.
4. **No tribal knowledge; the initrd verdict is falsifiable both ways.** The initrd-addressing
   conclusion is tied to a positive `hvc0` console marker (initramfs unpacked/mounted → "no quirk,"
   retire issue 7; kernel started but failed at the initramfs → "quirk," accommodation in code + a
   test), not to readiness alone; a boot failure with no initramfs-stage signal is **indeterminate**
   and does **not** retire the epic's "SLOF direct-kernel boot … (issue 7)" item. The finding
   (whichever) is recorded in ADR-0344.
5. **Guest kernel writer verdict, not assumption.** The real `_RealGuestKernelWriter`'s in-guest
   `depmod` on a ppc64le overlay is live-exercised with a **stub** (non-empty) `debuginfo_ref`
   install (`method != KDUMP`) — no real DWARF required — and recorded verified, or its exec-format
   failure captured as the libguestfs cross-arch constraint (accommodation scoped to issue 9).
   UNVERIFIED/defer applies only if the stub inject cannot run on the proof host at all; the writer
   is never asserted "arch-neutral" on the strength of the fake-writer unit tests.
6. **De-x86-ed prose.** `extract_boot_vmlinuz` (and adjacent install docstrings) no longer assert
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
