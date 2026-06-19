# ADR 0183 — Provider-aware platform `root=` cmdline (and XFS in the kdump fragment) for remote boot

- **Status:** Accepted
- **Date:** 2026-06-19
- **Deciders:** KDIVE maintainers
- **Refines:** [ADR-0061](0061-boot-cmdline-composition.md) (boot cmdline composition), [ADR-0082](0082-remote-install-in-guest-kernel.md) (remote in-guest kernel install)

## Context

With #594's console fix deployed, the remote-libvirt boot failure in #587 was diagnosed directly from
the captured boot console (135,664 bytes). The freshly-installed kdive kernel boots far enough to start
systemd in the initramfs, then fails to mount the real root and drops to `emergency.target`; the guest
agent never starts, so the boot-readiness probe times out (`boot_timeout`). The console establishes two
independent, jointly-required causes:

**Cause 1 — the platform appends a second `root=` that overrides the correct one.** The composed kernel
cmdline is provider-agnostic: `services/runs/steps.py` defines
`_REQUIRED_BASE_CMDLINE = "console=ttyS0 root=/dev/vda"` and `cmdline_for` prepends it to the build's
debug cmdline for **every** provider. For remote-libvirt the in-guest helper installs the kernel with
`grubby --add-kernel … --args="$cmdline" --copy-default`: `--copy-default` already inherits the base
image's correct `root=UUID=…` from the existing default GRUB entry, and the platform's `--args` then
layers `root=/dev/vda` on top. The console shows the resulting entry carrying **both**:

```
# base kernel (boots OK):
… root=UUID=7ae24950-9d2c-4848-9f9b-18f3be2543e6 ro … console=ttyS0,115200
# kdive kernel (fails):
… root=UUID=7ae24950-…-18f3be2543e6 ro … console=ttyS0 root=/dev/vda crashkernel=256M
```

