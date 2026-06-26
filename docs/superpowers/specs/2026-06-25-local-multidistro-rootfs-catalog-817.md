# Local multi-distro rootfs catalog (issue #817)

- **Issue:** [#817](https://github.com/randomparity/kdive/issues/817)
- **ADR:** [ADR-0251](../../adr/0251-local-multidistro-rootfs-catalog.md)
- **Status:** Accepted design; MVP (Fedora 44 + mechanism + incomplete-core handling) lands under #817,
  the other families as staged follow-ups in the same epic.

## Problem

On local-libvirt, `vmcore.fetch` with the default `kdump` method fails with `no complete core
appeared within the capture window` for a from-source kernel, while the explicit `host_dump`
method succeeds. Live diagnosis on the KVM host (2026-06-25) pinned the cause precisely:

- The ready rootfs (`fedora-kdive-ready-43.qcow2`) ships **makedumpfile 1.7.8** (Fedora 43,
  `LATEST_VERSION = 6.17.4`). For a kernel **7.0.0** it prints *"The kernel version is not
  supported"* and cannot do `-d 31` page filtering, so on a large-RAM guest the unfiltered dump
  overruns the capture window and stays `/var/crash/<ts>/vmcore-incomplete`.
- The host-side harvest glob is exactly `/var/crash/*/vmcore`, which never matches
  `vmcore-incomplete` → `READINESS_FAILURE` "no complete core appeared within the window."

Everything else is healthy and was ruled out live: crashkernel=256M reserves, `kdumpctl` arms
(`kexec_crash_loaded=1`), the capture kernel boots, mounts ext4, runs makedumpfile, and powers off
(`final_action poweroff`). The **baseline Fedora 6.18.5 kernel captures a complete vmcore**; only
the from-source 7.0 kernel fails. makedumpfile **1.7.9** (released 2026-04-20) is the first release
to "Support kernels up to v7.0 (x86_64)" and ships in **Fedora 44** (GA 2026-04-28).

The image is a bare ext4 whole-disk rootfs (no partition table or bootloader); the provider
direct-kernel-boots it and injects `/boot/vmlinuz-<ver>` + `/lib/modules/<ver>` so in-guest
`kdumpctl` can `kexec` a capture kernel.

## Goal

Two coupled outcomes:

1. **Fix #817:** ship a Fedora 44 rootfs whose makedumpfile filters a 7.0 vmcore, proven live.
2. **Generalize:** turn the single hardcoded Fedora rootfs build into a declarative multi-distro
   catalog (Fedora, Rocky, CentOS Stream, Debian, openSUSE) so the lifecycle can be exercised
   across base OSes, with kdump-capture available wherever a distro's makedumpfile is new enough.

A distro whose makedumpfile is older than the kernel-under-test will reproduce the *same*
`vmcore-incomplete` outcome; that is expected and surfaced clearly (see Incomplete-core handling),
not hidden — `host_dump` remains the documented path there.

## Scope

### MVP (this issue, #817)

1. **Catalog mechanism** — `fixtures/local-libvirt/rootfs_catalog.toml` (file-authoritative),
   a loader/validator, a base-source acquirer (virt-builder template *or* cloud-image URL with a
   pinned sha256), and a `FamilyCustomizer` seam.
2. **Fedora 44 entry** (`fedora-kdive-ready-44`), built live and proven to capture a complete 7.0
   vmcore via the default `kdump` method.
3. **Retain** `fedora-kdive-ready-43` as a regression reference.
4. **Incomplete-core handling** — detect `vmcore-incomplete` and return a categorized, actionable
   `READINESS_FAILURE` instead of the opaque window-timeout message.

Only the **`rhel`** family customizer ships in the MVP (covers Fedora 43/44 and the later
Rocky/CentOS entries).

**Build/de-risk ordering (MVP).** The cloud-image→bare-ext4 path for Fedora 44 was never exercised
in diagnosis (which reused the existing 43 image), so it is proven *first*, as a manual/scripted
spike (download → customize → repack → boot, the same way the diagnosis built and booted images by
hand — no `build-fs` wiring yet), before the catalog/family abstraction is generalized:
(1) hand-build a Fedora 44 bare-ext4 rootfs from the cloud qcow2 and boot + kdump-prove it, settling
the btrfs/cloud-init/SELinux-relabel mechanics; (2) only then refactor the inline Fedora
customization into the `rhel` customizer + catalog loader + base-source seam so `build-fs --image
fedora-kdive-ready-44` reproduces the proven image; (3) add the incomplete-core handling
(independently unit-testable). This front-loads the highest-risk unknown.

### Follow-ups (same epic, same design)

- RHEL family entries: Rocky 8/9/10 + CentOS Stream 9/10 (reuse `rhel`).
- `debian` customizer + Debian 12/13 (apt, `kdump-tools`, `update-initramfs`, AppArmor).
- `suse` customizer + openSUSE Tumbleweed (kdump-capable, newest makedumpfile) and Leap 15.6.

Each follow-up entry is live-proven for its lifecycle; the makedumpfile-vs-kernel limitation is
documented per release (and the first follow-up that renders it adds the structured capability flag).

## Follow-up realization: #823 — RHEL family entries (Rocky 8/9/10 + CentOS Stream 9/10)

The first follow-up adds five catalog rows reusing the `rhel` `FamilyCustomizer`, sourced from their
**GenericCloud qcow2s** (sha256-pinned `cloud-image` source, the same lane proven for Fedora 44),
makes the `rhel` family **EL-version-aware** (the MVP's package set was Fedora-shaped), and
**renders the structured capability flag** the MVP deferred.

### EL-version-aware `rhel` packaging (the load-bearing change)

"Reuse `rhel`" holds at the dnf level but the MVP's package set was Fedora-specific. Verified
against the distro package indexes (2026-06-26):

| EL major | makedumpfile + `kdumpctl` | `drgn` | kdump-enable unit |
|---|---|---|---|
| 8 (Rocky 8) | bundled in `kexec-tools` (no separate pkg) | EPEL only (`epel-release` is in the default-enabled `extras` repo) | `kdump.service` (kexec-tools) |
| 9 (Rocky/CentOS Stream 9) | bundled in `kexec-tools` (no separate pkg) | BaseOS/AppStream | `kdump.service` (kexec-tools) |
| 10 (Rocky/CentOS Stream 10) | **separate** `makedumpfile` pkg + `kdump-utils` (like Fedora) | BaseOS/AppStream | `kdump.service` (kdump-utils) |

`RhelFamily.packages(kind, distro, version)` therefore returns an EL-major-aware debug set:

- **Fedora and EL ≥ 10:** `drgn kexec-tools makedumpfile kdump-utils keyutils openssh-server`
  (unchanged from the MVP — separate `makedumpfile`/`kdump-utils` exist).
- **EL 8 / EL 9:** `drgn kexec-tools keyutils openssh-server` — `makedumpfile` and `kdumpctl` come
  from `kexec-tools`; the standalone `makedumpfile`/`kdump-utils` packages do not exist, so
  installing them by name would fail the build. **EL 8** additionally runs `dnf -y install
  epel-release` (a separate transaction, before the `drgn` install, so the EPEL repo metadata is
  present) because `drgn` is not in EL 8 BaseOS/AppStream.

Two MVP gates that keyed on Fedora package names are corrected: the kdump-enable block now gates on
`"kexec-tools" in packages` (present in every debug set, absent from the build set) rather than
`"kdump-utils" in packages`; the install list (and provenance `packages`) is the actually-installed
set, so provenance stays falsifiable. `CustomizeContext` gains `distro`/`version` so the family can
emit the EL-8 EPEL step. The `build` kind set is generic toolchain packages available on every EL,
unchanged.

### `kdump_capable` flag (the rendered capability)

`RootfsCatalogEntry` gains a required `kdump_capable: bool` field, parsed and validated by the
loader. It describes the **makedumpfile the build installs from the release's repos at build time**
(the bundled `kexec-tools` makedumpfile on EL 8/9, the separate `makedumpfile` pkg on EL ≥ 10 /
Fedora) — **not** the frozen sha256-pinned base, which the build updates from. `true` iff that
makedumpfile is **≥ 1.7.9**, the first release supporting an x86_64 **v7.0-class kernel**.

Two explicit preconditions of the boolean, named so it is not over-read:

- **Kernel-relative.** It is `true`/`false` *for the current default from-source kernel target*
  (v7.0-class). It is not an absolute "kdump works" flag: the same makedumpfile 1.7.8 image that is
  `false` here would produce a complete filtered core for an older (e.g. v6.x) kernel it does
  support. The flag answers "does the default `kdump` `vmcore.fetch` produce a *complete filtered*
  core for the v7.0-class kernel-under-test, or does it hit the incomplete-core remediation?"
- **Snapshot, not live truth.** The documented makedumpfile version is a point-in-time snapshot of a
  mutable upstream repo that the build re-pulls on every run. When a release ships makedumpfile
  ≥ 1.7.9, a fresh build silently becomes capable while the curated flag still reads `false` until
  re-verified. The flag is a curated default that can **lag** a distro's makedumpfile bump; the
  runtime `kdump_core_incomplete` remediation (which fires on the actual harvest) is the ground
  truth, not the flag.

The flag is **rendered** in the operator image table in
[`../../operating/runbooks/image-lifecycle.md`](../../operating/runbooks/image-lifecycle.md) and
**guarded** by `tests/images/test_rootfs_catalog.py`, which carries the authoritative per-entry
makedumpfile version (a documented snapshot, verified against distro package indexes 2026-06-26) and
asserts each row's `kdump_capable == (makedumpfile_version ≥ 1.7.9)`. The guard catches an
*internally inconsistent* edit (a flag flipped without bumping the documented version); it does not
and cannot detect upstream drift — re-verification is a manual, dated step.

### Verified makedumpfile matrix (2026-06-26)

| catalog name | base | makedumpfile (build-time) | source | `kdump_capable` (v7.0) |
|---|---|---|---|---|
| `fedora-kdive-ready-44` | Fedora 44 | 1.7.9 | `mdapi.fedoraproject.org/f44` | **true** |
| `fedora-kdive-ready-43` | Fedora 43 | 1.7.8 | `mdapi.fedoraproject.org/f43` | false |
| `rocky-kdive-ready-10` | Rocky 10.2 | 1.7.8 | Rocky 10 BaseOS `makedumpfile-1.7.8-1.el10` | false |
| `rocky-kdive-ready-9` | Rocky 9.8 | 1.7.6 | `kexec-tools-2.0.29` (bundled, c9s spec) | false |
| `rocky-kdive-ready-8` | Rocky 8.10 | 1.7.2 | `kexec-tools-2.0.26` (bundled, c8s spec) | false |
| `centos-stream-kdive-ready-10` | CentOS Stream 10 | 1.7.8 | c10s BaseOS `makedumpfile-1.7.8-1.el10` | false |
| `centos-stream-kdive-ready-9` | CentOS Stream 9 | 1.7.6 | `kexec-tools-2.0.29` (bundled, c9s spec) | false |

None of the EL releases ship makedumpfile ≥ 1.7.9 yet (1.7.9 published 2026-04-20, EL distros lag),
so **every #823 entry is `kdump_capable = false`** for the v7.0-class kernel-under-test. That is the
expected, disclosed outcome: their lifecycle proof covers provision/build/install/boot/`host_dump`,
and the default `kdump` path lands on the cause-neutral `kdump_core_incomplete` remediation (which
names `host_dump` and a newer image). Fedora 44 remains the only kdump-capable default.

### Live-proof preconditions (carry the MVP's negative-proof rigor)

A `kdump_capable = false` entry does **not** universally fail the default `kdump` path: per the
MVP live-proof gate, makedumpfile 1.7.8 on a *small* (4 GB) guest can still write a complete
**unfiltered** core that fits the window — the window-overrun only manifests at large RAM. So the
false-entry proof reuses the MVP's pinned **large** guest RAM, and the disclosure assertion is "the
default `kdump` path lands on `kdump_core_incomplete`" *at that RAM*; the pass signal is the
remediation (and the in-guest "kernel version is not supported" console line), not file existence.
The lifecycle planes (provision/build/install/boot/`host_dump`) are proven independently of RAM.

### Naming and registration

Rows follow the `fedora-kdive-ready-NN` convention: `rocky-kdive-ready-{8,9,10}` and
`centos-stream-kdive-ready-{9,10}` (`distro = "rocky"` / `"centos-stream"`). `distro`/`version` are
provenance metadata for a `cloud-image` row (the URL carries the base) and now also drive the
family's EL-major package decision; no new `virt-builder` templates are involved. Each row registers
in the inventory example the way Fedora 44 does.

### Live-proof results (#823, KVM host, 2026-06-26)

`build-fs --image <name>` was run live on the KVM host for all five entries (real
download + sha256-verify + `virt-customize` + `virt-tar-out`/`virt-make-fs` repack + guestfish
normalize + publish), and each built rootfs was inspected with guestfish. This exercises the
**build** lifecycle plane and the novel EL-major-aware `rhel` packaging end-to-end:

| entry | built digest (sha256, abbrev) | makedumpfile **in image** | drgn source | kdump/sshd/kdive-ready |
|---|---|---|---|---|
| `rocky-kdive-ready-8` | `5faef213…` | **1.7.2** (in `kexec-tools-2.0.26`) | `drgn-0.0.32-1.el8` from **EPEL** | all `enabled` |
| `rocky-kdive-ready-9` | `e16538a8…` | **1.7.6** (in `kexec-tools-2.0.29`) | `drgn-0.0.33-2.el9` (AppStream) | all `enabled` |
| `rocky-kdive-ready-10` | `80a035d9…` | **1.7.8** (`makedumpfile-1.7.8-1.el10` + `kdump-utils`) | `drgn-0.0.33-1.el10` | all `enabled` |
| `centos-stream-kdive-ready-9` | `9a3667f8…` | **1.7.6** (in `kexec-tools-2.0.29`) | `drgn-0.0.33-2.el9` | all `enabled` |
| `centos-stream-kdive-ready-10` | `4831fb80…` | **1.7.8** (`makedumpfile-1.7.8-1.el10` + `kdump-utils`) | `drgn-0.0.33-1.el10` | all `enabled` |

Confirms: (1) the EL-8 `epel-release`-before-`drgn` ordering works in `virt-customize` (the `drgn`
install would otherwise fail — drgn is not in EL 8 base); (2) EL 8/9 take makedumpfile + `kdumpctl`
from `kexec-tools` while EL 10 installs the standalone `makedumpfile`/`kdump-utils`; (3) the
**in-image makedumpfile version matches the documented matrix exactly** for every entry (all < 1.7.9
→ `kdump_capable = false` is correct against the real image); (4) `kdump.service` arms via the
`kexec-tools` gate (not the Fedora-only `kdump-utils`), and `sshd`/`kdive-ready` enable, the managed
key injects, and SELinux is permissive on every entry.

**Boot proof (EL9, direct-kernel on the v7.0.0 kernel-under-test).** `rocky-kdive-ready-9` was
direct-kernel-booted on the from-source **kernel 7.0.0** (`-cpu max,la57=off`, the model the kdive
libguestfs appliance uses — the default `qemu64` lacks the x86-64-v2 baseline EL9 glibc requires and
SIGILLs PID1): the kernel mounts `/dev/vda` ext4 with no initramfs (built-in virtio_blk/ext4),
pivots, `systemd 252-67.el9.rocky.0.1` reaches `Multi-User System`, the **`kdive-ready` serial
signal fires**, and it reaches the `Rocky Linux 9.8 … Kernel 7.0.0` login prompt. `crashkernel=256M`
reserved correctly. kdump.service's arming **failed** here because the ad-hoc boot skipped the kdive
**install** plane that injects `/lib/modules/7.0.0` (dracut needs them to build the capture
initramfs) — and notably `kdive-ready` **still fired**, confirming on an EL guest that the
`After=kdump.service` ordering (ADR-0251 point 6) releases readiness on the unit's terminal state
whether kdump arms or not.

The remaining lifecycle (provision → install the v7.0 kernel + modules → `force_crash` →
`host_dump`, and the default `kdump` path landing on `kdump_core_incomplete` at the pinned large RAM
once modules are injected and kdump arms) runs through the operator live-stack harness (the
env-gated, non-CI path the image-lifecycle runbook describes); the install/boot/kdump-capture
machinery is distro-agnostic and was proven on Fedora in the #817 MVP — the #823-specific risk was
the EL package divergence (and its boot), proven above.

## Follow-up realization: #824 — `debian` customizer + Debian 12/13 entries

The second follow-up adds a `debian` `FamilyCustomizer` and two catalog rows
(`debian-kdive-ready-12`, `debian-kdive-ready-13`) sourced from their **genericcloud qcow2s**
(sha256-pinned `cloud-image` source, the same lane proven for Fedora 44 and the #823 EL entries).
Unlike #823 (which reused `rhel`), Debian's packaging and init divergences need a distinct family;
the divergences below are verified against the Debian package database and manpages (2026-06-26).

### The load-bearing divergence: the kdump unit name (a generalization of point 6)

Debian's kdump arms through **`kdump-tools.service`**, not RHEL's `kdump.service`. Point 6 closes the
arm-vs-ready race by ordering the `kdive-ready` serial unit `After=kdump.service`. On Debian that edge
would name a unit that does not exist — and "ordering against an absent unit is a no-op", so the race
point 6 fixed would silently reopen: a `force_crash` on a just-`ready` Debian System could hit an
unarmed kdump and capture nothing.

The fix makes the readiness unit's kdump ordering **family-parameterized**. The `FamilyCustomizer`
gains a `kdump_unit: str` attribute (`rhel` → `kdump.service`, `debian` → `kdump-tools.service`); the
shared readiness unit is rendered with the family's unit so the `After=` edge always names the real
kdump unit. `After=` stays pure ordering, so a build image without that unit is still unaffected.

### `debian` packaging and customization (verified 2026-06-26)

| concern | `rhel` | `debian` |
|---|---|---|
| install | dnf (`--install`) | apt (`--install`, same virt-customize verb) |
| debug crash pkgs | `drgn kexec-tools makedumpfile kdump-utils keyutils openssh-server` | `makedumpfile kdump-tools crash python3-drgn openssh-server` |
| kdump enable | `systemctl enable kdump.service` | `systemctl enable kdump-tools.service` + `USE_KDUMP=1` in `/etc/default/kdump-tools` |
| capture initramfs | dracut (`kdumpctl`) | initramfs-tools (`update-initramfs`) |
| NMI-panic sysctl | `kernel.unknown_nmi_panic=1` (generic) | `kernel.unknown_nmi_panic=1` (identical, shared constant) |
| sshd unit | `sshd.service` | `ssh.service` |
| drgn package | `drgn` (CLI) | `python3-drgn` (ships `/usr/bin/drgn`, so the `kdive-drgn` helper's `drgn -k` works) |
| MAC | SELinux permissive + first-boot relabel | AppArmor — **no relabel** |

Notes on the non-obvious choices:

- **AppArmor handling in `normalize` is "no relabel".** AppArmor is profile-based (loaded from
  `/etc/apparmor.d/` by `apparmor.service` at boot), not xattr-labeled like SELinux, so the
  `virt-tar-out`/`virt-make-fs` repack does not strip it and no `/.autorelabel` is needed. The default
  Debian policy leaves **sshd unconfined**, so a host-injected `/root/.ssh/authorized_keys` is not
  blocked. Debian genericcloud ships **no `/etc/selinux/config`**, so the `debian` `normalize` does
  the fstab/crypttab rewrite only and deliberately touches neither SELinux nor AppArmor. The provenance
  records this: the build-pipeline `guest_selinux` field is generalized to **`guest_mac`** (`rhel` →
  `selinux-permissive`, `debian` → `apparmor`), exposed by the family.

- **cloud-init disable is version-proof.** Debian 13's newer cloud-init renamed
  `cloud-init.service` → `cloud-init-network.service`, so masking a fixed unit-name list would silently
  miss a stage on trixie. The `debian` lane instead drops **`/etc/cloud/cloud-init.disabled`** (cloud-init
  checks for this file and no-ops regardless of unit names) — one write, correct on both releases.

- **machine-id seed (cloud-image only).** Debian genericcloud ships an empty `/etc/machine-id`, which
  systemd treats as first boot and runs `preset-all`, which can reset `kdump-tools.service` to its
  vendor preset — the same hazard that disabled kdump on Fedora Cloud. The lane seeds the same fixed
  machine-id the `rhel` lane uses, so kdump stays armed on first boot.

- **No NetworkManager keyfile.** Debian genericcloud uses ifupdown + cloud-init's
  `cloud-ifupdown-helper`, which DHCPs each NIC automatically; it does not ship NetworkManager, so the
  `rhel` SSH-NIC NM keyfile (ADR-0218) would be inert. The `debian` debug image stages the reviewed
  `kdive-drgn` helper (the introspection contract) but **not** the NM keyfile — the extra SSH NIC the
  drgn-live transport renders is DHCP'd by the cloud helper.

### `kdump_capable` — both Debian entries are `false`

| catalog name | base | makedumpfile (build-time) | source | `kdump_capable` (v7.0) |
|---|---|---|---|---|
| `debian-kdive-ready-12` | Debian 12 (bookworm) | 1.7.2 | bookworm `makedumpfile 1:1.7.2` | false |
| `debian-kdive-ready-13` | Debian 13 (trixie) | 1.7.6 | trixie `makedumpfile 1:1.7.6` | false |

Neither ships makedumpfile ≥ 1.7.9, so both disclose the cause-neutral `kdump_core_incomplete`
remediation on the default `kdump` path for the v7.0-class kernel-under-test — the same posture as every
#823 entry. Their lifecycle proof covers provision/build/install/boot/`host_dump`; Fedora 44 remains the
only kdump-capable default. The per-row makedumpfile snapshot is added to the
`tests/images/test_rootfs_catalog.py` guard so a flag flipped without bumping the documented version fails.

**Post-dump action.** Debian's kdump-tools has no `final_action poweroff` analog (RHEL pins it so the
guest self-shuts-off, the host harvest's reliable completion signal); Debian governs capture-kernel
reboot/halt via the generic `kernel.panic` sysctl. For a `kdump_capable = false` entry the default
`kdump` path lands on the incomplete-core remediation regardless, and `host_dump` (host-side QEMU
`dump-guest-memory`) is the proven capture and does not depend on in-guest kdump timing — so the
`debian` customizer enables kdump-tools and sets `USE_KDUMP=1`/the NMI sysctl (enough for kdump to arm
and leave a `vmcore-incomplete`) without pinning a poweroff action. A Debian poweroff-on-dump pin is left
to the follow-up that first ships a kdump-capable Debian (makedumpfile ≥ 1.7.9).

### Naming, registration, and live-proof

Rows follow the convention: `debian-kdive-ready-{12,13}` (`distro = "debian"`, `version = "12"`/`"13"`,
`family = "debian"`), sourced from the **versioned** genericcloud serial qcow2 (not the rotating
`latest/` path, whose content changes on each point release and would break the sha256 pin), pinned by
sha256 computed from the downloaded image and cross-checked against Debian's published `SHA512SUMS`. Each
registers in the inventory example the way the #823 entries do. The build lifecycle is live-proven on the
KVM host (`build-fs --image <name>`: real download + sha256-verify + `virt-customize` + repack +
guestfish normalize + publish, then guestfish inspection of the built rootfs), and an EL-style direct-kernel
boot on the v7.0.0 kernel-under-test confirms the `kdive-ready` serial signal fires and kdump-tools arms;
the remaining capture lifecycle runs through the operator live-stack harness as for #823.

## Architecture

All changes are in the shared `images` layer and the local-libvirt provider.

### Catalog (`fixtures/local-libvirt/rootfs_catalog.toml`)

```toml
[[image]]
name    = "fedora-kdive-ready-44"
distro  = "fedora"
version = "44"
family  = "rhel"
arch    = "x86_64"
kind    = "debug"            # debug guest | build host
source  = { kind = "cloud-image",
            url  = "https://download.fedoraproject.org/pub/fedora/linux/releases/44/Cloud/x86_64/images/Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2",
            sha256 = "<pinned>" }

[[image]]
name    = "fedora-kdive-ready-43"
distro  = "fedora"
version = "43"
family  = "rhel"
arch    = "x86_64"
kind    = "debug"
source  = { kind = "virt-builder", template = "fedora-43" }
```

`source.kind = "virt-builder"` carries a `template`; `source.kind = "cloud-image"` carries a
`url` + `sha256`. The catalog is the single place an operator adds an image.

### `images/rootfs_catalog.py`

Loader/validator. Parses the catalog into typed `RootfsCatalogEntry`. Validates: unique `name`,
known `family` (`rhel|debian|suse`), `source.kind ∈ {virt-builder, cloud-image}` with the matching
required fields (template, or url+sha256). Resolves `--image <name>` to an entry. Replaces the
single-purpose `images/distros.py` template resolver.

### `images/base_source.py`

Acquire the base qcow2 into a worker temp `scratch`:
- `virt-builder <template> --output scratch` (existing mechanism, templated releases), or
- download `url` → verify `sha256` (fail-closed `CONFIGURATION_ERROR` on mismatch) → `scratch`.
  An unreachable URL (404 / network failure) raises a `CONFIGURATION_ERROR` naming the dead URL
  and that the catalog pin must be repinned — distinct from the sha256-mismatch error.

**Cloud-image base is NOT assumed equivalent to the virt-builder scratch.** A Fedora Cloud Base
image differs materially from the single-ext4 virt-builder scratch the existing repack was
validated against: its root filesystem is **btrfs with subvolumes**, it has a separate `/boot`
(and EFI ESP), and **cloud-init is enabled**. The cloud-image lane therefore must:
1. **disable/remove cloud-init** during customize (it otherwise waits on a datasource at boot —
   like the zram stall seen in diagnosis — and can reset network/ssh, clobbering the injected key);
1b. **seed `/etc/machine-id`** with a valid id (PROVEN in the Task-1 spike): Fedora Cloud ships
   `machine-id="uninitialized"`, which makes systemd treat first boot specially and run
   `systemctl preset-all`, resetting `kdump.service` to its vendor preset (**disabled**) so kdump
   never arms — the `kexec_crash_loaded=0` / "Kdump is not operational" failure. The virt-builder
   F43 scratch already carries a populated machine-id, which is why it was never reset. Seeding it
   makes kdump auto-arm at boot. (A `system-preset` file enabling kdump is the alternative; seeding
   machine-id is simpler and matches F43.)
2. let the existing `virt-tar-out` (root tree → tar) / `virt-make-fs` (tar → bare ext4) collapse
   the btrfs+multi-partition source into the one whole-disk ext4 the provider direct-kernel-boots,
   then **SELinux-relabel** the result (tar→ext4 drops the source's security xattrs);
3. be **proven first** (see Plan ordering): building `fedora-kdive-ready-44` from the cloud qcow2
   and booting + kdump-proving it is a de-risking spike that runs *before* the catalog/family
   abstraction is generalized — the cloud→bare-ext4 path was never exercised in diagnosis.

### `images/families/`

```python
class FamilyCustomizer(Protocol):
    family: str
    def packages(self, kind: str) -> tuple[str, ...]: ...
    def customize_argv(self, ctx: CustomizeContext) -> list[str]: ...
    def normalize(self, qcow2: Path) -> None: ...   # per-family fstab/MAC normalize + SELinux/AppArmor
```

`customize_argv` returns the family-specific `virt-customize` fragment; the shared pipeline
concatenates the universal bits (ssh-inject the managed key, upload + enable the `kdive-ready`
oneshot unit, disable cloud-init on cloud-image sources). The MVP ships `rhel`; the existing inline
Fedora customization in `rootfs_build.py` moves here. `debian` and `suse` are added by their
follow-ups; the protocol exists now so those PRs are additive.

**`kdump_capable` is deferred (YAGNI).** The MVP does *not* add a `kdump_capable` field/method or
any operator-facing capability surface — it would be unused machinery. The makedumpfile-vs-kernel
limitation is conveyed at runtime by the Incomplete-core handling remediation (which fires exactly
when it matters) and in prose docs for the entries. A structured capability flag is added by the
first follow-up that actually renders it (e.g. the operator image table for the Rocky/Debian
entries), where it has a concrete surface and a guard test.

| Concern | `rhel` | `debian` | `suse` |
|---|---|---|---|
| install | dnf | apt | zypper |
| crash pkgs | `drgn kexec-tools makedumpfile kdump-utils keyutils` | `makedumpfile kdump-tools crash` | `makedumpfile kdump kexec-tools drgn` |
| kdump enable | `systemctl enable kdump.service` | `kdump-tools` | `systemctl enable kdump.service` |
| initramfs | dracut (kdumpctl) | `update-initramfs` | dracut |
| final-action/NMI | kdump.conf `final_action poweroff` + `unknown_nmi_panic=1` | `/etc/default/kdump-tools` | `/etc/sysconfig/kdump` |
| sshd | `sshd` | `ssh` | `sshd` |
| MAC | SELinux permissive | AppArmor | SELinux permissive |

### `rootfs_build.py`

Pipeline: **acquire base (source) → virt-customize (family argv + ssh-inject + kdive-ready) →
repack whole-disk ext4 (existing) → normalize (family hook) → publish + provenance**. Provenance
records `source_image_digest = "cloud-image:<url>@sha256:<digest>"` or `"virt-builder:<template>"`.

### `rootfs_command.py`

`build-fs --image <name>` resolves a catalog entry (the primary path). The existing
`--distro/--releasever/--name/--dest/--kind/--package` flags stay as overrides and back-compat for
the default image.

### Incomplete-core handling (`providers/local_libvirt/retrieve.py`)

`_LibguestfsCoreReader.list_vmcores` additionally globs `/var/crash/*/vmcore-incomplete`. The
harvest still prefers a complete `vmcore`; an incomplete core is **not** promoted (a truncated /
unfiltered core is unreliable for `crash`/drgn). When no complete `vmcore` exists but an incomplete
one does, `capture` raises `READINESS_FAILURE` with a structured, drift-proof `details`:

```
reason      = "kdump_core_incomplete"
remediation = "an incomplete kdump core was found: the in-guest capture did not finish a complete
               core. Common causes: in-guest makedumpfile is older than the kernel-under-test, or
               the capture exceeded the window. Retry with method=\"host_dump\", or use a rootfs
               image whose makedumpfile supports this kernel (e.g. fedora-kdive-ready-44)."
```

**The remediation is cause-neutral on purpose.** `vmcore-incomplete` is also the *transient* name
kdump writes during a normal save, renaming to `vmcore` only on makedumpfile success (observed for
both 43 and 44 in diagnosis). On the harvest timeout path (`_real_wait_for_vmcore` force-offs after
the settle window), the host can read a still-being-written `vmcore-incomplete` from a capture that
was merely slow, not toolchain-incompatible. So the single `kdump_core_incomplete` reason must not
assert "makedumpfile too old" as fact — it names both likely causes and points at the two escapes
(`host_dump`, newer image). The wording is one shared constant interpolating no guest output. The
genuinely-empty `/var/crash` case keeps its existing `_no_core` message. Callers distinguish three
outcomes: complete core → success; incomplete core → the cause-neutral remediation; no core →
existing readiness failure.

### Inventory

The built image registers like `fedora-kdive-ready-43` today (systems.toml example +
`admin/default_fixtures` / image_catalog seed), so a System can boot it as its rootfs.

## Testing

### Fast tests (CI, no host)

- `rootfs_catalog.py`: good rows; unknown family; missing source fields; bad `kind`; virt-builder
  vs cloud-image shape; duplicate name.
- `base_source.py`: sha256 match passes; mismatch → `CONFIGURATION_ERROR` fail-closed; unreachable
  URL (404/network) → distinct `CONFIGURATION_ERROR` naming the dead URL (mock the downloader; no
  network in CI).
- `rhel` customizer: `customize_argv` for a kdump debug image (package set, kdump enable, sysctl,
  final_action) — behavior, not brittle string-exactness.
- `rootfs_build.py`: orchestration with all seams faked — ordering, provenance content, family
  normalize hook runs (not a hardcoded SELinux edit).
- `retrieve.py` incomplete-core handling: fake reader for {only `vmcore`}, {only `vmcore-incomplete`},
  {neither}, {both} → success / `kdump_core_incomplete` / existing `_no_core` / prefers complete.
- Inventory/guard tests: `fedora-kdive-ready-44` validates like 43.

### Live-proof gate (`live_vm`, this host, not CI)

The proof must **distinguish the fix from the bug**, not merely observe a file named `vmcore` — in
diagnosis, makedumpfile 1.7.8 still produced a complete `vmcore` on a small (4 GB) guest, because
the window-overrun failure only manifests when the unfiltered dump is large. So both proofs run at
a **pinned, large guest RAM** sized so 1.7.8 leaves `vmcore-incomplete`, and the pass/fail signal is
**makedumpfile behavior on the in-guest console**, not file existence:

1. **Reproduce the failure first.** On `fedora-kdive-ready-43` at the pinned RAM, force_crash +
   default `kdump` `vmcore.fetch`, and confirm (a) the in-guest console shows *"The kernel version
   is not supported"*, and (b) the fetch returns the `kdump_core_incomplete` remediation — i.e. the
   harvest sees `vmcore-incomplete` and the incomplete-core handling path fires. This is the
   negative-proof; if 43 captures cleanly the RAM is too small and the proof is invalid.
2. **Prove the fix.** Build `fedora-kdive-ready-44` from the F44 cloud qcow2; at the *same* RAM,
   force_crash + default `kdump` `vmcore.fetch`, and assert (a) the console shows **no** "kernel
   version is not supported" line, (b) a **complete** filtered `vmcore` is harvested, and (c)
   `postmortem.triage` runs on it.
3. Capture the console + transcript evidence for both, the way the diagnosis did.

## Considered & rejected

- **Pin/limit the kernel-under-test to what makedumpfile supports** — defeats the purpose
  (debugging arbitrary from-source kernels).
- **Widen the capture window / promote `vmcore-incomplete` to a core** — masks a truncated,
  unreliable dump as success; the newer makedumpfile is the real fix and the incomplete-core handling path
  discloses honestly.
- **Code registry in `distros.py`** — adding an image becomes a code change and drifts from the
  project's file-authoritative catalog convention.
- **Unify onto the ansible `kdive_image_catalog`** — that catalog is host-inventory (group_vars,
  remote full-disk images), not app-level; local needs the bare-ext4 repack. Reuse the *shape*,
  not the file.
- **Only Fedora 44 (no catalog)** — leaves the next distro a one-off again; the user's goal is a
  reusable multi-distro matrix.
