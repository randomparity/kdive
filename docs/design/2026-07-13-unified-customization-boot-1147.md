# Unified customization boot: replace virt-customize execution with a boot-to-self-customize mechanism

Date: 2026-07-13
Status: approved (design)
Issue: #1147 (epic #1139)
ADR: [0345](../adr/0345-unified-customization-boot.md)
Supersedes: parent design decision 5 in
[`2026-07-13-ppc64le-full-support.md`](2026-07-13-ppc64le-full-support.md)

## Goal

Build every kdive-ready rootfs by **booting the image once and letting it customize
itself**, instead of running the guest's package manager inside a host-arch libguestfs
appliance. One mechanism serves native (x86_64 under KVM) and foreign (ppc64le under TCG)
builds, and generalizes to future bare-metal installs and developer-specified custom setup.

This started as issue #1147 ("cross-arch customization boot, Fedora family only"). During
design the operator widened it: rather than *add* a second, foreign-only method beside
`virt-customize`, **unify on the boot method for all families** and delete the
`virt-customize --install/--run-command` execution path. That decision and its rationale
are recorded in ADR-0345; this spec is the mechanism.

## Why unify (not add a parallel path)

`virt-customize --install` executes the guest's `dnf`/`apt` — guest-arch code — inside
libguestfs's **host-arch** appliance. libguestfs refuses this cross-arch outright:

> `virt-customize: error: host cpu (x86_64) and guest arch (ppc64le) are not compatible,
> so you cannot use command line options that involve running commands in the guest.
> Use --firstboot scripts instead.`

(Red Hat BZ#1264835; the appliance boots its own host-arch kernel via supermin, and
`binfmt_misc` is per-kernel so a host `qemu-ppc64le-static` never reaches it.) A foreign-only
firstboot path *beside* the native `virt-customize` path would leave two customization
methods to maintain. Keeping both is the larger liability: as targets multiply
(bare-metal soon, where `virt-customize` has no analog), the appliance-execution path
becomes progressively more isolated. Unifying removes a whole renderer **and** the
guest-code-in-appliance surface; the boot method is also higher fidelity even natively
(the guest's real kernel + real `dnf`, not a chrooted `dnf` under the appliance kernel with
its known `uname -r`/scriptlet quirks). See ADR-0345 for the qemu-user-static rejected
alternatives (both the libguestfs-refusal and a host-chroot variant).

## Scope (two PRs)

The `virt-customize` argv path can only be deleted once **both** families are converted;
until then it stays live for the un-converted one. To keep PRs small and independently
live-validated, the work is two PRs:

- **PR #1147 (this spec):** build the shared mechanism; convert the **rhel** family
  (Fedora/RHEL) to it; live-prove native x86_64 (KVM) **and** ppc64le (TCG) on the x86_64
  host. The debian family keeps its `virt-customize` argv path unchanged (a brief transient
  two-method state).
- **PR (fast-follow, new issue):** convert the **debian** family to the mechanism; **delete**
  the `virt-customize --install/--run-command` execution path and the argv renderer;
  live-prove native x86_64 (KVM).

Out of scope for both PRs: virt-builder (non-cloud) bases on the boot path (see Network,
below); remote-libvirt; bare-metal (the mechanism is designed to extend there later).

## Design

### One step list, two renderers

A family stops emitting a flat `virt-customize` argv and instead emits **one ordered list of
typed customization steps** — the single source of truth for *what* the customization does.
Two renderers consume that list, differing only in *where* each step runs:

Step kinds (initial set, sufficient for both families):

| Step | Meaning | Offline-injector target | Argv-renderer target |
|------|---------|-------------------------|----------------------|
| `Mkdir(path)` | create a directory | guestfish `mkdir-p` | `--mkdir` |
| `WriteFile(path, content, mode?)` | write file content | guestfish `write`/`chmod` | `--write` (+`--chmod`) |
| `UploadFile(host_src, dest, mode?)` | upload a host file | guestfish `upload`/`chmod` | `--upload` (+`--chmod`) |
| `InstallPackages(names)` | install packages | firstboot: `dnf -y install …` | `--install a,b,c` |
| `RunCommand(sh)` | run a shell command | firstboot: the command | `--run-command` |
| `EnableUnit(name)` | enable a systemd unit | firstboot: `systemctl enable` | `--run-command 'systemctl enable …'` |