The kernel honors the **last** `root=`, so it tries to mount the whole disk `/dev/vda` (a GPT partition
table, no filesystem) — the console's `EXT4-fs (vda): VFS: Can't find ext4 filesystem` / `FAT-fs` /
`ISOFS` probes on `vda` all fail. `root=/dev/vda` is **local-libvirt's** convention: that provider does a
direct-kernel boot (the libvirt domain XML `<os><cmdline>` is the *entire*, authoritative cmdline with
no in-guest bootloader, and its rootfs overlay is a whole-disk ext4 image, `local_libvirt/rootfs_build.py`
`_FSTAB = "/dev/vda / ext4 …"`). The remote base image is **partitioned** (`vda1` EFI, `vda2` `/boot`,
`vda3` root) and its root is addressed by `root=UUID=…`, which its GRUB entry already carries. The
device value `root=/dev/vda` is therefore a property of the **boot medium**, not of the platform.

**Cause 2 — the built kernel has no XFS support, but the base image root is XFS.** The base image root
(`vda3`) and `/boot` (`vda2`) are XFS V5 (base kernel console: `XFS (vda3): Mounting V5 Filesystem`,
`SGI XFS with ACLs …`). The kdive build is `make x86_64_defconfig` + the `kdump` config fragment, and
`arch/x86/configs/x86_64_defconfig` enables `CONFIG_EXT4_FS` but **not** `CONFIG_XFS_FS`. The failed
boot prints no `SGI XFS` banner and never probes XFS — XFS is not compiled in. virtio-blk is present
(the failed kernel does reach `vda`), so the only missing root driver is XFS. Even with Cause 1 fixed
(correct `root=UUID=…` → `vda3`), the kernel could not mount an XFS root.

Both must be fixed: cmdline-only still hits no-XFS; XFS-only still mounts the empty whole disk.

## Decision

**1. Make the platform-owned `root=` cmdline provider-aware, expressed as runtime data.** Add a
`platform_root_cmdline: str | None` field to `ProviderRuntime` (`providers/core/runtime.py`), defaulting
to `"root=/dev/vda"`. The local-libvirt and fault-inject runtimes inherit the default; the remote-libvirt
runtime sets `platform_root_cmdline=None`, meaning *the in-guest bootloader owns the root device — the
platform must not inject one*. `system_required_cmdline` and `cmdline_for` take the resolved
`root_cmdline` rather than reading the module constant, and both call sites already hold the
`runtime`:

- `jobs/handlers/runs_install.py` composes the install cmdline → remote installs get
  `console=ttyS0 [crashkernel=256M] [debug cmdline]` with **no** `root=`, so `grubby --copy-default`'s
  inherited `root=UUID=…` is the only `root=` and survives.
- `mcp/tools/lifecycle/runs/view.py` advertises the required cmdline on `runs.get` (ADR-0061), so the
  agent-visible required cmdline matches what the provider actually injects.

`console=ttyS0` is still injected everywhere (kdive's serial console capture parity, ADR-0095, depends
on it). `grubby --args` appends rather than de-duping — the #587 console proves it (the failed entry
carried both `root=UUID=…` and `root=/dev/vda`) — so the injected `console=ttyS0` lands alongside the
base default's `console=tty0 console=ttyS0,115200`; multiple `console=` is harmless because the kernel
accepts several console directives (last is primary). `crashkernel=256M` remains gated on
`CaptureMethod.KDUMP`. The `_PLATFORM_OWNED_CMDLINE_TOKENS`
admission set (`root=`/`console=`/`crashkernel=`, which rejects a user build cmdline that sets them) is
**unchanged** — a user must never set `root=` on any provider; only what the *platform* injects becomes
provider-aware.

**2. Add `CONFIG_XFS_FS=y` and `CONFIG_XFS_POSIX_ACL=y` to the `kdump` build-config fragment.** The repo
tracks the packaged seed `src/kdive/build_configs/data/kdump.config` (the default a deployment inherits
when it declares no fragment) and the `systems.toml.example` template. An operator's deployed
`systems.toml` is gitignored and file-authoritative (`source='config'`, ADR-0122) — declaring its own
`kdump` fragment overrides the seed, so a remote deployment must carry the XFS lines there too (the
example documents this and the D2 cluster's `systems.toml` is updated at deploy). `=y` (not `=m`): the in-guest helper already regenerates the
initramfs (`dracut --force`) on every install, but a built-in driver is guaranteed present regardless of
dracut's host-config module-selection heuristics, so `=y` is the safer guarantee the XFS root mounts. This
is the fragment the #587 remote arc applies; it is where the remote root-fs driver requirement is
satisfied today.

## Consequences

- A remote-libvirt boot installs a kernel whose single `root=` is the base image's `root=UUID=…` and
  whose kernel can mount the XFS root, so the System reaches multi-user, the guest agent starts, and
  `runs.boot` readiness succeeds instead of `boot_timeout`.
- Local-libvirt and fault-inject behavior is byte-identical (they keep `root=/dev/vda`); the change is
  inert for every non-remote provider.
- `runs.get`'s advertised required cmdline now omits `root=` for remote Systems, matching the installed
  cmdline. A new field on `ProviderRuntime` with a backward-compatible default; no schema, migration, or
  tool-surface change.
- The packaged `kdump` seed now pulls in XFS (its bytes and sha256 change). An operator who declares a
  `kdump` fragment in their `systems.toml` (e.g. D2, which keeps its `CONFIG_GDB_SCRIPTS=y` marker)
  overrides the seed and must add the XFS lines there; the `systems.toml.example` template now shows them.
- The remote arc must be re-verified live on D2 after merge (build → install → boot → multi-user) — the
  console capture from #594 is the verification instrument.

## Considered & rejected

- **Add XFS via a new always-applied "remote-rootfs" build-config fragment** instead of the kdump
  fragment. Rejected for this fix as scope creep: the platform applies exactly one named config per
  build today; an "always-apply-for-remote" composition path plus a new catalog/seed/reconcile entry is
  a feature, not the bugfix #587 needs. The kdump fragment is what the remote arc uses; revisit a
  dedicated rootfs-driver fragment if non-kdump remote configs become common.
- **Strip `root=` inside the in-guest helper** (`kdive-install-kernel` ignores any `root=` in `--args`).
  Rejected — pushes boot-medium policy into a shell helper, is surprising, and leaves the worker
  composing a cmdline it knows is wrong; the provider-aware compose keeps the helper a dumb executor.
- **Keep one global `root=/dev/vda` and make the remote base image whole-disk ext4.** Rejected — the base
  image is an external, partitioned XFS cloud image; reshaping it to match a local convention is out of
  band and loses the partitioned/UUID boot the image ships with.
- **Resolve `root=` from the System's provisioning profile rather than the provider runtime.** Rejected —
  the root-device convention is fixed per provider (direct-kernel-boot vs in-guest GRUB), not per
  profile; runtime data is the simplest correct seam and both call sites already hold the runtime.
