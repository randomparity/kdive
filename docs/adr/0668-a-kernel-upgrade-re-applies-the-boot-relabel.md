# 0668 — A kernel upgrade re-applies the /boot relabel, non-fatally

## Status

Accepted (2026-09-16)

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
upgrade. Three statements of the manual instruction become false and are corrected with it:
`boot_kernels.yml:64-65`, `docs/operating/install.md`, and
`docs/operating/providers/local-libvirt.md`. The runner runbook's equivalent
(`docs/operating/runbooks/self-hosted-kvm-runner.md:396`) stays accurate, because it names
`playbooks/runner.yml`, which this decision does not reach.

The role gains its first `files/` directory, and `lint-shell` gains `deploy/ansible/roles` so the
shipped hook is shellchecked like every other script this repository ships.

`ansible.builtin.copy` defaults to `force: true`, so a later run of the role does restore a hook an
operator edited or deleted. Nothing schedules that run, though, so between it and the edit the host
is back to the old behaviour with no signal until
`scripts/operations/check-local-libvirt.sh` FAILs. Nothing removes the hook either: decommissioning
a worker host, or deliberately re-tightening `/boot`, means deleting
`/etc/kernel/postinst.d/kdive-kvm-readable` by hand, or the relaxation re-asserts itself on the next
kernel install. Choosing exit 0 over a hard failure likewise accepts that a host which cannot
relabel — no `kvm` group, a read-only or `vfat` `/boot` — reverts quietly, with only a stderr line
inside the package manager's output; that trade is argued below.

`live_vm_host/tasks/main.yml:86-104` carries its own duplicate point-in-time relabel and never
imports `boot_kernels.yml`, so this decision does not reach the self-hosted runner. Consolidating
the two is deferred; it moves the runner task baseline and is a separate change.

## Considered & rejected

- **Do nothing; keep the documented operator re-run.** verified: the instruction exists only as a
  YAML comment at `boot_kernels.yml:64-65` and nothing prompts for it. Detection is not the gap —
  `scripts/operations/check-local-libvirt.sh:149,276-277` already FAILs on an unreadable kernel —
  but it is after the fact and out of band, so the first signal an operator actually meets is a
  failed guest-image build, which is the defect #2567 reports.
- **Relabel only the image path the hook contract supplies as `$2`.** judgment: it would cover the
  newly installed kernel and nothing else, so a kernel installed while the hook was absent or
  broken stays `0600` until someone re-runs the role. Globbing the two patterns the role's `find`
  already spans for x86_64 and ppc64le (ADR-0356) re-converges the whole directory each time, at
  the cost of a `chmod` on a handful of files, and depends on no argument contract.
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
- **Watch `/boot` with a systemd path unit and reconcile.** judgment: `.path` units are
  inotify-driven rather than polling, so the objection is not cost but fit — it is a second,
  general-purpose watcher on a directory whose relevant event the package manager already delivers
  to a hook directory built for it, and it adds a unit to install, enable and debug.
- **Register the mode with `dpkg-statoverride` instead of a hook.** verified: it is Debian's own
  mechanism for persisting ownership and modes across upgrades, but an override is a literal path,
  not a pattern — on Ubuntu 26.04.1,
  `dpkg-statoverride --add root kvm 0640 '/boot/vmlinuz-*'` was accepted and listed back verbatim
  as `root kvm 640 /boot/vmlinuz-*`, matching only a file of that exact name. Covering a kernel
  installed under a new name would mean adding an override per version, which needs the very
  install-time hook this decision installs.