- **Offline injector (rhel, new path).** Applies `Mkdir`/`WriteFile`/`UploadFile` **now**,
  file-level via guestfish (arch-safe — no guest execution). Collects
  `InstallPackages`/`RunCommand`/`EnableUnit`, in order, into the **firstboot script** that
  the customization boot runs. This is the minimal in-guest blast radius: the pure file
  writes (cloud.cfg drop-in, NoCloud seed, machine-id, sysctl, kdump.conf edit, readiness
  unit, drgn helper) never execute anything.
- **Argv renderer (debian, transient).** Maps *every* step to `virt-customize` argv, byte-
  identical to today. Deleted in the fast-follow PR. A regression test pins its output.

The family's existing `customize_argv` is replaced by `customize_steps(ctx) -> list[Step]`
for rhel. debian keeps `customize_argv` until it is converted. The shared
`_fedora_customize.py` primitives (cloud-init args, kdump pin, markers, drgn helper) are
refactored to build `Step`s; the argv fragments they return today are produced by the argv
renderer over those same `Step`s, so there is one definition.

### Pipeline reordering (boot path)

A direct-kernel customization boot can only boot the **whole-disk-ext4** `root=/dev/vda`
layout (ADR-0030/0272). Today `virt-customize` runs on the *partitioned* cloud base
(`scratch`) before repack. The boot path therefore reorders: **repack + normalize first**,
then boot the finished-layout image to self-customize.

```
acquire_base -> scratch (partitioned cloud base)
  -> repack whole-disk-ext4 -> staged
  -> normalize (fstab=/dev/vda, rm crypttab, SELINUX=permissive)   [pre-boot; NO /.autorelabel yet]
  -> inject offline steps + firstboot unit                          [guestfish, arch-safe]
  -> boot transient domain kdive-build-<uuid> under accel (KVM native / TCG foreign)
       firstboot: install pkgs, enable units, record versions,
                  self-remove unit, echo kdive-customize-ok, poweroff
  -> await ok-marker | fail on failure-marker / hard-panic / crashed-domstate / timeout (+tail)
  -> seal: force-off + undefine domain; reset cloud-init per-instance state;
           touch /.autorelabel (SELinux); assert firstboot unit removed
  -> provenance probes read from `staged`
  -> verify_cloud_init -> publish
```

`normalize` deliberately does **not** touch `/.autorelabel` on this path (see seal-time
details below): the build boot runs under permissive with the repack-dropped labels, which is
harmless, and only the provision boot relabels.

The base cloud image ships a bootable kernel + initramfs in `/boot`; `select_kernel_and_initrd`
(ADR-0272) picks it for the direct-kernel `<kernel>`/`<initrd>`. The debug/build package sets
install no kernel, so `boot_kernel_count` stays 1 (provisionable) after customization.

Provenance probes (`inspect_versions`, makedumpfile/drgn markers, boot facts, os-release) read
the customized `staged` image, not `scratch` (which is never customized on this path).

### Boot-customize-seal orchestration

Assembled from existing local-libvirt seams (all injected for unit tests):

- **Build-boot identity (a build is not a System).** The reused seams
  (`render_domain_xml`, the serial `<log>` sink, `classify_console`/domstate polling) are keyed
  on a UUID. A customization build has no `system_id`, so the orchestration mints a **per-build
  UUID** and drives those seams with it: the transient domain is named `kdive-build-<uuid>`
  (namespaced away from `kdive-<system-uuid>` provision domains) and its console log resolves
  from the same UUID. Distinct per-build UUIDs give **concurrent-build isolation** — two builds
  on one host never collide on domain name or console path, and never collide with a
  provisioned System. The transient domain is **force-off + `undefine`d on every exit path**
  (success, failure-marker, hard-panic, timeout, crash) so no build domain is ever left defined.
- **Render + start.** `render_domain_xml(build_uuid, ..., accel, emulator)` with an egress NIC,
  then `defineXML`+`create` (the `_define_and_start` pattern). The serial `<log append="off">`
  sink gives a truncated per-boot console automatically.
