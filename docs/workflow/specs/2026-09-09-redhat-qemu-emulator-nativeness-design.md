# Name the RedHat QEMU emulator package by nativeness — design

Issue #2390. Decision: [ADR-0641](../../adr/0641-redhat-qemu-emulator-package-by-nativeness.md).

## Problem

`scripts/check-setup-deps.sh` names `qemu-system-x86` as the x86_64 emulator package for the whole
RedHat family. EL ships no package providing `/usr/bin/qemu-system-x86_64` — the binary is
`/usr/libexec/qemu-kvm`, off `PATH` — so on EL it names a package absent from AppStream, reports
the emulator missing, and under `-y` tries to install it. `check-local-libvirt.sh` and
`kdivectl doctor` reach the same wrong conclusion, and the doctor gates.

## Scope

Per ADR-0641: `package_for`'s RedHat emulator rows answer `qemu-kvm` when the requested
`qemu-system-<arch>` binary is the host's own, and the arch-named package otherwise; no distro id
is split. A resolver returns the emulator's path — arch-named binary on `PATH`, else
`/usr/libexec/qemu-kvm` — and each of the six probe sites in ADR-0641 decision 2 uses it,
including the one that execs it. Probes report the resolved path, not the name sought.
No empty-package guard is added: `package_for`'s catch-all at `:188` already prevents one.
`docs/operating/platform-support.md:35-52` is updated; the `libvirt_stack` role is untouched.

### Failure model

**Actors** — a contributor running `just check-deps` (with or without `-y`),
`just check-local-libvirt`, or `kdivectl doctor`, on any supported distro and either arch.

**Invariants at stake** — the package set `-y` installs, privileged and host-mutating; every
non-emulator package name; the three diagnostics' verdicts, which must agree.

**Accepted failure classes**
- On EL the foreign-arch advisory still names `qemu-system-ppc`, which EL lacks. Report-only, and
  EL has no foreign emulator to name instead.
- Oracle and Amazon Linux resolve as `fedora`; harmless — `qemu-kvm` is the answer either way.
- A stub at `/usr/libexec/qemu-kvm` reads as present; `virt-host-validate` stays the gate.
- The libexec path is named once per tool, in three languages, with no shared constant.

**Covered elsewhere** — foreign-arch TCG and `preflight-env.sh:98` #2402; SUSE #2392; live #2393.

## Success

1. On a RedHat-family host with no emulator present, the report names `qemu-kvm` for the host's
   own architecture, while the foreign-arch line still names the arch-named package.
2. Every non-emulator package name is unchanged, on every distro id.
3. On a host with `/usr/libexec/qemu-kvm` and no arch-named binary on `PATH`, all three
   diagnostics report the native emulator present and the doctor check does not fail.
4. `docs/operating/platform-support.md:35-52` records what EL can and cannot run.

## Validation

- Success 1 and 2 — **Mode: focused-test**. Extend `tests/scripts/test_check_setup_deps.py`, which
  drives the script as a subprocess under stubbed `PATH`, `KDIVE_OS_RELEASE` and `uname`: the
  emulator package named on each host arch (red before: `qemu-system-x86` either way); the eight
  non-emulator `:fedora` rows unchanged.
- Success 3 — **Mode: focused-test**. Shell sides through that harness with a stub libexec path
  and an emptied `PATH` (red before: a line from each probe); both doctor probes through
  `default_guest_arch_accel_probe` and `default_pseries_fadump_probe`'s injected `which` and
  `is_executable`, already host-free.
- Success 4 — **Mode: task-test-not-applicable**. Prose with no executable consumer.
