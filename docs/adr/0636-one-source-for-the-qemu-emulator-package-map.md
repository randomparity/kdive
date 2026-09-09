# 0636 — One source for the QEMU emulator package map

## Status

Accepted (2026-09-09)

- **Issue:** #2390 (sub-issue of #2388)

## Context

Two places named the QEMU system-emulator package and disagreed.
`libvirt_stack_qemu_package_map` gave the RedHat family `qemu-kvm`; `package_for` in
`scripts/check-setup-deps.sh` gave `qemu-system-x86` for the `fedora` distro id.
Nothing compared them, so the disagreement was invisible.

The evidence does not favour the shell side. On EL9 and EL10 no package provides
`/usr/bin/qemu-system-x86_64` — the binary ships as `/usr/libexec/qemu-kvm` — and
`qemu-system-x86` is not in AppStream. On Fedora, `qemu-kvm` is a metapackage whose
x86_64 requirement is `qemu-system-x86`. So the role was right for the whole family,
and the dependency checker was wrong twice: in the package it named, and in the `PATH`
probe it used to decide that package was missing.

Only the report breaks. KDIVE resolves the emulator from live libvirt capabilities
(ADR-0340/ADR-0345) and emits `<emulator>` only for TCG domains, so a RedHat host
provisions and boots with the binary off `PATH`; `just check-deps` is what reports it
permanently missing and names a package that does not exist there.

## Decision

### 1. One file, read directly by both consumers

`deploy/ansible/roles/libvirt_stack/vars/qemu_packages.yml` holds one entry per
`<os_family>_<arch>` pair over the families either consumer knows — Debian, RedHat,
Suse, Archlinux — as **flat scalars only**:

```yaml
libvirt_stack_qemu_package_RedHat_x86_64: qemu-kvm
```

Ansible loads it with `include_vars`; `check-setup-deps.sh` reads it with a bounded
line reader. There is no generated copy and no drift to guard, because there is one
file. The flat shape is the load-bearing constraint: it keeps the shell reader a dozen
lines of `while read` instead of a YAML parser, so a script that runs before `uv sync`
gains no interpreter dependency.

Carrying `Suse` and `Archlinux` rows is not support for those families. It preserves
coverage `scripts/check-setup-deps.sh` already had; no role task reads them. The zypper
install task, package list, daemon model, and unsupported-family assert are #2392's.

### 2. The RedHat family installs `qemu-kvm` on both architectures

Unchanged in the role, corrected in `package_for`.

### 3. The emulator probe accepts a known off-`PATH` location

`check-setup-deps.sh` treats the native emulator as present when the arch-named binary
is on `PATH` **or** `/usr/libexec/qemu-kvm` is executable. Native only: EL ships no
foreign-architecture emulator in AppStream, so the cross-architecture advisory keeps
reporting one absent there, which is true.

## Consequences

- `just check-deps` stops reporting a missing emulator, and stops naming a nonexistent
  package, on RHEL, CentOS Stream, Rocky, and AlmaLinux.
- The canonical file must stay flat. A nested or quoted value is valid YAML that the
  shell reader misreads, and nothing in this change fails when that happens; #2396 adds
  the guard.
- The file lives inside a role because Ansible ships `vars/` to the target and resolves
  no repo-relative path there. A shell script therefore reads a role's file.
- The map stays keyed on host architecture. This record governs which package names the
  emulator, not which architectures a host is provisioned for, so the foreign-architecture
  gap and the arch-to-binary map in
  `src/kdive/diagnostics/contributions/guest_arch_accel.py` are both outside it.

## Considered & rejected

- **Leave both tables and add a comparison guard.** judgment: satisfies the divergence
  check while keeping the duplication that caused it; the next edit still has two places
  to remember.
- **Generate a bash fragment from a nested YAML map.** verified: measured at roughly 90
  lines of generator plus drift tests against 12 lines of reader for the flat file, and
  it reintroduces a second committed copy — the thing this record removes — whose
  staleness then needs its own CI gate.
- **A repo-level data file outside the role.** verified: Ansible ships a role's `vars/`
  and `defaults/` to the target and resolves neither `vars_files` nor a repo-relative
  `include_vars` path there; `deploy/ansible/playbooks/runner.yml` applies this role to
  remote `live_vm_runners` hosts, so a repo-root path would not exist where the task runs.
- **Unify on `qemu-system-<arch>` for every family.** verified: rpmfind's provider listing
  for `/usr/bin/qemu-system-x86_64` returns Fedora, openSUSE, Mageia and OpenMandriva
  packages and no EL9 or EL10 package; the same absence is reported against Incus
  (lxc/incus#1301) and Packer (hashicorp/packer#10892). The name does not exist on the
  family the role actually provisions.
- **Split the collapsed `fedora` distro id into `fedora` and `rhel`.** verified:
  `qemu-kvm` is correct on both — on Fedora 44 `dnf repoquery --requires qemu-kvm` returns
  `qemu-system-x86 = 2:10.2.2-1.fc44`, and on EL it is the supported package name — so the
  split would add a branch whose arms answer identically. The real Fedora/EL difference is
  the binary's location, which decision 3 handles without it.
- **Symlink `/usr/libexec/qemu-kvm` onto `PATH`.** judgment: a host mutation to make a
  read-only report come out right.
