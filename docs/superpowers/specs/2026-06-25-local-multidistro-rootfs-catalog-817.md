# Local multi-distro rootfs catalog (issue #817)

- **Issue:** [#817](https://github.com/randomparity/kdive/issues/817)
- **ADR:** [ADR-0250](../../adr/0250-local-multidistro-rootfs-catalog.md)
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

### Follow-ups (same epic, same design)

- RHEL family entries: Rocky 8/9/10 + CentOS Stream 9/10 (reuse `rhel`).
- `debian` customizer + Debian 12/13 (apt, `kdump-tools`, `update-initramfs`, AppArmor).
- `suse` customizer + openSUSE Tumbleweed (kdump-capable, newest makedumpfile) and Leap 15.6.

Each follow-up entry is live-proven for its lifecycle; `kdump_capable` is set per release.

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

The cloud-image base is a full-disk image; the existing `virt-tar-out` / `virt-make-fs` repack
already extracts the root tree and rebuilds a bare ext4, independent of the source partition
layout, so no per-source repack change is needed.

### `images/families/`

```python
class FamilyCustomizer(Protocol):
    family: str
    def packages(self, kind: str) -> tuple[str, ...]: ...
    def customize_argv(self, ctx: CustomizeContext) -> list[str]: ...
    def normalize(self, qcow2: Path) -> None: ...   # per-family fstab/MAC normalize + SELinux/AppArmor
    def kdump_capable(self, version: str) -> bool: ...
```

`customize_argv` returns the family-specific `virt-customize` fragment; the shared pipeline
concatenates the universal bits (ssh-inject the managed key, upload + enable the `kdive-ready`
oneshot unit). The MVP ships `rhel`; the existing inline Fedora customization in `rootfs_build.py`
moves here. `debian` and `suse` are added by their follow-ups; the protocol exists now so those
PRs are additive.

`kdump_capable(version)` is **disclosure metadata**, not a runtime gate: it returns `False` for a
release whose makedumpfile can't reach current kernels (Fedora 43, Rocky 8/9, Debian 12, Leap), so
the inventory entry is documented "host_dump for bleeding-edge kernels." The worker still attempts
kdump if asked; the Incomplete-core handling path gives the clear failure when the kernel is too new.

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
remediation = "in-guest makedumpfile could not complete a filtered core for this kernel
               (often makedumpfile older than the kernel-under-test). Retry with
               method=\"host_dump\", or use a newer rootfs image (e.g. fedora-kdive-ready-44)."
```

The wording is one shared constant interpolating no guest output. The genuinely-empty `/var/crash`
case keeps its existing `_no_core` message. Callers now distinguish three outcomes: complete core →
success; incomplete core → too-old-makedumpfile remediation; no core → existing readiness failure.

### Inventory

The built image registers like `fedora-kdive-ready-43` today (systems.toml example +
`admin/default_fixtures` / image_catalog seed), so a System can boot it as its rootfs.

## Testing

### Fast tests (CI, no host)

- `rootfs_catalog.py`: good rows; unknown family; missing source fields; bad `kind`; virt-builder
  vs cloud-image shape; duplicate name.
- `base_source.py`: sha256 match passes; mismatch → `CONFIGURATION_ERROR` fail-closed (mock the
  downloader; no network in CI).
- `rhel` customizer: `customize_argv` for a kdump debug image (package set, kdump enable, sysctl,
  final_action) — behavior, not brittle string-exactness.
- `rootfs_build.py`: orchestration with all seams faked — ordering, provenance content, family
  normalize hook runs (not a hardcoded SELinux edit).
- `retrieve.py` incomplete-core handling: fake reader for {only `vmcore`}, {only `vmcore-incomplete`},
  {neither}, {both} → success / `kdump_core_incomplete` / existing `_no_core` / prefers complete.
- Inventory/guard tests: `fedora-kdive-ready-44` validates like 43; `kdump_capable` flags consistent.

### Live-proof gate (`live_vm`, this host, not CI)

1. `build-fs --image fedora-kdive-ready-44` builds from the F44 cloud qcow2.
2. Drive the lifecycle, force_crash, `vmcore.fetch` with the **default (kdump)** method.
3. Assert a **complete** vmcore is captured and `postmortem.triage` runs on it.
4. Re-run on `fedora-kdive-ready-43` and confirm the harvest returns the `kdump_core_incomplete`
   remediation (negative-proof of the incomplete-core handling path).

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
