# 0637 — Name the RedHat QEMU emulator package by nativeness

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

The missing-emulator conclusion is not confined to one report. Three tools reach it
independently, and one of them gates: `kdivectl doctor` exits nonzero when any check fails
(`src/kdive/cli/commands/doctor.py:101-102`). Fixing the shell diagnostics alone would leave the
doctor as the only tool failing a healthy EL host — worse than today, where all three agree the
host is broken.

## Decision

### 1. The RedHat emulator rows answer by nativeness, not by architecture

`package_for` answers `qemu-kvm` when the requested `qemu-system-<arch>` binary is the host's
own emulator, and keeps the arch-named package for a foreign request. No distro id is split;
`load_distro_id`'s collapsed `fedora` arm is unchanged.

Measured on Fedora 44 x86_64: `dnf repoquery --requires qemu-kvm` returns `qemu-system-x86`. The
same query run natively on a Fedora 44 **ppc64le** host returns `qemu-system-ppc`. One name, the
host's own emulator, either architecture. On Rocky Linux 10.2 x86_64 (EL10),
`dnf provides /usr/libexec/qemu-kvm` answers `qemu-kvm-core` from `appstream`, and
`dnf list --available 'qemu-system-*'` returns `No matching Packages to list`.

Whether `qemu-kvm` is likewise available on **EL ppc64le** is not settled here: the default repos
of the Rocky 9, CentOS Stream 9 and CentOS Stream 10 ppc64le images carry no `qemu-kvm` at all.
Tracked in #2405. It does not affect this decision's other three arms, each measured above.

This is what the `libvirt_stack` role has always installed
(`deploy/ansible/roles/libvirt_stack/defaults/main.yml:28-34`), reached from the other side.
The role and the checker still hold separate tables, because they answer different questions.
They are not a copy of one another and are not merged here.

### 2. Every native-emulator probe resolves the emulator, not just `PATH`

There are six emulator probes across four tools; two of them execute the binary they find. A fix
to fewer than all of them leaves the project's diagnostics contradicting each other on a working
EL host:

| site | mechanism |
|---|---|
| `check-setup-deps.sh:284-292` | `command_exists`, advisory line |
| `check-setup-deps.sh:404-409` | `require_command`, future tier |
| `check-local-libvirt.sh:173-178` | `_cmd` (`command -v`), `note_fail` |
| `guest_arch_accel.py:129-139` | `shutil.which`, feeds the gating FAIL at `provider_checks.py:495-502` |
| `check-local-libvirt.sh:196-207` | `_cmd`, then **execs** `qemu-system-ppc64 --version` |
| `pseries_fadump.py:51` | `shutil.which`, then **execs** `--version` via `detect_pseries_fadump` |

The executing sites are why this is a resolution change rather than a presence relaxation: they
need a path they can run, not a boolean. A resolver returns the emulator's path — the arch-named
binary on `PATH`, else `/usr/libexec/qemu-kvm` — and each site uses it.

The fadump probe is the sharpest case: PATH-only it returns `not_applicable` on an EL ppc64le
host, the exact host fadump exists for, while `LocalLibvirtDiscovery` reads libvirt's capabilities
XML and sees the arch — so its own promise that "doctor and discovery cannot diverge" fails there.

Native only: EL ships no foreign-architecture emulator, so the cross-architecture advisory keeps
reporting one absent there, which is true.

### 3. A probe reports the path it found, not the name it looked for

`check-setup-deps.sh:287`, `:289` and `check-local-libvirt.sh:175` name the arch-named binary in
their success messages. On EL that binary does not exist, so a message asserting it is present
would be false. Each reports the resolved path instead.

### 4. No empty-package guard is added, because the catch-all already prevents one

