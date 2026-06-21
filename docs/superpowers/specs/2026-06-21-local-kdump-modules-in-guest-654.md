# Local-libvirt kdump: modules in the guest (#654)

- **Date:** 2026-06-21
- **Issue:** [#654](https://github.com/randomparity/kdive/issues/654)
- **ADR:** [ADR-0206](../../adr/0206-modules-in-guest-shared-contract.md)
- **Status:** Approved (design)

## Problem

A local-libvirt System whose provisioning profile sets `crashkernel` selects
`CaptureMethod.KDUMP`, boots with `crashkernel=256M` reserved, and (since
[ADR-0203](../../adr/0203-local-libvirt-kdump-overlay-harvest.md)) can have a
guest-written `/var/crash/<ts>/vmcore` harvested host-side. But the arc cannot be
driven by a *real* in-guest kdump: `runs.install` rejects a KDUMP System because the
build never produces the artifact the install preflight requires, and even if it
booted, the guest has no way to assemble a crash-capture environment for the
freshly-built kernel.

The install error today:

```
install: resolved cmdline 'console=ttyS0 root=/dev/vda crashkernel=256M' (method kdump)
CategorizedError: kdump capture initramfs not staged (a separate initrd is required for kdump)
```

### Root cause

Local-libvirt direct-kernel-boots the raw `bzImage` (`root=/dev/vda`, no initramfs,
builtin virtio/ext4) into a whole-disk-ext4 qcow2, and **never installs `/lib/modules`
into the guest** (`build.py`: "A local System direct-kernel-boots, so no `/lib/modules`
bundle is produced"). Standard in-guest kdump needs the guest's `kdumpctl`/`kdump.service`
to build a crash initramfs via dracut against `/lib/modules/<running-ver>` — which the
custom kernel never installs into the guest. So `crashkernel=` is reserved but no capture
environment can be assembled.

[ADR-0055](../../adr/0055-install-readiness-kdump-seam.md) §5 made the *presence of a
staged `<initrd>`* the host-observable proxy for "capture path armed", and `steps.py`
carries an `initrd_ref` field for it — but the build never produces an initrd, so the
gate is permanently unsatisfiable for local kdump. ADR-0055 §5 itself anticipated this:
"#115's in-guest probe supersedes the gate and lifts the boundary."

### The deferred requirement

"Get kernel modules into the guest" has been deferred for local-libvirt since direct-kernel
boot with builtin drivers never needed them. Remote-libvirt already solves it: its build
runs `make modules_install`, ships a `vmlinuz + /lib/modules` bundle, and a guest-agent
helper (`kdive-install-kernel`, ADR-0081/0082) installs the kernel + modules in-guest and
runs the distro grub/dracut path; remote KDUMP capture already ships (ADR-0084). #654
retires the deferral for local rather than bolting on a kdump-only carrier.

## Decision (summary; full rationale in ADR-0206)

Converge local-libvirt on remote's outcome — **a real `/lib/modules/<ver>` in the guest** —
via a shared build-output contract with provider-specific delivery:

- **Shared contract:** a build produces a kernel image + kernel modules (+ debuginfo). Each
  provider lands the modules in the guest its own way. Remote: modules ride inside
  `kernel_ref`'s bundle, delivered by the in-guest helper (unchanged). Local: the build
  publishes a separate `modules_ref`, and the install plane injects it into the per-System
  qcow2 overlay via libguestfs.
- The crash initramfs is built **in-guest by the distro's `kdumpctl`** on both providers —
  local does not hand-roll a capture image.

This makes the guest filesystem mutable after provisioning (a deliberate property:
it lets an agent run multiple debugging tests, and establishes the host-writes-guest
seam a future "agent drives guest contents" capability builds on).

## Design

### 1. Build plane — config-driven modules artifact

When the resolved `.config` is kdump-capable (`CONFIG_CRASH_DUMP=y` — already the default
via the ADR-0096 kdump fragment), `LocalLibvirtBuild.build` runs `make modules_install`
(reusing the shared `real_run_modules_install` seam that remote already uses) into a staging
dir and publishes the `/lib/modules/<ver>` tree as a gzip tar — a **separate** `modules_ref`
artifact, because local boots the raw `bzImage` as `<kernel>` and cannot fold modules into
it. When the operator overrides the config to a non-crash-dump kernel, no modules artifact is
produced (today's behavior). No new build-profile field — the trigger is the resolved config.

`BuildOutput`, `BuildStepResult`, and `InstallRequest` gain a **new** `modules_ref: str | None`
field. `initrd_ref` is **kept as-is**: it is a live field, not dormant plumbing — the external
upload lane populates it (`complete_build.py`: `initrd_ref=finalization.keys.get("initrd")`),
the install handler reads it (`installed_initrd_ref`), and `install.py` stages it as the
domain `<initrd>` for an uploaded build that ships one. `modules_ref` (a built `/lib/modules`
tarball for host-side injection) and `initrd_ref` (an optionally-uploaded boot initrd) are
distinct artifacts and stay distinct fields. The build handler threads `modules_ref` into the
ledger result alongside the existing fields.

The slow `make modules_install` + publish path stays `live_vm`-gated; the orchestration and
publish contract (config-driven trigger fires/skips, `modules_ref` round-trips) are
unit-tested with fakes.

### 2. Install plane — libguestfs overlay injection

`LocalLibvirtInstall.install`, when the method is KDUMP and a `modules_ref` is present:
fetches the modules tarball, **force-offs the domain if it is active**, mounts the per-System
overlay (`overlay_path`, the file ADR-0203 reads) read-write via libguestfs, writes
`/lib/modules/<ver>`, runs `depmod`, and verifies a real sentinel.

- **Force-off before the rw mount.** A read-write libguestfs mount of a qcow2 a running guest
  is mutating yields inconsistent/corrupt writes — the exact hazard ADR-0203 force-offs to
  avoid. `runs.install` is admitted on any `succeeded` Run regardless of the System's power
  state (ADR-0030 §1 gates on Run state, not System state), and recovery is a new Run on the
  same System (ADR-0026 §7), so a re-install can target an already-booted, running System. So
  install must `destroy` the domain if `isActive()` before mounting — idempotent, mirroring
  `boot()`'s destroy-then-create power-cycle and ADR-0203's force-off. The subsequent `boot()`
  re-creates the domain, so force-off at install is consistent with the existing boot path.
- **Idempotent injection.** A failed install records no `run_steps` row (ADR-0030 §2), so a
  retry re-runs the whole install body, including injection. Injection must therefore be
  idempotent and self-healing of a partial prior write: clobber any existing
  `/lib/modules/<ver>` (remove the version dir, or extract to a temp dir and atomically rename)
  before extracting, and verify a **completion sentinel** — `depmod` exits 0 and the
  `modules.dep` it rewrites exists (or the extracted file count matches the tarball manifest) —
  not merely that the version directory exists (a half-written tree from a crashed prior attempt
  would false-pass a directory-presence check). The sentinel must not assume loadable modules
  exist: an all-builtin kdump kernel can leave a valid but empty `modules.dep`, so "non-empty"
  is not a safe completion signal. The exact sentinel is a plan detail.

A new injected `GuestModuleWriter`-style seam mirrors ADR-0203's `GuestCoreReader` split: the
orchestration (force-off → fetch → mount → clobber → write → depmod → verify-sentinel) is pure
and unit-tested with a fake; only the real libguestfs / `depmod` / `domain.destroy()` calls are
`# pragma: no cover - live_vm`, selected by `from_env`.

Two mechanics deferred to the implementation plan (both `live_vm`-validated): exactly how
`depmod` runs under libguestfs (appliance `command` against the guest's `kmod` vs. precomputed
`modules.dep` injection), and whether `kdumpctl` needs a nudge beyond the image's
`systemctl enable`.

### 3. Gate replacement (refines ADR-0055 §5)

`_kdump_capture_present(initrd_path)` — the staged-`<initrd>` proxy — is replaced. The new
host-observable proxy for "capture path armable" is **"modules for the run's kernel version
were injected into the overlay."** A KDUMP install with no `modules_ref` (e.g. a non-crash-dump
build) is the `configuration_error`. The production boot stays direct-kernel with **no
`<initrd>` element** — modules live in the rootfs, drivers are builtin, and `kdumpctl` builds
the crash initramfs in-guest.

### 4. Guest image — add `kdump-utils`

The local `debug` fs kind (`rootfs_command.py`) ships `drgn, kexec-tools, makedumpfile` but not
`kdump-utils` (Fedora's `kdump.service`/`kdumpctl` package that arms kexec on boot and rebuilds
the crash initramfs). Add `kdump-utils` to that package set and enable the service, so the
in-guest path works on kdive's own local image. One-line image-spec change keeps #654
end-to-end rather than dependent on a separate image issue.

### 5. Capture path — unchanged

ADR-0203's host-side overlay harvest already reads `/var/crash/<ts>/vmcore`. With modules
injected and `kdumpctl` arming kexec in-guest, a real panic now actually writes a core there;
`vmcore.fetch(method=kdump)` harvests it.

## Error handling

Uses the existing `ErrorCategory` taxonomy:

- Build: `modules_install` non-zero → `BUILD_FAILURE`; modules publish failure →
  `INFRASTRUCTURE_FAILURE`.
- Install: KDUMP method but `modules_ref` absent → `CONFIGURATION_ERROR` (the replaced gate);
  libguestfs/`depmod`/force-off failure during injection → `INFRASTRUCTURE_FAILURE` (retryable
  host fault) with the overlay path in details — and because injection is idempotent
  (clobber-then-extract + sentinel verify), the retry the worker drives after such a failure is
  safe; absent libguestfs binding → `MISSING_DEPENDENCY` (mirrors ADR-0203); a vanished
  `modules_ref` object → `STALE_HANDLE` (mirrors the kernel fetch seam).
- All injected guest output (`depmod`/libguestfs stderr) passes the redactor before any error
  snippet, per the cross-cutting invariant.

## Testing

Pure orchestration cores unit-tested with fakes:

- config-driven modules-build trigger (fires on `CONFIG_CRASH_DUMP=y`, skips otherwise);
- `modules_ref` round-trip through `BuildStepResult` (`dump`/`load`/`refs`);
- install injection orchestration (fetch → write → depmod → verify; error paths above);
- the replaced gate (KDUMP + no `modules_ref` → `configuration_error`; KDUMP + `modules_ref`
  → admitted, **no `<initrd>`** emitted in the domain XML).

Real libguestfs / `make modules_install` / `depmod` / panic→capture stays `live_vm`-gated and
runbook-validated. The four-method runbook §4b note and ADR-0203's "boot side ready"
precondition are updated.

## Scope

**In scope:** local build modules artifact; local install overlay injection + `depmod`; gate
replacement; `kdump-utils` in the local debug image; ADR-0206 + the ADR-0055 §5 refinement;
runbook update.

**Non-goals:**

- Refactoring remote's working in-guest bundle/helper — it already satisfies the shared
  contract; documented, not rebuilt.
- A general agent-driven "write arbitrary guest contents" API — #654 establishes the
  libguestfs-writes-guest seam a future capability builds on, but exposes no such tool.
- Lifting the `live_vm` gate on real capture.

## Considered & rejected

- **Build a self-contained capture initramfs host-side (dracut `kdumpbase`), stage as
  `<initrd>`.** Smallest blast radius, keeps the ADR-0055 gate — but solves only kdump capture,
  leaves local permanently divergent from remote, and does not address the general
  modules-in-guest gap (the next loadable-module need re-opens it).
- **Ship `/lib/modules` only as a third build ref but install it in-guest via a local
  guest-agent helper (mirror remote exactly).** Adds a guest-agent dependency and a localhost
  round-trip where the host already owns the overlay file directly — more moving parts on a
  same-host provider (the same reasoning ADR-0203 used to reject a local guest agent).
- **Treat the guest rootfs as immutable after provisioning.** Rejected by the maintainer:
  a mutable guest fs lets an agent run multiple debugging tests and is the foundation for the
  agent eventually driving guest contents.
- **Repurpose the `initrd_ref` field into `modules_ref` (one slot).** Rejected: `initrd_ref`
  is live — the external upload lane carries an uploaded boot initrd through it
  (`complete_build.py`) and `install.py` stages it as `<initrd>`. Folding the built modules
  tarball into that slot would break the upload lane and conflate two distinct artifacts; the
  two stay separate fields.
- **Mount the overlay read-write without checking the domain's power state.** Rejected: a
  re-install onto an already-booted System (ADR-0026 §7 recovery) would rw-mount a live qcow2 —
  the corruption hazard ADR-0203 force-offs to avoid. Install force-offs if active first.
- **Verify injection by the `/lib/modules/<ver>` directory's existence.** Rejected: a retry
  after a crashed prior attempt would false-pass on a half-written tree. Injection clobbers then
  verifies a `modules.dep` content sentinel.
