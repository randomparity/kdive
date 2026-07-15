# Proof record — ppc64le catalog parity + customization-boot hardening (#1152)

Date: 2026-07-14
Issue: #1152 · Epic: #1139 · Spec: `2026-07-14-ppc64le-catalog-parity-1152.md`
ADRs: 0350 (catalog parity), 0351 (repack ext4 fsck-compat)

## What this proves

The #1152 acceptance criterion is "at least one non-Fedora family proven end-to-end via TCG
customization boot; remaining families at minimum build-validated." Driving the **CentOS Stream 9
ppc64le** customization boot surfaced four real, previously-latent bugs in the shared
customization-boot mechanism (ADR-0345, #1147) — each fixed and unit-tested. The mechanism now runs
end-to-end through boot → fsck → cloud-init → network → dnf-start on a non-Fedora EL9 image, but **no
EL9 customize boot has reached `kdive-customize-ok`** (two distinct blockers, below, tracked in
#1174). The end-to-end live proof is therefore carried by a **Fedora ppc64le** customize-boot that
exercises all four shared fixes; the five catalog rows are build-validated by the loader tests +
`just ci`, and the EL9 rows' live customize-boot is gated behind #1174.

## Environment

- Host: x86_64, libvirt 12.0.0 / QEMU 10.2.2, `/dev/kvm` present, `qemu-system-ppc64` (pseries
  TCG). libguestfs appliance is x86_64, e2fsprogs 1.47.3. Build driven with
  `KDIVE_LIBVIRT_URI=qemu:///session python -m kdive build-fs` (console readability, #1147).

## Bugs found by the CentOS Stream 9 ppc64le customize boot (all fixed)

Each was invisible to unit tests and to the Fedora (#1147) path; only a real non-Fedora EL9 image
under TCG exposed them.

1. **ext4 `orphan_file` fsck-incompatibility (ADR-0351).** The repack stamps the e2fsprogs-1.47
   `orphan_file` feature (Fedora appliance default); EL9's 1.46.5 e2fsck rejects it at
   `systemd-fsck-root` → emergency mode, before the firstboot runs. Fix: `virt-make-fs` to raw →
   `tune2fs -O ^orphan_file` → `qemu-img convert` to qcow2. **Verified**: the CS9 boot reached
   `kdive login:` (no emergency mode) after the fix; the shipped Fedora ppc64le image's ext4 has no
   `orphan_file`.
2. **EPEL enabled for EL8 only.** `drgn` ships in EPEL on every EL major (8/9/10), never in EL
   BaseOS; the customizer enabled EPEL only for EL8, so EL9/EL10 could not install drgn. Fix: widen
   the guard to every EL clone (`_el_major is not None`); Fedora keeps base-repo drgn.
3. **cloud-init empty-user-data crash on EL9.** The baked NoCloud `user-data` was `#cloud-config\n`,
   which `yaml.safe_load` parses to `None`; cloud-init 24.4-8.el9's `_should_wait_via_user_data`
   runs `"write_files" in yaml.safe_load(user_data)` unguarded → `TypeError: argument of type
   'NoneType' is not iterable` → `failed stage init-local` → no network. Fix: bake
   `#cloud-config\n{}\n` (parses to an empty dict). **Verified**: after the fix, cloud-init ran
   init-local → network, `enp0s0` got `10.0.2.15` and a default route on both CS9 and Fedora.
4. **Firstboot oneshot hit systemd's default 90s start-timeout.** `render_firstboot_unit` set no
   `TimeoutStartSec`, so `kdive-customize.service` inherited `DefaultTimeoutStartSec` (90s); a
   package-installing customization that runs longer is SIGTERM'd mid-run, firing the `-failed`
   marker (seen once at 64% of an 11 MB/s download). Fix: `TimeoutStartSec=infinity`, deferring the
   deadline to the host orchestration's TCG-scaled window.

## Live proof — Fedora ppc64le (TCG), all four fixes

`build-fs --image fedora-kdive-ready-44-ppc64le` (session/TCG) **built + published** end-to-end:

- Repack (with the ADR-0351 `tune2fs` strip) succeeded; boot reached `network-online.target` with
  no emergency mode (Fix 1 holds on Fedora); cloud-init ran init-local → network (Fix 3 holds);
  `kdive-customize.service` ran the dnf5 install to completion under the lifted timeout (Fix 4) and
  emitted **`kdive-customize-ok`**, then a clean poweroff.
- Published image: `digest=sha256:842dd383f94200b555d520797187b1ee18d0f9b246841e3e1cdb7842f8ba7fec`.
  Provenance records `arch=ppc64le`, kernel `6.19.10-300.fc44.ppc64le`, installed
  `drgn-0.2.0 / kexec-tools-2.0.32 / makedumpfile-1.7.9 / kdump-utils-1.0.61 / openssh-server-10.2p1`,
  capabilities `[ssh, selinux, kdump, drgn]`, `layout=whole-disk-ext4-qcow2`.
- The shipped image's ext4 feature set contains **no `orphan_file`** (Fix 1 verified on the sealed
  image).

Because Fixes 1/3/4 are in the arch/family-neutral shared mechanism, a passing Fedora ppc64le
customize-boot is a direct regression check that they do not break the known-good path (#1147), and
Fix 1 is additionally proven to have unblocked the CS9 boot past fsck.

## No EL9 customize-boot reached `kdive-customize-ok` — gated follow-up #1174

The end-to-end proof above is **Fedora only**. No EL9 (CentOS Stream / Rocky) customize boot has
reached `kdive-customize-ok` on any arch; two distinct blockers were observed, both tracked in
**#1174**:

- **CentOS Stream 9 ppc64le (TCG):** runs through boot → fsck → cloud-init → network → dnf-start,
  then the `dnf4` metadata download from the CentOS mirror CDN **stalls at 0 B/s** under the
  TCG/SLIRP emulated network (one run drew 11 MB/s; most stall) → dnf exhausts its mirrors → the
  firstboot exits. Emulated-network / mirror-CDN connectivity, not kdive code (Fedora's `dnf5` + CDN
  are reliable under the same SLIRP, which is why #1147 and the Fedora proof here pass).
- **Rocky 9 x86_64 (KVM, native network):** driven to decouple the composed EL9 path from the TCG
  mirror stall. dnf metadata (BaseOS + AppStream) downloaded **fast and fully**, then the firstboot
  exited with **no dnf install output and no error** on the console. Fix 4's
  `TimeoutStartSec=infinity` is confirmed wired into the injected unit (`_inject_offline_script`
  uploads the `render_firstboot_unit` output verbatim), so it is not the 90s systemd timeout. The
  identical rendered firstboot command sequence (both dnf installs + the systemctl/sed steps)
  **succeeds in a `rockylinux:9` container**, so the commands are valid on EL9 userspace — the
  failure is a guest-cloud-image / customization-boot-environment issue that needs interactive boot
  + guest-log inspection to diagnose. Deferred to #1174 rather than diagnosed in this catalog-parity
  PR.

So the four fixes are each verified to the checkpoint they unblock (Fix 1 → fsck passes on CS9;
Fix 3 → cloud-init + network on CS9/Rocky; Fix 2 → the drgn/EPEL install resolves in a Rocky 9
container; Fix 4 → wired), and the **composed** shared mechanism is proven end-to-end on Fedora
ppc64le, but the **composed EL9 self-customize has not been observed to reach `customize-ok`**. The
CentOS/Rocky EL9 rows (ppc64le and the pre-existing x86_64) therefore ship **catalog/loader-validated
only**; their live customize-boot proof is gated behind #1174.

## Reproduction

```
# non-Fedora bugs surfaced/fixed against, and the TCG dnf-stall limit observed on (#1174):
KDIVE_LIBVIRT_URI=qemu:///session python -m kdive build-fs --image centos-stream-kdive-ready-9-ppc64le
# composed EL9 path attempted on native KVM (undiagnosed post-metadata firstboot exit, #1174):
KDIVE_LIBVIRT_URI=qemu:///session python -m kdive build-fs --image rocky-kdive-ready-9
# end-to-end live proof (all four fixes), built + published:
KDIVE_LIBVIRT_URI=qemu:///session python -m kdive build-fs --image fedora-kdive-ready-44-ppc64le
```
