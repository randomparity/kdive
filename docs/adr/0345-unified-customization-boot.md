# ADR 0345 — Unify rootfs customization on a boot-to-self-customize mechanism; retire virt-customize execution

- **Status:** Accepted
- **Date:** 2026-07-13
- **Issue:** #1147
- **Epic:** #1139 (full ppc64le support)
- **Supersedes:** parent design decision 5 in
  `docs/design/2026-07-13-ppc64le-full-support.md` ("virt-customize remains the native-arch
  path")
- **Builds on:** ADR-0251 (family customizer seam), ADR-0288 (cloud-init first boot),
  ADR-0272/0030 (baseline direct-kernel boot, whole-disk-ext4 layout), ADR-0340 (accel-derived
  domain XML), ADR-0341 (TCG deadline scaling), ADR-0342 (#1144 live TCG boot proof)

## Context

`LocalLibvirtRootfsBuildPlane` customizes a base image by running `virt-customize
--install/--run-command`, which executes the guest's `dnf`/`apt` — guest-arch code — inside
libguestfs's **host-arch** appliance. That is fine when guest arch == host arch. It is
impossible cross-arch: libguestfs's appliance boots its own host-arch kernel (supermin), and
`binfmt_misc` is a per-kernel-instance feature, so a host `qemu-ppc64le-static` never reaches
the appliance. libguestfs refuses the operation outright:

> `virt-customize: error: host cpu (x86_64) and guest arch (ppc64le) are not compatible, so
> you cannot use command line options that involve running commands in the guest. Use
> --firstboot scripts instead.`

(Red Hat BZ#1264835; Ubuntu LP#1864164.) Producing ppc64le kdive-ready images on the x86_64
host therefore cannot use the `virt-customize` execution path at all.

Issue #1147 was originally scoped to *add* a foreign-only customization-boot path beside the
native `virt-customize` path (parent design decision 5: "virt-customize remains the native-arch
path"). That leaves **two** customization methods to maintain. The operator reconsidered and
chose to **unify**: as debug/provisioning targets multiply — bare-metal installs are next,
where `virt-customize` has no analog — the appliance-execution path becomes progressively more
isolated and harder to maintain, and the boot method generalizes cleanly (it mirrors how a
developer provisions a debug box and opens a seam for developer-specified custom setup). This
ADR records that decision and supersedes decision 5.

## Decision

**Build every kdive-ready rootfs by booting the image once and letting it customize itself,
and retire the `virt-customize --install/--run-command` execution path.** The rollout is two
PRs (rhel first, then debian + deletion) so the change is small and independently
live-validated per family; the argv path is deleted only once both families are converted.

Concretely (the mechanism is specified in
`docs/design/2026-07-13-unified-customization-boot-1147.md`):

- **One step list, two renderers.** A family emits one ordered list of typed customization
  `Step`s (`Mkdir`/`WriteFile`/`UploadFile`/`InstallPackages`/`RunCommand`/`EnableUnit`) — the
  single source of truth for *what* the customization does. An **offline injector** applies the
  pure file steps now (guestfish, arch-safe) and collects the execution steps into a firstboot
  script; a transitional **argv renderer** maps every step to `virt-customize` argv (byte-
  identical to today) for the not-yet-converted family. Only genuine execution — package
  install, unit enable, version-marker probes — runs in-guest; every file write stays offline.
- **Pipeline reordering (boot path).** A direct-kernel boot requires the whole-disk-ext4
  `root=/dev/vda` layout, so the boot path repacks + normalizes **before** the customization
  boot, then boots the finished-layout image to self-customize, then seals. Provenance probes
  read the customized `staged` image.
- **Completion handshake — the explicit marker is authoritative, not a heuristic.** The
  firstboot script self-removes its own unit, then echoes `kdive-customize-ok` to the arch
  console (`ttyS0`/`hvc0`) and powers off; an `ERR`/`EXIT` trap echoes `kdive-customize-failed` +
  the error tail and powers off immediately, so a broken install fails fast instead of burning
  the full timeout. These build markers are distinct from the provision-time `kdive-ready`
  marker. The customization-boot crash classifier is **subtractive**: it keeps the provision
  `_CRASH_SIGNATURE` genuine faults (`Oops:`, `unable to handle kernel`, `KFENCE:`, GPF,
  `KASAN:`, `Kernel panic`) but removes the two watchdog patterns a starved TCG vCPU emits
  benignly under load (`detected stall`, `BUG: soft lockup`) — so a real oops that wedges the
  guest fast-fails while benign stalls do not false-fail the exact ppc64le-TCG path this feature
  proves. Failure is the `kdive-customize-failed` marker, a genuine-fault pattern, the domain
  settling (shut off or crashed, via the crashed-aware domstate probe) without the ok-marker, or
  timeout — no pvpanic is rendered, so a panic is caught by settled-without-ok rather than a
  separate "crashed" branch. The deadline is a measured value (the live-proof native-KVM
  customization time × a 3× margin absorbing mirror/network fetch variance) ×
  `tcg_deadline_multiplier(accel)`; failure surfaces the bounded console tail.
- **Build-boot identity, transient + auto-destroy.** A build is not a System, so the
  orchestration mints a per-build UUID and names the domain `kdive-build-<uuid>` (namespaced
  from provision domains, giving concurrent-build isolation). The domain is created **transient**
  via `createXML(VIR_DOMAIN_START_AUTODESTROY)` — never persisted (nothing to `undefine` or
  leak) and auto-destroyed when the worker connection drops, so even a mid-build worker SIGKILL
  (#583) cannot leave a defined build domain behind; no reaper is needed. Its **corollary** is
  load-bearing: AUTODESTROY ties the domain to the creating connection, so the build holds **one**
  libvirt connection open across the whole poll+seal — not the open/close-per-op pattern the
  reused seams use. It renders `on_reboot=destroy` (a guest reboot mid-customization fast-fails
  rather than re-running/looping) and `restrict=off` (`guest_egress=True`) **unconditionally** —
  the build's mirror fetch is decoupled from the provision-time ADR-0313 operator egress policy
  (the build boot runs the vendor image + kdive firstboot, the same trust as today's
  `virt-customize` fetch). A **dedicated `render_customization_domain_xml` + `build_domain_name`**
  emit the `kdive-build-<uuid>` name (`render_domain_xml`/`domain_name_for` stay System-only — the
  former needs a `ProvisioningProfile` + SSH forward); that form is already excluded from
  System-name parsing, so the reconciler ignores build domains.
- **Three seal-time details the reordering forces (all offline, post-boot).** (1) The build boot
  runs cloud-init to completion for the *constant* NoCloud instance-id, so seal removes
  `/var/lib/cloud/{instances,instance,sem,data}` — else the provision boot sees the instance as
  already-initialized and skips once-per-instance modules, notably `resize_rootfs`
  (ADR-0312), silently losing the disk-grow guarantee. (2) `normalize` does **not** touch
  `/.autorelabel` before the build boot (permissive tolerates the repack-dropped labels, so no
  in-build relabel/reboot); seal touches it once so only the *provision* boot relabels what
  customization added (SELinux/rhel only). (3) Self-removal is guest-side on the success path
  only — any failure discards the whole image; an offline assert the unit is gone before publish
  is defense-in-depth.

The x86_64-byte-identical acceptance criterion of decision 5 is **intentionally dropped**:
native builds now boot to customize, so their behavior changes by design. It is replaced by a
behavioral criterion — the built image still provisions and boots — proven live under KVM.

## Consequences

- One customization method for every arch and (after the fast-follow) every family; the guest-
  code-in-appliance surface and a whole renderer are removed. The mechanism extends to
  bare-metal installs and to future developer-specified custom setup.
- Higher customization fidelity even natively: the guest's real kernel + real package manager
  run, not a chrooted `dnf` under the appliance kernel (no `uname -r`/scriptlet quirks).
- Native builds gain a full-guest-boot dependency: modestly slower under KVM (the `dnf` install
  + kdump initramfs rebuild dominates regardless) and a new failure mode on the build host
  (already a libvirt host). Failures are surfaced via the console tail, not a silent timeout.
- A brief transient two-method state (rhel=boot, debian=argv) exists between the two PRs; it
  ends when the fast-follow deletes the argv path.
- virt-builder (non-cloud) bases are unsupported on the boot path (no cloud-init to bring up
  first-boot networking); rhel catalog rows are cloud images, so nothing shipped is affected.
- No migration; no schema change. Build orchestration and the family seam change; the domain-XML
  / deadline / console machinery is reused unchanged.

## Rejected alternatives

- **qemu-user-static via `virt-customize` (register `binfmt_misc` + `qemu-ppc64le-static`, run
  the existing `--install` cross-arch).** Rejected because it *cannot work*: libguestfs's
  appliance runs its own host-arch kernel and does not inherit the host's `binfmt_misc`, and
  libguestfs guards against cross-arch guest commands at the tool level regardless (BZ#1264835).
  libguestfs itself points at firstboot scripts.
- **qemu-user-static via a host chroot (mount the ppc64le qcow2 on the host, copy in
  `qemu-ppc64le-static`, register `binfmt_misc` with the `F` flag, `chroot` + run `dnf` under
  user-mode emulation).** Technically possible but rejected: `rpm` scriptlets that fork/thread
  are the classic failure mode for user-mode QEMU; it needs a privileged host qcow2 mount and a
  host-global `binfmt` registration (new machinery and attack surface kdive does not have); and
  it reuses none of kdive's already-proven full-system TCG boot path. Full-system emulation runs
  the guest as-designed with no syscall-translation gaps and reuses the #1144/#1146 machinery.
- **Keep `virt-customize` for native and add a foreign-only boot path (parent decision 5).**
  Rejected: two customization methods are the larger long-term liability. As targets multiply
  (bare-metal next, with no `virt-customize` analog), the appliance path only grows more
  isolated. Unifying deletes a renderer and the guest-code-in-appliance surface, and is higher
  fidelity even natively.
- **Convert both families and delete the argv path in one PR.** Rejected in favor of two PRs:
  changing the native build path for every family at once, with live re-validation of every
  image, is a large non-bisectable change. Per-family PRs keep each conversion independently
  provable; the transient two-method state is short and ends at the fast-follow.
- **Put the whole customization (including file writes) in the firstboot script.** Rejected:
  pure file writes are arch-safe and need no execution, so injecting them offline via guestfish
  minimizes the in-guest blast radius (fewer failure points during the boot) and matches the
  issue's "file ops are arch-safe" framing. The typed step list keeps one source of truth while
  letting each renderer place a step where it belongs.
- **Poweroff-only completion (no explicit failure marker).** Rejected: a failed `dnf` that still
  powers off cleanly is then indistinguishable from success and burns the full (TCG-scaled)
  timeout before the build gives up. The distinct `kdive-customize-failed` marker + `ERR` trap
  makes failure fast and unambiguous.

## Rollout

Two PRs. PR #1147 lands the shared mechanism, converts rhel, and live-proves native x86_64
(KVM) + ppc64le (TCG) on the x86_64 host. The fast-follow converts debian and deletes the
`virt-customize` execution path + argv renderer, live-proving native x86_64. No migration; the
change is build-orchestration + the family seam. This ADR moves to **Accepted** when PR #1147
merges (the mechanism + first family); the argv-path retirement completes at the fast-follow.