An earlier revision of this record added a guard in `note_package` against an empty package name
reaching the privileged `-y` install array, where `dnf install -y "" bc` fails the whole
transaction. That guard is not added, because no reachable call can produce an empty answer:
`package_for`'s final arm (`:188`) is `*) printf "%s" "${name}"`, so a non-empty name always
yields a non-empty package, and both callers that could pass an empty binary name — the advisory
at `:285` and the future tier at `:404` — are already behind `arch_is_supported`.

The guard was required by the previous design, which introduced a row that deliberately answered
nothing. This design has no such row. `:188` is therefore load-bearing: it is what keeps an empty
element out of the install array, and a future row that prints nothing would defeat it.

## Consequences

- `just check-deps`, `just check-local-libvirt` and `kdivectl doctor` agree on a working EL host,
  and none names a package that does not exist there nor offers to install one.
- `package_for`'s contract widens: for the RedHat family its answer depends on the host
  architecture as well as the requested binary. Its other rows stay binary-keyed.
- On EL the *foreign*-architecture advisory still names `qemu-system-ppc`, which EL does not
  ship. That line is report-only and never reaches an installer, and EL has no
  foreign-architecture emulator to name instead, so no correct advice exists.
- A distro that is EL-like but declares neither `rhel` nor `centos` — Oracle Linux, Amazon Linux
  — is still treated as Fedora. Decision 1 makes that harmless, because `qemu-kvm` is the answer
  for every RedHat-family distro either way.
- `/usr/libexec/qemu-kvm` is now a second known emulator location in three languages. It is
  named once per tool rather than centrally, because the three do not share a library.

## Considered & rejected

- **Fix the shell diagnostics and leave `kdivectl doctor` to a follow-up.** verified: the doctor
  gates. `DiagnosticsReport.has_failure` (`src/kdive/diagnostics/service.py:160-162`) drives
  `_FAIL_EXIT` at `src/kdive/cli/commands/doctor.py:101-102`, and `GuestArchAccelCheck`
  (`provider_checks.py:495-502`) FAILs on `native_supported and not native_emulator_present and
  target_is_local`. EL would stay gated and this issue's outcome unmet.
- **Split the collapsed `fedora`/`rhel`/`centos` distro id.** verified: no token rule separates
  EL-like from Fedora-like. Oracle Linux 9 is `ID="ol"`, `ID_LIKE="fedora"` and Amazon Linux 2023
  is `ID="amzn"`, `ID_LIKE="fedora"`, carrying no `rhel` or `centos` token, while Fedora
  derivatives declare the same `ID_LIKE`. The split also silently re-answers two further
  consumers keyed on the id — `print_install_hint:207` and the `-y` installer at `:483` — so EL
  would lose auto-install entirely.
- **Unify the role's map and the checker's table onto one canonical file** — this issue's
  original proposal. verified: the two answer different questions, and collapsing them onto one
  row per (family, architecture) regresses the Fedora cross-arch advisory from `qemu-system-ppc`
  to `qemu-kvm`. On Fedora 44 `dnf repoquery --requires qemu-kvm` returns only `qemu-system-x86`,
  and `/usr/bin/qemu-system-ppc64` comes from `qemu-system-ppc-core`.
- **Unify on `qemu-system-<arch>` for every family.** verified: on Rocky Linux 10.2 (x86_64, EL10,
  BaseOS + AppStream + Extras enabled) `dnf list --available 'qemu-system-*'` exits with
  `No matching Packages to list`, while `dnf provides /usr/libexec/qemu-kvm` answers
  `qemu-kvm-core-18:10.1.0-16.el10_2.x86_64` from `appstream` — EL packages the emulator only at
  the libexec path. For EL9, rpmfind's provider listing for `/usr/bin/qemu-system-x86_64` returns
  Fedora, openSUSE, Mageia and OpenMandriva packages and no EL package; the same absence is
  reported against Incus (lxc/incus#1301) and Packer (hashicorp/packer#10892).
- **Symlink `/usr/libexec/qemu-kvm` onto `PATH`.** judgment: a host mutation to make a read-only
  report come out right.
