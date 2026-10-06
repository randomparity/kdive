# 0723 — drgn-live reads the Run's staged DWARF vmlinux, not kernel BTF

## Status

Proposed

Supersedes [ADR-0322](0322-drgn-live-missing-debuginfo-warning.md). Amends
[ADR-0328](0328-live-drgn-capability-signal.md) (the BTF floor),
[ADR-0329](0329-drgn-live-runtime-debuginfo-probe.md) and
[ADR-0335](0335-attach-runtime-debuginfo-probe.md) (when the probe runs). Extends
[ADR-0221](0221-local-live-drgn-debuginfo-staging.md) to every install path.

## Context

ADR-0322 and ADR-0328 assume that in-guest drgn reads the running kernel's BTF
(`/sys/kernel/btf/vmlinux`). No released drgn does: upstream BTF type and object finders are
unmerged ([osandov/drgn#176](https://github.com/osandov/drgn/issues/176)), and the 0.0.31
release that sets `BTF_CAPABLE_DRGN` added ORC unwinding and a module API, not a BTF reader. On a
native POWER9 guest (kdive `8a7ce8fe2`, kernel 7.2.8 with BTF and DWARF5 built in, drgn
`0.2.0-1.fc44`, no `vmlinux` uploaded), `test_spine_live_script_over_the_wire` failed with
`could not find 'init_uts_ns'` (#2734, #3121).

A matching DWARF `vmlinux` does work. ADR-0221 already stages the Run's uploaded `vmlinux` at
`/usr/lib/debug/lib/modules/<release>/vmlinux`, a path drgn searches by default, but only on the
local-libvirt legacy install path.

## Decision

We will treat the Run's uploaded DWARF `vmlinux` (`debuginfo_ref`), staged in the guest at
`/usr/lib/debug/lib/modules/<release>/vmlinux`, as the only supported symbol source for drgn-live
on an uploaded kernel.

1. `live_drgn` keeps its values and keys. It drops the BTF floor (`min_drgn_required` is null),
   and its note states that drgn-live needs the Run's `vmlinux` upload.
2. The static `missing_debuginfo` warning keys on "an external build with no uploaded
   `vmlinux`" and names `vmlinux` as missing. `DEBUG_INFO_BTF` plays no part in it.
3. The runtime resolution probe runs whenever the static check is silent, including when a
   `vmlinux` was uploaded, because an install path may not stage it.
4. `kdive-drgn` stops passing `-s /sys/kernel/btf/vmlinux`, and `KDIVE_BTF_PATH` is removed.
5. Every install path stages the uploaded `vmlinux`: the external-boot authority path (#3123)
   and remote-libvirt (#3124), with a guest free-space check (#3125).

## Consequences

- Agents get a truthful signal before provisioning and a truthful warning at attach, on every
  path, before #3123 and #3124 land.
- Every drgn version with tooling reports `capable`, because any released drgn reads DWARF. A
  drgn too old for a new kernel's structures is caught by the always-on probe, not by the signal.
- An attach with an uploaded `vmlinux` costs one more probe round trip (at most 10 s).
- drgn-live now needs a `vmlinux` upload of hundreds of MB, and guest disks must hold it (#3125).
- Images built before this change keep the old `-s` flag. drgn still loads its default debug
  info after a `-s` file and ignores a file that matches no module (`drgn/cli.py` `--symbols`
  help and `default_symbols` handling, upstream `main`, 2026-10-06), so those images work once the
  `vmlinux` is staged. The implementation proves this on a real guest.

## Considered & rejected

- **Keep BTF and wait for upstream.** verified: osandov/drgn#176 is open with BTF finders only
  on an unmerged branch (read 2026-10-06), so drgn-live stays broken for 0.5.0.
- **Analyze a memory snapshot on the host.** judgment: a per-call memory dump is slow, pauses
  the guest, and changes the live semantics that ADR-0240 gives agents.
- **Ship drgn built from the BTF branch in guest images.** judgment: unreleased upstream code
  in every image, maintained by kdive.
- **Add a `requires_debuginfo` value to `live_drgn`.** judgment: a published enum change that
  agents must absorb, for a dependency the note and the warning already state.
