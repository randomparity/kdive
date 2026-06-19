# Remote boot: provider-aware `root=` cmdline + XFS root support (#587)

Authoritative decision: [ADR-0183](../adr/0183-provider-aware-platform-root-cmdline.md). This spec is
the falsifiable design + acceptance criteria the implementation must meet.

## Problem

A remote-libvirt System builds and installs a kernel, then `runs.boot` fails `boot_timeout`: the guest
reboots into systemd **emergency mode** because the freshly-installed kernel cannot mount the root
filesystem. Root-caused from the #594 boot console (135,664 bytes) to two independent, jointly-required
causes:

1. The platform appends `root=/dev/vda` after the base image's correct `root=UUID=…`; the kernel honors
   the last `root=` and mounts the whole disk `/dev/vda` (a GPT table, no filesystem).
2. The built kernel (`x86_64_defconfig` + `kdump` fragment) has no XFS support, but the base image root
   (`vda3`) is XFS V5.

See ADR-0183 §Context for the console evidence.

## Design

### Part 1 — provider-aware platform `root=`

`root=/dev/vda` is local-libvirt's direct-kernel-boot whole-disk-ext4 convention, not a platform global.
Express the platform-owned root device as runtime data:

- `ProviderRuntime` (`src/kdive/providers/core/runtime.py`) gains
  `platform_root_cmdline: str | None = "root=/dev/vda"`.
- local-libvirt + fault-inject runtimes inherit the default; remote-libvirt sets
  `platform_root_cmdline=None`.
- `services/runs/steps.py`:
  - `system_required_cmdline(method, root_cmdline)` builds `console=ttyS0` + (the root arg when not
    `None`) + (`crashkernel=256M` when `method is KDUMP`).
  - `cmdline_for(conn, run, method, *, root_cmdline)` prepends that required cmdline to the build's debug
    cmdline as today.
- Both call sites already resolve the provider `runtime` and pass `runtime.platform_root_cmdline`:
  - `jobs/handlers/runs_install.py` (composes the install cmdline).
  - `mcp/tools/lifecycle/runs/view.py` (`runs.get` advertised required cmdline, ADR-0061).

Token order for the required cmdline is fixed and deterministic: `console=ttyS0` first, then the optional
`root=…`, then optional `crashkernel=256M`. Unchanged: `console=ttyS0` is always injected (serial console
capture parity), and `_PLATFORM_OWNED_CMDLINE_TOKENS` (`root=`/`console=`/`crashkernel=`) still rejects a
user build cmdline that sets any of them, on every provider.

`grubby --args` appends rather than de-duping (the #587 console proves this — the failed entry carried
both `root=UUID=…` and `root=/dev/vda`). So injecting `console=ttyS0` on remote appends to the base
default's `console=tty0 console=ttyS0,115200`, leaving multiple `console=` tokens — harmless, since the
kernel accepts several `console=` directives (the last is the primary). Removing the platform `root=` for
remote is what matters: the only remaining `root=` is the base default's correct `root=UUID=…`.

### Part 2 — XFS in the kdump fragment

Add to both copies of the `kdump` build-config fragment:

```
CONFIG_XFS_FS=y
CONFIG_XFS_POSIX_ACL=y
```

- `src/kdive/build_configs/data/kdump.config` (the tracked packaged seed — the default a deployment
  inherits when it declares no `kdump` fragment).
- `systems.toml.example` (tracked template) — documents that a remote deployment declaring its own
  `kdump` fragment must carry the XFS lines, since a declared fragment (`source='config'`, ADR-0122)
  overrides the seed. The operator's deployed `systems.toml` is gitignored; D2's is updated at deploy.

`=y` (built-in), not `=m`: the install helper already regenerates the initramfs (`dracut --force`), but a
built-in driver is guaranteed present regardless of dracut's host-config module-selection heuristics.

## Acceptance criteria

1. `system_required_cmdline(KDUMP, "root=/dev/vda")` == `"console=ttyS0 root=/dev/vda crashkernel=256M"`.
2. `system_required_cmdline(KDUMP, None)` == `"console=ttyS0 crashkernel=256M"` (no `root=`).
3. `system_required_cmdline(CONSOLE, None)` == `"console=ttyS0"` (no `root=`, no `crashkernel=`).
4. `cmdline_for` with a build debug cmdline `"foo=bar"` and `root_cmdline=None`, method CONSOLE →
   `"console=ttyS0 foo=bar"`.
5. The remote-libvirt runtime exposes `platform_root_cmdline is None`; the local-libvirt and fault-inject
   runtimes expose `platform_root_cmdline == "root=/dev/vda"`.
6. The install handler composes a remote install cmdline containing **no** `root=` token, and a local
   install cmdline containing exactly `root=/dev/vda`.
7. `runs.get` advertises a required cmdline with no `root=` for a remote System and with `root=/dev/vda`
   for a local System.
8. The packaged seed `kdump.config` contains `CONFIG_XFS_FS=y` and `CONFIG_XFS_POSIX_ACL=y`; the
   `systems.toml.example` kdump block documents them.
9. `_PLATFORM_OWNED_CMDLINE_TOKENS` still rejects a user cmdline containing `root=` (unchanged) on any
   provider.

## Out of scope

- A dedicated always-applied remote-rootfs build-config fragment (revisit if non-kdump remote configs
  become common).
- Changing how the install plane regenerates the guest initramfs. The helper already runs
  `dracut --force` on every install; `=y` is chosen because a built-in driver is guaranteed present
  regardless of dracut's host-config module-selection heuristics, not because no initramfs is generated.
- A new `configuration_error` that distinguishes emergency-mode from a slow boot (a separate observability
  improvement the issue lists as optional).

## Verification

Unit + handler tests for criteria 1–9. Live re-verification on D2 after merge: build → install → boot a
remote System to multi-user (guest agent connects, `runs.boot` succeeds), using the #594 console capture
as the instrument.
