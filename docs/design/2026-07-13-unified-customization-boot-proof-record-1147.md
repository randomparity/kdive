# Proof record — unified customization boot, x86_64 KVM + ppc64le TCG (#1147)

Date: 2026-07-14
Issue: #1147 · Epic: #1139 · Spec: `2026-07-13-unified-customization-boot-1147.md` · ADR-0345

The live proof required by the #1147 plan (Task 10): build a kdive-ready rootfs by booting the
image once to self-customize (ADR-0345), on both the native (x86_64/KVM) and foreign
(ppc64le/TCG-on-x86_64) paths, then provision and boot the built image. Reaching the build's
`kdive-customize-ok` marker is the live gate the faked-seam unit tests cannot provide (a missed
offline `multi-user.target.wants` enable manifests only here); the provision proves the built
image is bootable, and the x86_64 leg proves the seal's cloud-init reset kept `resize_rootfs`.

## Environment

- Host: x86_64, libvirt 12.0.0 / QEMU 10.2.2; `/dev/kvm` present; `qemu-system-ppc64` present
  (pseries TCG). libguestfs appliance is x86_64.
- The build was driven with `python -m kdive build-fs` under `KDIVE_LIBVIRT_URI=qemu:///session`
  (see "Console readability" below); provision ran through the live-stack worker
  (`qemu:///system`, root) on branch `feat/cross-arch-customization-boot-1147`.
- Catalog rows: `fedora-kdive-ready-44` (Fedora Cloud Base 44 x86_64, rhel family,
  `customize_via="boot"`), `fedora-kdive-ready-44-ppc64le` (Fedora Cloud Base 44 ppc64le). Both
  exercise the boot path.

## Findings the live proof surfaced (all fixed in this PR)

Each was invisible to the injected-seam unit tests and to the native (x86_64) path; only a real
foreign-arch image under a real hypervisor + appliance exposed them.

1. **Build disk not reachable by the hypervisor.** The customization boot attaches the in-progress
   disk (and the extracted baseline kernel/initrd) straight from the per-build workspace
   directory, which `tempfile.TemporaryDirectory` creates mode `0700`. libvirt's dynamic ownership
   relabels/chowns the disk *file* at start but never widens parent directories, so
   `qemu:///system` (uid `qemu`/107) could not traverse the scratch dir: `createXML` failed
   `Cannot access storage file … Permission denied (as uid:107)`. Fixed by
   `_grant_hypervisor_traversal` (o+x on the workspace tree, o+r on the read-only kernel/initrd)
   before `createXML`.
2. **Cross-arch-broken offline checks — the deepest failure.** The seal's unit-removed assertion
   and `verify_cloud_init` used guestfish `sh 'test …'`/`sh 'grep …'`, which exec the *guest's*
   `/bin/sh`/`grep` inside the *host-arch* appliance → `Exec format error` on a ppc64le image. This
   is the exact cross-arch limitation the whole feature exists to avoid, hidden inside the offline
   seal. The generic runner also *misdiagnosed* it as "unit was not self-removed" (the unit had in
   fact self-removed cleanly — confirmed by inspecting the rejected staged image). Fixed by
   replacing every `sh` guest-command check with libguestfs-**native** predicates
   (`is-file`/`is-symlink`/`exists`/`grep` — appliance operations on guest *data*), parsing their
   output in Python.
3. **Successful-build teardown noise.** The firstboot's own `systemctl poweroff` makes the
   transient domain vanish before teardown, so `_force_off` logged a `VIR_ERR_NO_DOMAIN` traceback
   on *every* successful build. Fixed by treating "domain already gone" as the expected end state.

### Console readability (operator precondition, not a code bug)

The completion handshake reads the serial `<log>`, which virtlogd writes `root:0600`. On this
libvirt 12 / virtlogd the ADR-0223 mitigations do **not** hold: the pre-touched worker-owned
`0644` file is unlinked+recreated `root:0600`, and the console dir's `default:user:<worker>:r` ACL
is neutralized because the `0600` create mode zeroes the ACL mask (`mask::---`). A non-root reader
therefore cannot read the handshake and cannot re-permission a root-owned file. The reliable
readers are the **worker running as root** (the deployment default here) or
**`KDIVE_LIBVIRT_URI=qemu:///session`** (session virtlogd writes the log worker-owned). The build
in this proof used `qemu:///session`. `prepare_console` is retained (it still guarantees the
console directory exists and helps on virtlogd builds that do truncate in place); its docstring and
the plan's Known-preconditions are corrected to state the version-dependence.

