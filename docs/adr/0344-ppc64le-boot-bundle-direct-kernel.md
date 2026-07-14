# ADR 0344 — Direct-kernel-boot ppc64le kernel bundles: trust the upload contract, stay arch-opaque

- **Status:** Accepted
- **Date:** 2026-07-13
- **Issue:** #1146
- **Epic:** #1139 (full ppc64le support)
- **Builds on:** ADR-0343 (arch-aware upload contract), ADR-0342 (#1144 live TCG boot proof),
  ADR-0340 (accel-derived domain XML), ADR-0341 (TCG deadline scaling), ADR-0272/0030 (baseline
  direct-kernel boot), ADR-0234 (combined kernel tar layout)

## Context

The local-libvirt install/boot path — `lifecycle/boot/kernel_bundle.py`
(`extract_boot_vmlinuz`, `repack_modules_subtree`), `lifecycle/boot/guest_kernel_writer.py`, and
`lifecycle/install.py` — has only ever staged and booted x86 bzImage payloads. #1145 (ADR-0343)
made the *upload* contract arch-aware, so a ppc64le combined tar (an ELF64-LE `EM_PPC64`
`boot/vmlinuz` + `lib/modules/<ver>/`) now validates at `runs.complete_build`. But the *install*
plane that extracts that boot member, injects its module tree, and redefines the domain's
direct-kernel `<os>` was never audited or exercised for a ppc64le bundle. The epic's
"Known unverified" list flags exactly this:

> SLOF direct-kernel boot of the uploaded ELF payload as packaged by the contract (issue 7).

Auditing the path shows the mechanics are already **byte-agnostic**:

- `extract_boot_vmlinuz` copies the `boot/vmlinuz` member's bytes to a host file for the
  `<kernel>` element — it reads no magic; an ELF `vmlinux` round-trips like a bzImage.
- `repack_modules_subtree` and the writer's `depmod -a <ver>` operate on `lib/modules/<ver>/`
  names/blobs; `_read_release` parses the version (e.g. `6.19.10-300.fc44.ppc64le`) from the path.
  No arch assumption.
- `_render_direct_kernel_xml` sets `<kernel>`/`<initrd>`/`<cmdline>` on the *existing* domain,
  inheriting the pseries machine, `hvc0` console, and TCG accel/CPU rendering the provisioner
  already produces (ADR-0340, live-proven in #1144).

The remaining x86-literalness is in the **prose**: `extract_boot_vmlinuz`'s docstring asserts
libvirt's `<kernel>` element "needs a raw **bzImage** path" — false for ppc64le. That is the
tribal-knowledge trap the acceptance criteria forbid.

One empirical unknown remains: the exact **initrd addressing** an uploaded (potentially modular)
ppc64le kernel needs to direct-kernel-boot on pseries under QEMU/SLOF — never before booted
end-to-end from the packaged contract.

## Decision

**The install/boot path stays arch-opaque and trusts the upload contract; it does not
re-validate the payload arch.** The uploaded bundle was arch-validated at `runs.complete_build`
(ADR-0343) against `BOOT_MEMBER_FORMATS`. Re-checking the boot member's magic at install time
would duplicate that gate for the same, now-trusted bytes — no new safety — and would
*re-introduce* the very bzImage-literalness this issue removes. So the boot path treats
`boot/vmlinuz` as opaque kernel bytes for the `<kernel>` element, regardless of arch.

Concretely:

- **De-x86 the prose.** `extract_boot_vmlinuz`'s docstring states the `<kernel>` element needs a
  raw kernel image — a bzImage on x86_64, an ELF `vmlinux` on ppc64le (powerpc has no bzImage) —
  extracted host-side. Adjacent x86-literal examples in `install.py` are generalized to the
  arch-opaque case (an embedded-initramfs kernel), not deleted. No behavior change.
- **Arch-parameterized regression tests are the durable guard.** Because the value of
  "byte-agnostic" is that it *stays* so, new tests feed an ELF64-LE `EM_PPC64` boot member and a
  `…​.ppc64le` module version through `extract_boot_vmlinuz` / `repack_modules_subtree` /
  `_read_release` / the arch-parameterized install-inject flow, asserting the ELF member is
  extracted byte-identically and the module tree injects. They fail the instant a bzImage
  assumption re-enters the boot path. The x86 assertions stay byte-identical.
- **Live proof, and the initrd finding.** A documented `live_stack` run on the x86_64 host
  uploads the guest's own baseline ppc64le kernel as a contract bundle, `runs.install`s and
  `runs.boot`s it on a provisioned `arch=ppc64le` System under TCG, and observes `runs.boot`
  readiness. **The initrd-addressing outcome is recorded here** — whether the bundle boots with
  no initrd, with a staged `<initrd>`, or needs a pseries-specific accommodation — so it is code +
  ADR, not tribal knowledge. Expected outcome: no special addressing (QEMU loads `-initrd`; the
  kernel finds it via the device tree, as it does the `-kernel` payload). If an accommodation is
  required, it lands in code with a test and its rationale is appended to this ADR.

## Consequences

- An uploaded ppc64le ELF bundle installs and direct-kernel-boots on pseries under TCG through the
  unchanged, arch-opaque install path; an x86_64 bundle is byte-identical to today (asserted, not
  assumed).
- The boot path has exactly one arch gate — the upload contract (ADR-0343). Install trusts it, so
  there is no second, drift-prone magic table in the boot path.
- The arch-parameterized tests lock the byte-agnostic contract: a future change that re-adds a
  bzImage assumption to `kernel_bundle.py` or the writer fails CI.
- The epic's "SLOF direct-kernel boot … (issue 7)" Known-unverified item is retired by the live
  proof and this ADR; the initrd-addressing behavior on pseries is now documented.
- No migration, no new deadline/XML/fetch machinery, no schema change.

## Rejected alternatives

- **Mirror ADR-0343's `BOOT_MEMBER_FORMATS` check into `extract_boot_vmlinuz` (re-validate the
  boot member's arch at install).** Rejected: the bytes were already validated at upload
  (ADR-0343); re-checking them at install adds no safety (same trusted bytes) and re-introduces the
  bzImage-literalness this issue removes. One gate, at the upload boundary, is the contract.
- **Rename `extract_boot_vmlinuz` / the `boot/vmlinuz` member to something arch-neutral.**
  Rejected: `boot/vmlinuz` is the stable contract member name (ADR-0234/0343) and is what
  Fedora/RHEL install as `/boot/vmlinuz-<ver>` on *both* arches; renaming churns the contract and
  the advertisement for a cosmetic gain. The docstring, not the name, carried the wrong claim.
- **Cross-compile a fresh ppc64le kernel for the live proof.** Rejected as an unnecessary host
  requirement: repackaging the guest's own baseline ppc64le kernel as a contract bundle exercises
  the identical arch-opaque install→boot path an operator's cross-compiled upload would take, with
  no cross-toolchain on the proof host. A genuinely cross-compiled upload rides the same path.
- **Assert the boot path arch-neutrality only at the unit level (skip the live proof).**
  Rejected: the acceptance criteria require a documented live boot of an *uploaded* bundle, and
  the initrd-addressing behavior on pseries is only knowable by booting — the exact tribal-knowledge
  gap this issue closes.
- **Pre-emptively add a pseries initrd-addressing accommodation before the proof.** Rejected:
  speculative — QEMU/SLOF is expected to need none. The accommodation is added only if the live
  proof forces it (fail-fast on the evidence, ADR the finding either way).

## Rollout

Additive and backward compatible. No migration and no behavior change on the x86_64 boot path
(the change is docstring prose + tests + a live proof, plus a narrowly-scoped pseries
accommodation only if the live proof forces one). ppc64le is proven through the same install/boot
code x86 already uses.
