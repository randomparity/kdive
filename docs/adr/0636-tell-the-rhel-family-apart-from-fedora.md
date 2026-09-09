# 0636 — Tell the RHEL family apart from Fedora in the dependency checker

## Status

Accepted (2026-09-09)

- **Issue:** #2390 (sub-issue of #2388)

## Context

`scripts/check-setup-deps.sh` collapses `fedora`, `rhel` and `centos` into one distro id and
names `qemu-system-x86` as the x86_64 emulator package for all of them. On Fedora that is
right. On RHEL, CentOS Stream, Rocky and AlmaLinux no package provides
`/usr/bin/qemu-system-x86_64` — the binary ships as `/usr/libexec/qemu-kvm`, off `PATH` — so
the checker names a package absent from AppStream, probes a path EL never populates, reports
the emulator permanently missing, and under `-y` tries to install the nonexistent name.

#2390 proposed the cause is duplication: the Ansible role's `libvirt_stack_qemu_package_map`
names these packages too and disagrees, so one canonical file should serve both. The two
tables answer different questions. `package_for` is keyed on the requested binary — which
package provides `qemu-system-<arch>` here — and all six of its call sites ask that.
The role's map is keyed on the host's own architecture — which package to install so this
host can run guests. They agree on Debian, openSUSE and Arch, and diverge on the RHEL family
for one reason: EL has no package providing the arch-named binary. Collapsing them onto one
row per (family, arch) makes the foreign-architecture advisory on Fedora say `qemu-kvm`,
which is already installed and contains no PPC emulator.

Only the report breaks. KDIVE resolves the emulator from live libvirt capabilities
(ADR-0340/ADR-0345) and emits `<emulator>` only for TCG domains, so an EL host provisions and
boots with the binary off `PATH`; `just check-deps` is what reports it missing.

## Decision

### 1. `load_distro_id` gains a `rhel` id, matched ahead of `fedora`

Every EL variant carries `fedora` in its match string, so the `rhel` arm is correct only
ahead of the `fedora` arm. That ordering is the whole correctness of the split, and a test
pins it with an `ID_LIKE` fixture — the existing suite writes only bare `ID=`.

Every existing `:fedora` row in `package_for` also matches `:rhel`. Those eight rows keep
exactly the answers EL gets today, so the split changes no package name outside the emulator
rows. `load_distro_id` reporting EL as Fedora is what made this defect possible; leaving it
in place would seed the next one.

### 2. The RHEL family's x86_64 emulator package is `qemu-kvm`, and it has no ppc64le one

`package_for qemu-system-x86_64 rhel` answers `qemu-kvm`. `package_for qemu-system-ppc64
rhel` answers nothing, because nothing provides it. The cross-arch advisory already has the
idiom for an unanswerable arch — `check-setup-deps.sh:301` skips rather than advise `""` —
and gains a branch that says the emulator is unavailable instead of naming a package.

An empty answer must never reach an install list. It cannot here: only the advisory asks the
foreign question, and the future tier asks only for the host's native binary.

### 3. Both native-emulator probes accept `/usr/libexec/qemu-kvm`

The script probes the native emulator twice — `check-setup-deps.sh:284-292` for the advisory
line, and `:404-409`, which appends the native binary to `future_cmds` and checks it through
`require_command`. Patching only the first leaves `just check-deps` still listing the
emulator missing on EL. Both route through one predicate. Native only: EL ships no
foreign-architecture emulator, so the cross-arch line keeps reporting one absent there, which
is true.

## Consequences

- `just check-deps` stops reporting a missing emulator and stops naming a nonexistent package
  on RHEL, CentOS Stream, Rocky and AlmaLinux, and stops offering to install one under `-y`.
- The distro id set grows from four to five. A new `package_for` row must now decide whether
  it applies to Fedora, to EL, or to both; the eight widened rows are the worked example.
- `/usr/libexec/qemu-kvm` present without a working emulator reads as present.
  `check-local-libvirt.sh` stays the authoritative gate, as `:281-283` already says of
  `/dev/kvm`.
- The role's map and the checker's table stay separate, so the same package name appears in
  both for Debian. That is two answers to two questions that coincide, not a copy.

## Considered & rejected

- **One canonical file both consumers read** — the issue's own proposal. verified: the two
  tables answer different questions, and unifying them regresses the Fedora cross-arch
  advisory from `qemu-system-ppc` to `qemu-kvm`. On Fedora 44, `dnf repoquery --requires
  qemu-kvm` returns only `qemu-system-x86`, while `/usr/bin/qemu-system-ppc64` comes from
  `qemu-system-ppc-core`. `docs/operating/platform-support.md:34` names its table as the
  foreign-arch one, so the same change would strand its worked example at `:45`.
- **Do nothing.** judgment: the checker names a package EL does not have and offers to
  install it; that is the reported defect.
- **Detect EL only inside the emulator rows, leaving the id collapsed.** judgment: three
  lines smaller, and it leaves `load_distro_id` asserting EL is Fedora for every future row.
- **Unify on `qemu-system-<arch>` for every family.** verified: rpmfind's provider listing
  for `/usr/bin/qemu-system-x86_64` returns Fedora, openSUSE, Mageia and OpenMandriva
  packages and no EL9 or EL10 package; the same absence is reported against Incus
  (lxc/incus#1301) and Packer (hashicorp/packer#10892).
- **Symlink `/usr/libexec/qemu-kvm` onto `PATH`.** judgment: a host mutation to make a
  read-only report come out right.