## Result 1 — x86_64 native (KVM)

`build-fs --image fedora-kdive-ready-44` (session/KVM): **built + published** in **81 s** total
(download + repack + normalize + offline inject + customization boot + seal + publish). Console
(`ttyS0`) captured the genuine customization: cloud-init brought up SLIRP networking
(`10.0.2.15`), `kdive-customize.service` started (proving the offline `multi-user.target.wants`
enable), dnf5 fetched the Fedora 44 repos and installed the packages, then `kdive-customize-ok` and
a clean poweroff. Published image checks: `drgn-0.2.0`, `kexec-tools`, `makedumpfile-1.7.9`
installed; `kdive-customize.service` + script self-removed; `/var/lib/cloud/{instances,…}` reset;
`/.autorelabel` touched.

Provision (worker, `qemu:///system`) of the built image → **ready**, then in the running guest
over the worker-loopback SSH forward:

```
$ df -h /
/dev/vda        9.8G  1.2G  8.1G  13% /          # grew from the ~6G base to the 10G alloc disk
$ rpm -q drgn kexec-tools makedumpfile
drgn-0.2.0-1.fc44.x86_64
kexec-tools-2.0.32-3.fc44.x86_64
makedumpfile-1.7.9-1.fc44.x86_64
$ systemctl is-enabled kdive-ready.service kdump.service
enabled / enabled
```

The `9.8G` root filesystem is the load-bearing signal: `resize_rootfs` ran at provision, which
only happens because the seal reset cloud-init's per-instance state so the provision boot sees a
fresh instance (ADR-0312). A failed seal would have left `/` at ~6G.

## Result 2 — ppc64le foreign (TCG on x86_64)

`build-fs --image fedora-kdive-ready-44-ppc64le` (session/TCG): **built + published** in **3 m
52 s** (`digest=sha256:4cbbe2399fbbebb245a5c2741c0bf60809abbdfc4ee0d7261795d81eedc15ac7`). Console
(`hvc0`, spapr-vty) captured a real ppc64le kernel booting under TCG, cloud-init + SLIRP
networking, dnf fetching the **ppc64le** repos, `kdive-customize-ok`, and clean poweroff. Published
image (verified with arch-safe `is-file`/virt-inspector, since `rpm -q` cannot exec cross-arch):
`/usr/sbin/kexec` + `/usr/sbin/makedumpfile` present, drgn helper present, `kdive-ready.service`
present, **`kdive-customize.service` gone** (self-removed), `/.autorelabel` touched, cloud-init
instances reset; rpm db lists kexec-tools/makedumpfile/drgn.

Provision (worker, `qemu:///system`, `arch=ppc64le`) → **ready**, persisted **`accel=tcg`**, then
**SSH-reachable** (worker `check_ssh_reachable` returned an SSH banner) — the load-bearing proof
that the customization-boot-built ppc64le image boots end-to-end to userspace under TCG with sshd
up. This extends the #1144 proof (which used a file-injection scaffold) to a real `build-fs`-built
image.

## Deadline default

`KDIVE_LIBVIRT_CUSTOMIZATION_BOOT_WINDOW_S` stays **1800 s**. The measured native-KVM
customization time (~40–50 s) and the ppc64le TCG time (~3.5 min wall; the customize boot is a
fraction of the 3 m 52 s total) sit far under the base window; at the TCG multiplier (×10) the
ceiling is generous. The default is intentionally conservative to absorb mirror/network fetch
variance rather than re-pinned to the fast-mirror measurement, which would risk false timeouts on a
slow-mirror day. No change.

## Reproduction

```
# build (as the worker identity; here via session to read the console as a non-root user)
KDIVE_LIBVIRT_URI=qemu:///session python -m kdive build-fs --image fedora-kdive-ready-44
KDIVE_LIBVIRT_URI=qemu:///session python -m kdive build-fs --image fedora-kdive-ready-44-ppc64le
# provision each built image through the running worker and assert ready + resize/reachable
# (allocate → systems.provision{rootfs:{kind:local,path:<built>}} → await ready → df / | check_ssh_reachable)
```
