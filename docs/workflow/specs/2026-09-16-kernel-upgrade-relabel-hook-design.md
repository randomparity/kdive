# Kernel-upgrade relabel hook — design

Issue: #2567. Decision record: [ADR-0668](../../adr/0668-a-kernel-upgrade-re-applies-the-boot-relabel.md).

## Problem

`roles/local_worker_host/tasks/boot_kernels.yml:55-72` relabels the kernels in `/boot` when the role runs
to `0640 root:kvm`, so libguestfs' supermin appliance can copy one as the invoking worker account
(ADR-0222, #2479). A later kernel package installs a new file under a new name at `0600 root:root`. After
a reboot the running kernel is unreadable, builds fail, and `:64-65` leaves the repair to an operator.

## Scope

One static hook, `roles/local_worker_host/files/kernel-postinst-kvm-readable`, installed by one new
`ansible.builtin.copy` task in `boot_kernels.yml` — inside the existing Debian-family block, so that guard
and the kvm-group assertions above it govern it unchanged. It re-asserts the role's rule over
`/boot/vmlinuz-*` and `/boot/vmlinux-*`, the patterns the role's `find` uses for x86_64 and ppc64le
(ADR-0356), not the one image path the hook contract supplies. It exits 0 when it cannot act.

The `find` + `file` loop **stays**: the hook covers kernels installed after it exists, the loop those
already in `/boot` on first run, including the running one. Dropping it would regress first provision.
The now-false `:64-65` comment is corrected, and `lint-shell` gains `deploy/ansible/roles`.

Out: Fedora/RHEL/Suse relabel behaviour; retrofitting broken hosts; #2568's `onboard.sh`. Also out,
reported: `live_vm_host/tasks/main.yml:86-104` keeps a duplicate relabel, never imports
`boot_kernels.yml`, so the runner keeps this defect.

### Failure model

Actors: an operator applying `local_worker_host` as root to a Debian-family worker host, then `dpkg`
running the hook as root during a later kernel install. Both already require root; no remote actor.

Invariants: no path reaches `0644` (`boot_kernels.yml:11`); the relaxation stays scoped to group `kvm`.

Accepted failure classes:

- The hook cannot relabel (no `kvm` group; `chgrp`/`chmod` refused on read-only or `vfat` `/boot`). It
  warns and exits 0; `check-local-libvirt.sh` FAILs on the result, where a non-zero exit would instead
  half-configure `dpkg`.
- The runner keeps the defect (see Scope) — outside the frozen surface, reported.
- No repository gate can prove a real kernel upgrade — a host arm proves it.

Covered elsewhere: already-broken hosts — operator runbook, per the non-goals.

## Success

1. A kernel installed by a package upgrade on a Debian-family host ends `0640 root:kvm`.
2. The hook installs only under the existing Debian-family guard, and no path reaches `0644`.
3. The hook relabels both `vmlinuz-*` and `vmlinux-*`.
4. The role still relabels kernels already present on first run.

## Validation

- Guard covers the install task (2). focused-test: `tests/run-local-worker-host.py`'s `boot_kernel_guard`
  arm gains the hook task name; red if it leaves the block; green via `just test-ansible`.
- Constants are `0640`/`kvm`, never `0644` (2), and both globs are present (3). focused-test: a new
  assertion in that harness reads the shipped hook; red when the mode literal becomes `0644`.
- A real upgraded kernel lands `0640 root:kvm` (1). task-test-not-applicable: the observable needs a host,
  a privileged package install and `dpkg`'s own invocation; the pull request's real-host arm proves it.
- First-run relabel retained (4). task-test-not-applicable: the `find` + `file` loop is unchanged here.
