# One source for the QEMU emulator package map — design

Issue #2390. Decision: [ADR-0636](../../adr/0636-one-source-for-the-qemu-emulator-package-map.md).

## Problem

`libvirt_stack_qemu_package_map` and `package_for` in `scripts/check-setup-deps.sh`
independently name the QEMU system-emulator package and disagree on the RedHat family. The shell
side is wrong: EL ships no package providing `/usr/bin/qemu-system-x86_64`, so it names a package
absent from AppStream, probes a path EL never populates, and under `-y` installs that package.

## Scope

Per ADR-0636: one flat canonical file, `deploy/ansible/roles/libvirt_stack/vars/qemu_packages.yml`,
holding `libvirt_stack_qemu_package_<Family>_<arch>` scalars for Debian, RedHat, Suse and
Archlinux. The role loads it with `include_vars` and uses the flat keys; the nested map leaves
`defaults/main.yml`. `check-setup-deps.sh` resolves `package_for`'s two emulator rows through a
bounded line reader over it, and its native probe also accepts `/usr/libexec/qemu-kvm`.
`docs/operating/platform-support.md:38-45` is updated to match. Exclusions are the charter's.

### Failure model

**Actors** — a contributor running `just check-deps`, with or without `-y`; the `libvirt_stack` role
applied by `deploy/ansible/playbooks/runner.yml` to a remote `live_vm_runners` host. Designed for
Debian, Ubuntu, Fedora, RHEL, CentOS Stream, Rocky and AlmaLinux; openSUSE and Arch are reported
on, never provisioned.

**Invariants at stake** — the package `check-setup-deps.sh -y` installs, which is privileged
and host-mutating; the package the role installs, identical before and after; the report.

**Accepted failure classes**
- A family neither reader knows gets no advice — the four cover every id `load_distro_id`
  resolves; `unknown` has its own branch.
- `/usr/libexec/qemu-kvm` present without a working emulator reads as present;
  `check-local-libvirt.sh` stays the gate, as `check-setup-deps.sh:281-283` says for `/dev/kvm`.
- Suse and Archlinux names unverified on a live host — none is reachable, and both rows are
  carried unchanged from existing coverage.

**Covered elsewhere** — file shape #2396; SUSE role support #2392; live proof #2393;
foreign-arch provisioning (unowned follow-up).

## Success

1. One file holds all eight (family, architecture) names; neither consumer keeps a copy.
2. `package_for` answers `qemu-kvm` for the RedHat family on both architectures.
3. It reports the native emulator present given `/usr/libexec/qemu-kvm` and no `PATH` binary.
4. `docs/operating/platform-support.md:38-45` matches the canonical file.

## Validation

- Success 1–3 — **Mode: focused-test**. `deploy/ansible/tests/run-qemu-package-map.sh` sources
  `check-setup-deps.sh` under a no-main guard and asserts every family's emulator answer including
  `qemu-kvm` for `fedora` (red before: `qemu-system-x86`), the unknown-id fallback, and that
  `libvirt_stack_qemu_package_RedHat_x86_64` — the key the role's Jinja builds — resolves; then
  drives `native_emulator_exists` with a stub emulator and empty `PATH` (red before: no such
  function). Green: that script printing `ok`, in `just test-ansible`; role side also by
  `--syntax-check` in `just lint-ansible`.
- Success 4 — **Mode: task-test-not-applicable**. Four rows of a hand-written Markdown table with no
  executable consumer; a test searching prose for wording would assert nothing about behaviour, and
  #2396 owns a name-to-doc assertion.