- **Network on the customization boot.** The kdive DHCP `cloud.cfg` drop-in + NoCloud seed are
  `WriteFile` steps injected offline *before* the boot, so cloud-init brings up deterministic
  DHCP over the SLIRP NIC on the customization boot itself — not dependent on the vendor base's
  default (which kdive already distrusts). The firstboot unit is
  `After=network-online.target Wants=network-online.target`. **Cloud-image bases only**: a
  virt-builder base ships no cloud-init to bring the network up on first boot, so it is out of
  scope for the boot path and documented as such (rhel catalog rows are all cloud images).
- **Completion handshake — distinct markers, fast-fail.** The firstboot script writes to the
  arch console device (`ttyS0`/`hvc0`, via `arch_traits`, as the readiness unit already does):
  - success: `set -e`; install/enable/version-markers → **disable + `rm` its own unit &
    script** → `echo kdive-customize-ok > /dev/<console>` → `systemctl poweroff`.
  - failure: an `ERR`/`EXIT` trap echoes `kdive-customize-failed` + the last error lines →
    `poweroff` immediately (never waits the full timeout on a broken install).

  The two build markers are **distinct** from the provision-time `kdive-ready` marker.
- **Poll — the explicit marker is authoritative, not a heuristic regex.** The provision-boot
  `classify_console` crash regex (`readiness.py` `_CRASH_SIGNATURE`) matches `detected stall`
  and `soft lockup` — lines a **slow TCG guest emits benignly under load** while running `dnf`
  + a kdump initramfs rebuild. Reusing it verbatim would spuriously fail the exact ppc64le-TCG
  build this feature must prove. So the customization boot does **not** reuse that regex.
  Failure is signalled authoritatively by:
  - `kdive-customize-ok` → success → force-off + undefine → seal.
  - `kdive-customize-failed` (the ERR-trap marker) **or** a libvirt **`crashed`** domstate
    **or** a hard-panic-only console pattern (`Kernel panic`, GPF, KASAN — patterns that cannot
    fire benignly) **or** shutoff-without-ok-marker → `PROVISIONING_FAILURE` +
    `redacted_console_tail` (the normal evidence path).
  - window elapsed → `BOOT_TIMEOUT` + console tail.

  Benign `detected stall` / `soft lockup` lines are explicitly **excluded** from the build-boot
  classifier; the guest's own `set -e`/ERR trap is the real install-failure detector.
- **Deadline — measured, not "high".** A dedicated customization-boot window (new operator-
  tunable `KDIVE_*` setting) covers `dnf install` of the debug set **plus** the kdump initramfs
  rebuild, × `tcg_deadline_multiplier(accel)` for foreign. The default is **pinned to the
  native-KVM customization time measured in the live proof** (recorded in the proof record) with
  margin — not an arbitrary constant — so it is falsifiable. The live proof also records whether
  the boot-tuned TCG multiplier suffices for this download+install+initramfs workload or needs a
  dedicated factor. **In-guest package fetch:** `dnf` install now runs in the guest over the
  SLIRP NIC (host uplink, NAT'd — the same upstream mirrors virt-customize used, not a new
  network dependency). Transient mirror/GPG failures rely on `dnf`'s built-in retries and, on
  exhaustion, fail the build via the ERR-trap marker with the console tail as evidence — never a
  silent timeout. A host-side package cache/proxy is a documented future optimization, not
  required for correctness.

### Seal-time details the reordering forces

The seal runs **offline** (guestfish) after the transient domain is force-off + undefined, on
the customized `staged` image, before publish:

