# 0636 — Name the RedHat QEMU emulator package by whether it is the host's own

## Status

Accepted (2026-09-09)

- **Issue:** #2390 (sub-issue of #2388)

## Context

`scripts/check-setup-deps.sh` names `qemu-system-x86` as the x86_64 emulator package for the
whole RedHat family. On Fedora that is right. On RHEL, CentOS Stream, Rocky and AlmaLinux no
package provides `/usr/bin/qemu-system-x86_64` — the binary ships as `/usr/libexec/qemu-kvm`,
off `PATH` — so the checker names a package absent from AppStream, probes a path EL never
populates, reports the emulator permanently missing, and under `-y` tries to install that name.

`package_for` (`:94`) is keyed on the requested binary alone: *which package provides
`qemu-system-<arch>` on this distro*. That key is well defined on Debian, openSUSE and Arch. It
is not well defined on the RedHat family, because there the answer also depends on whether the
requested binary is the host's own emulator. `qemu-kvm` is a metapackage that pulls exactly the
host architecture's emulator and no other, so it answers the native question on both
architectures and both Fedora and EL, and answers the foreign question nowhere.

## Decision

### 1. The RedHat emulator rows answer by nativeness, not by architecture

`package_for` answers `qemu-kvm` when the requested `qemu-system-<arch>` binary is the host's
own emulator, and keeps the arch-named package for a foreign request. No new distro id is
introduced; `load_distro_id`'s collapsed `fedora` arm is unchanged.

Measured on Fedora 44: `dnf repoquery --requires qemu-kvm` returns `qemu-system-x86`, and
`dnf --forcearch=ppc64le repoquery --requires qemu-kvm` returns `qemu-system-ppc`. One name,
the host's own emulator, either architecture.

This is what the `libvirt_stack` role has always installed
(`deploy/ansible/roles/libvirt_stack/defaults/main.yml:28-34`), reached from the other side.
The role and the checker still hold separate tables, because they answer different questions —
the role asks which package to install for this host, the checker asks which package provides a
named binary. They are not a copy of one another and are not merged here.

### 2. All three native-emulator probes accept `/usr/libexec/qemu-kvm`

The native emulator is probed in three places, and a fix to fewer than all three leaves the
project's diagnostics contradicting each other on a working EL host:

- `check-setup-deps.sh:284-292` — the cross-architecture advisory line;
- `check-setup-deps.sh:404-409` — the future tier, via `require_command`;
- `scripts/operations/check-local-libvirt.sh:173-178` — `_cmd` (`command -v`, `PATH` only),
  which `note_fail`s "not found on PATH" and refers the operator back to `check-setup-deps.sh`.

Native only: EL ships no foreign-architecture emulator, so the cross-architecture line keeps
reporting one absent there, which is true.

### 3. `note_package` ignores an empty package name

`note_package` (`:154-164`) appends whatever it is handed to the per-tier array that
`maybe_install_tier` (`:483-494`) expands into `dnf install -y` / `apt-get install -y`. An empty
element there fails the whole transaction, losing every other package in the tier. Guarding the
shared choke point rather than each caller also closes the case where any future `package_for`
row falls through to nothing.

## Consequences

- `just check-deps` and `just check-local-libvirt` agree on a working EL host, and neither names
  a package that does not exist there nor offers to install one.
- `package_for`'s contract widens: for the RedHat family its answer depends on the host
  architecture as well as the requested binary. Its other rows are untouched and stay
  binary-keyed.
- On EL the *foreign*-architecture advisory still names `qemu-system-ppc`, which EL does not
  ship. That line is report-only and never reaches an installer, and EL has no
  foreign-architecture emulator to name instead, so no correct advice exists. Recorded in
  `docs/operating/platform-support.md` rather than silently left wrong.
- A distro that is EL-like but declares neither `rhel` nor `centos` — Oracle Linux, Amazon Linux
  — is still treated as Fedora. Decision 1 makes that harmless for the native package, because
  `qemu-kvm` is the answer for every RedHat-family distro either way.

## Considered & rejected

- **Split the collapsed `fedora`/`rhel`/`centos` distro id.** verified: no token rule separates
  EL-like from Fedora-like. Oracle Linux 9 is `ID="ol"`, `ID_LIKE="fedora"` and Amazon Linux
  2023 is `ID="amzn"`, `ID_LIKE="fedora"` (read from `docker.io/library/oraclelinux:9` and
  `public.ecr.aws/amazonlinux/amazonlinux:2023`), carrying no `rhel` or `centos` token, while
  Fedora derivatives declare the same `ID_LIKE`. The split also silently re-answers two further
  consumers keyed on the id — `print_install_hint:207` and the `-y` installer at `:483` — so EL
  would lose auto-install entirely.
- **Unify the role's map and the checker's table onto one canonical file** — this issue's
  original proposal. verified: the two answer different questions, and collapsing them onto one
  row per (family, architecture) regresses the Fedora cross-arch advisory from `qemu-system-ppc`
  to `qemu-kvm`. On Fedora 44 `dnf repoquery --requires qemu-kvm` returns only `qemu-system-x86`,
  and `/usr/bin/qemu-system-ppc64` comes from `qemu-system-ppc-core`.
- **Fix only the probes and leave `package_for` alone.** judgment: an EL host that genuinely
  lacks QEMU is still told to install a package that does not exist there, which is half the
  reported defect.
- **Unify on `qemu-system-<arch>` for every family.** verified: rpmfind's provider listing for
  `/usr/bin/qemu-system-x86_64` returns Fedora, openSUSE, Mageia and OpenMandriva packages and no
  EL9 or EL10 package; the same absence is reported against Incus (lxc/incus#1301) and Packer
  (hashicorp/packer#10892).
- **Symlink `/usr/libexec/qemu-kvm` onto `PATH`.** judgment: a host mutation to make a read-only
  report come out right.
