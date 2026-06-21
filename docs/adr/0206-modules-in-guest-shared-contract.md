# ADR 0206 — Modules in the guest: a shared build-output contract, two delivery mechanisms (local-libvirt kdump)

- **Status:** Accepted
- **Date:** 2026-06-21
- **Deciders:** kdive maintainers
- **Issue:** [#654](https://github.com/randomparity/kdive/issues/654)
- **Refines (does not supersede):** [ADR-0055](0055-install-readiness-kdump-seam.md) §5 (the
  staged-`<initrd>` kdump gate this replaces), [ADR-0030](0030-install-boot-plane.md) (the
  local install/boot plane), [ADR-0096](0096-kdump-config-fragment-build-input.md) (the
  kdump config fragment whose `CONFIG_CRASH_DUMP=y` is the build trigger).
- **Builds on:** [ADR-0203](0203-local-libvirt-kdump-overlay-harvest.md) (the host-side
  overlay read/write seam and the same-host fact this reuses),
  [ADR-0081](0081-remote-build-kernel-bundle.md) /
  [ADR-0082](0082-remote-install-in-guest-kernel.md) (remote's modules-in-guest delivery this
  converges with), [ADR-0169](0169-decouple-build-system-binding.md) (the build is decoupled
  from the System, so the trigger cannot be the System's capture method).
- **Spec:** [`../superpowers/specs/2026-06-21-local-kdump-modules-in-guest-654.md`](../superpowers/specs/2026-06-21-local-kdump-modules-in-guest-654.md)

## Context

A local-libvirt System with a `crashkernel` provisioning profile boots kdump-capable
(`crashkernel=256M` reserved) and, since ADR-0203, can have a guest-written
`/var/crash/<ts>/vmcore` harvested host-side from its qcow2 overlay. But the real
panic→kdump→capture arc cannot run: `runs.install` rejects the System.

```
install: resolved cmdline 'console=ttyS0 root=/dev/vda crashkernel=256M' (method kdump)
CategorizedError: kdump capture initramfs not staged (a separate initrd is required for kdump)
```

The root cause is that local-libvirt direct-kernel-boots the raw `bzImage` (`root=/dev/vda`,
no initramfs, builtin virtio/ext4) into a whole-disk-ext4 qcow2 and **never installs
`/lib/modules` into the guest**. Standard in-guest kdump needs the guest's `kdumpctl` /
`kdump.service` to build a crash initramfs via dracut against `/lib/modules/<running-ver>` —
which the custom kernel never installs. So `crashkernel=` is reserved but no capture
environment can be assembled.

ADR-0055 §5 made the *presence of a staged `<initrd>`* the host-observable proxy for "capture
path armed", and `services/runs/steps.py` carries an `initrd_ref` field for it. The build
never produces an initrd, so the gate is permanently unsatisfiable for local kdump — and
ADR-0055 §5 itself flagged that #115's in-guest probe would supersede it.

"Get kernel modules into the guest" has been a deferred requirement for local-libvirt:
direct-kernel boot with builtin drivers never needed it. **Remote-libvirt already solves it.**
Its build runs `make modules_install` and ships a `vmlinuz + /lib/modules` bundle inside
`kernel_ref` (ADR-0081); an in-guest helper (`kdive-install-kernel`, ADR-0082) extracts the
modules into the guest and runs the distro grub/dracut path; the ansible base image ships
`kexec-tools`/`makedumpfile`/`kdump-utils` and enables `kdump.service`; remote KDUMP capture
ships (ADR-0084). The asymmetry — remote has real modules in the guest, local has none — is
the deferred requirement, and #654's kdump need forces it.

## Decision

Converge local-libvirt on remote's outcome — **a real `/lib/modules/<ver>` in the guest** —
via a shared build-output contract with provider-specific delivery, and let the distro build
the crash initramfs in-guest on both providers.

### 1. The build output is a shared contract: kernel + modules (+ debuginfo)

A build produces a bootable kernel image, the kernel's `/lib/modules/<ver>` tree, and the
`vmlinux` debuginfo. Each provider lands the modules in the guest its own way; the contract
is the *outcome* (a real module tree in the guest), not a single byte layout:

- **Remote (unchanged):** modules ride inside `kernel_ref`'s gzip bundle, delivered by the
  in-guest `kdive-install-kernel` helper over the guest agent (networked target).
- **Local (new):** the build publishes a **separate `modules_ref`** artifact (the
  `/lib/modules/<ver>` tree as a gzip tar), and the install plane injects it into the
  per-System qcow2 overlay host-side via libguestfs (same-host target).

Local cannot fold modules into `kernel_ref` the way remote does, because it
direct-kernel-boots the raw `bzImage` as the libvirt `<kernel>` — so for local the modules are
a distinct artifact. Remote's existing bundle packaging already satisfies the contract and is
**not** refactored (it is delivery-correct for an in-guest install; rebuilding it would
destabilize shipped, verified code for no behavioral gain). A future ADR may unify the
packaging if a concrete need appears.

### 2. The build trigger is the resolved `.config`, not a profile field

The build is decoupled from the System (ADR-0169) and never sees the provisioning profile
where `crashkernel` (the capture-method selector) lives. So the trigger for "also build the
modules artifact" is the resolved `.config`: when `CONFIG_CRASH_DUMP=y` (already the default
via the ADR-0096 kdump fragment), `LocalLibvirtBuild` runs `make modules_install` (reusing the
shared `real_run_modules_install` seam) and publishes `modules_ref`. A config the operator
overrode to drop crash-dump produces no modules artifact (today's behavior). This keeps the
zero-config path kdump-capable end-to-end with no new build-profile field.

### 3. `modules_ref` is a new field; `initrd_ref` stays for the upload lane

`BuildOutput`, `BuildStepResult`, and `InstallRequest` gain a **new** `modules_ref: str | None`
field. `initrd_ref` is **kept** — it is not dormant: the external/upload build lane
(ADR-0048/0166) populates it (`complete_build.py`: `initrd_ref=finalization.keys.get("initrd")`),
the install handler reads it (`installed_initrd_ref`), and `LocalLibvirtInstall.install` stages
it as the domain `<initrd>` when an uploaded build ships one. A built `/lib/modules` tarball
(`modules_ref`) and an optionally-uploaded boot initrd (`initrd_ref`) are distinct artifacts and
stay distinct fields. Only the kdump *gate* over `initrd_ref` is removed (§5), not the field or
its staging path.

### 4. Local install injects modules into the overlay; the guest builds the crash initramfs

`LocalLibvirtInstall.install`, when the method is KDUMP and `modules_ref` is present: fetches
the modules tarball, **force-offs the domain if active**, read-write-mounts the per-System
overlay via libguestfs, **clobbers** any existing `/lib/modules/<ver>`, writes the tree, runs
`depmod`, and verifies a `modules.dep` content sentinel. Two safety properties are explicit:

- **Force-off first.** `runs.install` is admitted on any `succeeded` Run regardless of the
  System's power state (ADR-0030 §1 gates on Run state), and recovery is a new Run on the same
  System (ADR-0026 §7), so a re-install can target an already-booted System. A read-write
  libguestfs mount of a live qcow2 corrupts it (the hazard ADR-0203 force-offs to avoid), so
  install `destroy`s the domain if `isActive()` before the mount — idempotent, mirroring
  `boot()`'s destroy-then-create; the later `boot()` re-creates it.
- **Idempotent injection.** A failed install records no `run_steps` row (ADR-0030 §2), so a
  retry re-runs injection. It must self-heal a partial prior write: clobber the version dir (or
  temp-extract + atomic rename) before extracting, and verify a completion sentinel (`depmod`
  exits 0 and rewrites `modules.dep`) — not merely that the version dir exists. The sentinel
  must not require `modules.dep` be non-empty: an all-builtin kdump kernel leaves a valid empty
  one.

A `GuestModuleWriter` seam mirrors ADR-0203's `GuestCoreReader` split — the
force-off→fetch→clobber→write→depmod→verify orchestration is pure and unit-tested with a fake;
only the libguestfs/`depmod`/`domain.destroy()` calls are `live_vm`-gated. The crash initramfs is
then built **in-guest by `kdumpctl`** — local hand-rolls no capture image, matching remote.

### 5. The kdump install gate is replaced (refines ADR-0055 §5)

`_kdump_capture_present(initrd_path)` is replaced. The host-observable proxy for "capture path
armable" becomes **"modules for the run's kernel version were injected into the overlay."** A
KDUMP install with no `modules_ref` is the `configuration_error`. The production boot stays
direct-kernel with **no `<initrd>` element** — modules live in the rootfs, drivers are builtin,
`kdumpctl` arms kexec in-guest.

### 6. The guest filesystem is mutable after provisioning

Injecting into the overlay at install establishes that the host may write the guest
filesystem after provisioning — a deliberate property. It lets an agent run multiple debugging
tests against one System and is the foundation a future "agent drives guest contents"
capability builds on. #654 exposes no general guest-write tool; it only uses the seam for
module injection.

### 7. The local debug image ships `kdump-utils`

The local `debug` fs kind (`images/rootfs_command.py`) ships `drgn, kexec-tools, makedumpfile`
but not `kdump-utils` (Fedora's `kdump.service`/`kdumpctl`). Add it and enable the service so
the in-guest path works on kdive's own local image — a one-line image-spec change that keeps
#654 end-to-end.

## Consequences

- A local System built kdump-capable now boots, arms kdump in-guest, and on
  `control.force_crash` writes a real `/var/crash/<ts>/vmcore` the ADR-0203 harvest fetches —
  the keystone gap closes; local kdump no longer requires staging a core into the overlay.
- The deferred "modules in the guest" requirement is retired for local-libvirt; loadable
  modules are available in the guest for any debugging scenario, not just kdump.
- `make modules_install` runs on every kdump-capable local build, adding build time even for
  console/gdbstub Systems (the cost of the config-driven, zero-config default).
- `libguestfs` was already a local KDUMP host prereq (ADR-0203, for the harvest); it is now
  also required for the install-time module injection. Absence stays a typed
  `MISSING_DEPENDENCY`.
- The two providers converge on one outcome (a real guest module tree) while keeping
  delivery-appropriate mechanisms; the build output is a documented shared contract rather
  than two drifting per-provider shapes.
- Build-id and the capture path are unchanged; no schema or migration change.

## Considered & rejected

- **Self-contained capture initramfs built host-side (dracut `kdumpbase`), staged as
  `<initrd>`.** Smallest blast radius and keeps the ADR-0055 gate, but solves only kdump
  capture, leaves local permanently divergent from remote, and does not address the general
  modules-in-guest gap — the next loadable-module need re-opens it. Rejected for a general
  solution.
- **Ship `/lib/modules` for local but install it via a local guest-agent helper (mirror remote
  exactly).** Adds a guest-agent dependency and a localhost round-trip where the host already
  owns the overlay file directly — more moving parts on a same-host provider (the reasoning
  ADR-0203 used to reject a local guest agent). Rejected for host-side libguestfs injection.
- **Treat the guest rootfs as immutable after provisioning** (rebuild a fresh overlay per
  Run rather than mutate it). Rejected by the maintainer: a mutable guest fs lets an agent run
  multiple debugging tests and is the foundation for agent-driven guest contents.
- **Repurpose the existing `initrd_ref` field into `modules_ref` (one slot).** Rejected:
  `initrd_ref` is live, not dormant — the external/upload build lane carries an uploaded boot
  initrd through it (`complete_build.py`) and `install.py` stages it as `<initrd>`. Folding the
  built modules tarball into that slot would break the upload lane and conflate two distinct
  artifacts. `modules_ref` is a new field; `initrd_ref` stays.
- **Mount the overlay read-write without checking the domain's power state.** Rejected: a
  re-install onto an already-booted System (ADR-0026 §7 recovery) would rw-mount a live qcow2 —
  the corruption hazard ADR-0203 force-offs to avoid. Install force-offs if active first.
- **Verify injection by the `/lib/modules/<ver>` directory's existence.** Rejected: a retry
  after a crashed prior attempt false-passes on a half-written tree; injection clobbers then
  verifies a `modules.dep` content sentinel.
- **Refactor remote's bundle into a separate `modules_ref` too, for byte-identical
  packaging.** Destabilizes shipped, verified remote code (ADR-0081/0082) for no behavioral
  gain; remote's in-guest install is delivery-correct as is. Rejected; the shared contract is
  the outcome, not the byte layout.
- **An explicit build-profile field to opt into the modules artifact.** Decouples the trigger
  from the config but reintroduces boilerplate on the common (kdump-capable) build and a way
  for the profile to disagree with the resolved config. Rejected for the config-driven trigger
  (ADR-0096's zero-config-is-kdump-capable principle).
