# 0668 — A kernel upgrade re-applies the /boot relabel, non-fatally

## Status

Proposed

## Context

`local_worker_host` relabels `/boot` kernels to `0640 root:kvm` so libguestfs' supermin appliance
can copy one as the invoking worker account (ADR-0222, #2479). The relabel is a point-in-time
`ansible.builtin.find` plus a `file` loop over its results
(`deploy/ansible/roles/local_worker_host/tasks/boot_kernels.yml:55-72`), so it only ever sees the
kernels present when the role runs. A kernel package upgrade installs a new file under a new name
at the distro default `0600 root:root`, which that run never saw. After an unattended upgrade and
a reboot, the running kernel is the unreadable one and guest-image builds fail with
`infrastructure_failure — libguestfs failed extracting the baseline kernel from the rootfs base`.

The role documents the gap and leaves it to a human — "re-run the recipe afterwards"
(`boot_kernels.yml:64-65`) — with no prompt and no check. Debian and Ubuntu already run every
executable in `/etc/kernel/postinst.d/` after installing a kernel, which is the platform's own
mechanism for exactly this. Two constraints bound the fix: no path may reach `0644`, because
Fedora ships these world-readable and widening a Debian host to match is the regression the
Debian-family guard exists to prevent (`boot_kernels.yml:2-11`); and the guard itself stays, so
the hook is installed only where the relabel both applies and helps.

Both halves of this were measured on an Ubuntu 26.04.1 worker host running 7.0.0-31-generic.
Reinstalling that kernel package with no hook present left `/boot/vmlinuz-7.0.0-31-generic` at
`600 root root`; with the hook installed, the same install left it `640 root kvm`.

## Decision

We will install a static hook at `/etc/kernel/postinst.d/kdive-kvm-readable` from the role's
Debian-family block, which re-asserts `0640 root:kvm` over `/boot/vmlinuz-*` and `/boot/vmlinux-*`
on every kernel install, and we will keep the existing `find` + `file` loop alongside it. The hook
reports on stderr and exits 0 whenever it cannot relabel, rather than failing the package install.

The two arms cover disjoint sets and neither subsumes the other: the loop converges the kernels
already on a host when the role first runs, including the running one; the hook keeps every later
kernel converged without an operator.

## Consequences

A Debian-family worker host stops silently losing its guest-image build capability across a kernel
upgrade, and `boot_kernels.yml:64-65`'s manual instruction becomes false and is removed.

The role gains its first `files/` directory, and `lint-shell` gains `deploy/ansible/roles` so the
shipped hook is shellchecked like every other script this repository ships.

The hook is host state that no later role run reconciles: an operator who edits or deletes it gets
no warning until `scripts/operations/check-local-libvirt.sh` FAILs. Choosing exit 0 over a hard
failure accepts that a host whose `kvm` group has been removed reverts to the old defect quietly,
with only a stderr line during the upgrade; that trade is argued below.

`live_vm_host/tasks/main.yml:86-104` carries its own duplicate point-in-time relabel and never
imports `boot_kernels.yml`, so this decision does not reach the self-hosted runner. Consolidating
the two is deferred; it moves the runner task baseline and is a separate change.

## Considered & rejected

- **Do nothing; keep the documented operator re-run.** verified: the instruction exists only as a
  YAML comment at `boot_kernels.yml:64-65`, nothing in the repository prompts for it, and the
  first failure signal is a failed guest-image build — the defect #2567 reports.
- **Relabel only the image path the hook contract supplies as `$2`.** judgment: it binds the hook
  to an argument contract for no gain, and still needs the `vmlinuz`/`vmlinux` distinction that
  globbing the two patterns gets for free, since the role's `find` already spans both for x86_64
  and ppc64le (ADR-0356).
- **Drop the `find` + `file` loop and rely on the hook alone.** verified: the hook runs only on a
  subsequent kernel install, so on a freshly provisioned host every kernel already in `/boot`,
  the running one included, keeps `0600 root:root` — precisely the state `boot_kernels.yml:55-72`
  exists to clear. The issue's own scoping bullet states the hook does not cover them.
- **Exit non-zero when the relabel cannot be applied.** verified: on Ubuntu 26.04.1 (kernel
  7.0.0-31-generic), a `/etc/kernel/postinst.d` hook that exits 1 made
  `apt-get install --reinstall linux-image-7.0.0-31-generic` exit 100 and left the package
  `install ok half-configured`, needing `dpkg --configure -a` to repair. The failure it would be
  signalling — an unreadable kernel — is already detected by
  `scripts/operations/check-local-libvirt.sh` as a FAIL, so breaking package management is the
  worse of the two outcomes.
- **Inline the script with `ansible.builtin.copy`'s `content:` instead of a `files/` entry.**
  verified: `lint-shell` (`justfile:477-479`) walks only `scripts deploy/compose
  deploy/remote-libvirt-guest-helpers deploy/ansible/tests examples deploy/systemd
  .github/scripts`, so a script embedded in YAML is never shellchecked; `shfmt -f
  deploy/ansible/roles` matches nothing today, so adding that one path costs no other file.
- **Watch `/boot` with a systemd path unit and reconcile.** judgment: a polling reconciler against
  an event the platform already delivers, with a unit to install, enable and debug.
