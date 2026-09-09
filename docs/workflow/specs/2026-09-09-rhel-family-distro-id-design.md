# Tell the RHEL family apart from Fedora — design

Issue #2390. Decision: [ADR-0636](../../adr/0636-tell-the-rhel-family-apart-from-fedora.md).

## Problem

`scripts/check-setup-deps.sh` collapses `fedora`, `rhel` and `centos` into one distro id, then
names `qemu-system-x86` as the x86_64 emulator package for all of them. EL ships no package
providing `/usr/bin/qemu-system-x86_64` — the binary is `/usr/libexec/qemu-kvm`, off `PATH` — so on
RHEL, CentOS Stream, Rocky and AlmaLinux the checker names a package absent from AppStream, reports
the emulator permanently missing, and under `-y` tries to install that name.

## Scope

Per ADR-0636: `load_distro_id` gains a `rhel` id matched ahead of `fedora`; the eight existing
`:fedora` rows in `package_for` also match `:rhel`, answering as they do today;
`qemu-system-x86_64:rhel` answers `qemu-kvm` and `qemu-system-ppc64:rhel` answers nothing, with the
cross-arch advisory reporting unavailability rather than naming a package; both native-emulator
probes accept `/usr/libexec/qemu-kvm`. `docs/operating/platform-support.md:38-45` gains an EL row.
The `libvirt_stack` role is untouched — its map answers a different question and is already right.
Exclusions are the charter's.

### Failure model

**Actors** — a contributor running `just check-deps`, with or without `-y`, on Fedora, RHEL,
CentOS Stream, Rocky, AlmaLinux, Debian, Ubuntu, openSUSE or Arch.

**Invariants at stake** — the package set `-y` installs, which is privileged and host-mutating;
every non-emulator package name, on every distro; the report.

**Accepted failure classes**
- `/usr/libexec/qemu-kvm` present without a working emulator reads as present;
  `check-local-libvirt.sh` stays the gate, as `check-setup-deps.sh:281-283` says for `/dev/kvm`.
- A distro matching none of the five ids keeps the `unknown` branch's existing answers.
- EL ppc64le guests get no package advice, because no package exists to name.

**Covered elsewhere** — SUSE role support #2392; live proof #2393; the role's host-arch keying
(unowned follow-up).

## Success

1. `rhel` is the id resolved for `ID=rhel`, `ID=rocky` and `ID=centos`, including their
   `fedora`-bearing `ID_LIKE`, and `package_for qemu-system-x86_64 rhel` answers `qemu-kvm`.
2. Every other `package_for` answer on EL is unchanged from what it returns today.
3. A run on an EL host with `/usr/libexec/qemu-kvm` and no arch-named binary on `PATH` reports no
   missing emulator and names no emulator package to install.
4. The EL cross-arch line reports ppc64le unavailable without naming a package.
5. `docs/operating/platform-support.md:38-45` distinguishes Fedora from the RHEL family.

## Validation

- Success 1–4 — **Mode: focused-test**. Extend `tests/scripts/test_check_setup_deps.py`, which
  already drives the script as a subprocess under a stubbed `PATH`, `KDIVE_OS_RELEASE` and `uname`.
  Cases: `ID=rocky` with `ID_LIKE="rhel centos fedora"` resolves `rhel` (red before: resolves
  `fedora`); the x86_64 emulator package on `rhel` is `qemu-kvm` (red before: `qemu-system-x86`);
  a stub `/usr/libexec/qemu-kvm` with an emptied `PATH` gives a full run naming no emulator (red
  before: a missing-emulator line from each of the two probes); the EL ppc64le advisory names no
  package; and the eight widened rows answer identically for `fedora` and `rhel`.
- Success 5 — **Mode: task-test-not-applicable**. Four rows of a hand-written Markdown table with
  no executable consumer; a test searching prose asserts nothing about behaviour.
