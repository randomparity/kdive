# Name the RedHat QEMU emulator package by nativeness — design

Issue #2390. Decision: [ADR-0636](../../adr/0636-redhat-qemu-emulator-package-by-nativeness.md).

## Problem

`scripts/check-setup-deps.sh` names `qemu-system-x86` as the x86_64 emulator package for the whole
RedHat family. EL ships no package providing `/usr/bin/qemu-system-x86_64` — the binary is
`/usr/libexec/qemu-kvm`, off `PATH` — so on RHEL, CentOS Stream, Rocky and AlmaLinux it names a
package absent from AppStream, reports the emulator permanently missing, and under `-y` tries to
install that name. `check-local-libvirt.sh`, which it refers operators to, fails the same host.

## Scope

Per ADR-0636: `package_for`'s two RedHat emulator rows answer `qemu-kvm` when the requested
`qemu-system-<arch>` binary is the host's own, and keep the arch-named package for a foreign
request; no distro id is split and `load_distro_id` is unchanged. All three native-emulator
probes accept `/usr/libexec/qemu-kvm` — `check-setup-deps.sh:284-292`, `:404-409`, and
`check-local-libvirt.sh:173-178`. `note_package:154-164` ignores an empty package name, and
`docs/operating/platform-support.md:35-45` records what EL can and cannot run. The
`libvirt_stack` role is untouched. Exclusions are the charter's.

### Failure model

**Actors** — a contributor running `just check-deps` (with or without `-y`) or
`just check-local-libvirt` on any of the nine supported distros, on x86_64 or ppc64le.

**Invariants at stake** — the package set `-y` installs, which is privileged and host-mutating;
every non-emulator package name; both diagnostics' reports, which must agree.

**Accepted failure classes**
- On EL the foreign-arch advisory still names `qemu-system-ppc`, which EL lacks. Report-only, and
  EL has no foreign emulator to name instead.
- Oracle and Amazon Linux resolve as `fedora`; harmless, since `qemu-kvm` is the RedHat-family
  answer either way.
- A stub at `/usr/libexec/qemu-kvm` reads as present; `virt-host-validate` stays the gate.

**Covered elsewhere** — foreign-arch TCG and `preflight-env.sh:98` #2402; SUSE #2392; live #2393.

## Success

1. `package_for qemu-system-x86_64 fedora` answers `qemu-kvm` on an x86_64 host and
   `qemu-system-x86` on a ppc64le host; `qemu-system-ppc64` answers the mirror.
2. Every non-emulator `package_for` answer is unchanged, on every distro id.
3. A run on an EL host with `/usr/libexec/qemu-kvm` and no arch-named binary on `PATH` reports no
   missing emulator, from `check-setup-deps.sh` and `check-local-libvirt.sh` alike.
4. An empty package name never reaches a per-tier install array.
5. `docs/operating/platform-support.md:35-45` records what EL can and cannot run.

## Validation

- Success 1–4 — **Mode: focused-test**. Extend `tests/scripts/test_check_setup_deps.py`, which
  drives the script as a subprocess under stubbed `PATH`, `KDIVE_OS_RELEASE` and `uname`. Cases:
  the emulator package named for `fedora` on each host arch (red before: `qemu-system-x86`
  either way); a stub `/usr/libexec/qemu-kvm` with an emptied `PATH` gives a full run naming no
  emulator (red before: a line from each probe); a row returning nothing contributes no array
  element (red before: an empty element reaches the tier); the eight non-emulator `:fedora` rows
  answer unchanged. `check-local-libvirt.sh` needs the same `/usr/libexec` override, one case.
- Success 5 — **Mode: task-test-not-applicable**. Hand-written Markdown prose with no executable
  consumer; a test searching it asserts nothing about behaviour.