1. **Reset cloud-init per-instance state — else `resize_rootfs` is skipped at provision.** The
   customization boot runs cloud-init to completion for the baked NoCloud instance-id (a
   **constant** `kdive-rootfs`, `_fedora_customize.py`). cloud-init records once-per-instance
   modules as done under `/var/lib/cloud/instances/<id>`. If that state ships in the image, the
   provision boot sees the *same* instance-id as already-initialized and **skips** cc_resizefs
   (`resize_rootfs`, ADR-0312/#985) — silently losing the disk-grow guarantee the build even
   asserts is enabled. Fix: seal removes `/var/lib/cloud/instances`, the `instance` symlink,
   `/var/lib/cloud/sem`, and `/var/lib/cloud/data` so the provision boot is a genuine cloud-init
   first boot. A test/live assertion confirms `resize_rootfs` actually runs at provision.
2. **SELinux relabel happens only at provision — not on the build boot.** `normalize` does
   **not** touch `/.autorelabel` before the build boot; the build boot runs under permissive
   with the repack-dropped labels (harmless — permissive raises no denials on unlabeled files),
   so there is no in-build relabel, no autorelabel-triggered reboot, and no relabel time to
   budget. Seal touches `/.autorelabel` **once, post-boot**, so the *provision* boot relabels
   everything — the base tree and everything customization installed. (rhel/SELinux families
   only; debian/AppArmor needs no relabel.)
3. **Self-removal is guest-side on the success path.** Any failure discards the whole image (the
   build works in a scratch workspace and publishes atomically), so a guest that never reached
   self-removal never ships. A cheap offline `guestfish` assert that the firstboot unit is gone
   before publish is defense-in-depth.

## Testing

**Unit (injected seams — no libguestfs/qemu):**
- rhel `customize_steps` produces the correct typed step list, parametrized over kind
  (debug/build) and EL-major (Fedora vs EL8/9 package divergence, EPEL).
- The offline injector applies file-ops via a fake guestfish and renders the exec-ops into the
  expected firstboot script — asserting `set -e` + `ERR`/`EXIT` trap, self-removal of the unit,
  the arch console device, and the two markers.
- **debian argv-renderer regression guard:** its `virt-customize` argv is byte-identical to
  today (debian is not converted yet; this proves no accidental drift).
- boot-customize-seal orchestration against fake domain/console seams:
  ok-marker→seal; failure-marker→`PROVISIONING_FAILURE`+tail; hard-panic pattern→fail;
  `crashed` domstate→fail; timeout→`BOOT_TIMEOUT`+tail; shutoff-without-ok-marker→fail;
  deadline scales by `tcg_deadline_multiplier(accel)`.
- **build-boot classifier excludes benign TCG output:** a console region containing
  `rcu: … detected stalls` / `watchdog: BUG: soft lockup …` **without** the ok/failed marker is
  *not* classified as a failure (only the markers, `crashed` domstate, hard-panic patterns, and
  timeout are).
- **seal steps invoked:** cloud-init per-instance state removed; `/.autorelabel` touched exactly
  once (post-boot, not before); firstboot-unit-removed assertion runs; transient domain
  undefined on every exit path (success/failure/timeout/crash).
- **build-boot identity:** the transient domain is `kdive-build-<uuid>` with a UUID-derived
  console path; two concurrent builds get distinct names/paths.

**Live proof (x86_64 host):**
- **x86_64 KVM** — build a Fedora x86_64 kdive-ready image *via customization boot* (the native
  path now boots too), then provision + boot it, **asserting `resize_rootfs` runs at provision**
  (the cloud-init-state-reset guarantee). This is the native no-regression evidence, redefined
  as **behavioral** (the image still provisions, boots, and grows its disk) rather than
  byte-identical. The measured native-KVM customization time pins the deadline default.
- **ppc64le TCG** — build the Fedora ppc64le Cloud Base into a kdive-ready image via TCG
  customization boot, then provision + boot. The original #1147 acceptance criterion. The proof
  records whether the boot TCG multiplier covered the download+install+initramfs workload.

## Epic re-sequencing

- ADR-0345 supersedes parent design decision 5 ("virt-customize remains the native-arch path").
  The `2026-07-13-ppc64le-full-support.md` decision 5 and the #8 sub-issue row are updated to
  reference it; the x86_64-byte-identical criterion is intentionally dropped (replaced by the
  behavioral criterion above).
- A new fast-follow issue is filed for the debian conversion + argv-path deletion.
- Build / image-lifecycle docs note the customization-boot mechanism.

## Known limitations

- **virt-builder (non-cloud) bases** are not supported on the boot path this PR — no cloud-init
  to bring the network up on the first customization boot. rhel catalog rows are cloud images,
  so this affects nothing shipped; a non-cloud network bring-up (e.g. an injected
  systemd-networkd unit) is a follow-up if a virt-builder row is ever added.
- Native builds now boot the full guest OS to customize — modestly slower than the supermin
  appliance under KVM (the `dnf` install + kdump initramfs rebuild dominates either way) and a
  new failure mode on the build host (which is already a libvirt host).
