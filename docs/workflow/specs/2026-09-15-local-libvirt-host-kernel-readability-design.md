# Local-libvirt host kernel readability and an honest check-deps report

Issue: [#2479](https://github.com/randomparity/kdive/issues/2479). Branch
`fix/local-libvirt-host-kernel-readability-2479`, base `main`. Guardrails: `just lint`,
`just type`, `just lint-shell`, `just lint-ansible`, `just test-ansible`, `just records`,
`just ci`.

## Problem

Debian and Ubuntu ship `/boot/vmlinuz-*` as `root:root 0600`. libguestfs copies a host kernel
as the invoking user when it builds its supermin appliance, so every guest-image build on that
family fails with an opaque `supermin exited with error status 1` (ADR-0222). Three things are
wrong at `main`:

1. **Provisioning does not declare the requirement for this host.**
   `deploy/ansible/playbooks/local-libvirt-host.yml` runs `libvirt_stack`, `libvirt_pool_net`
   and `local_worker_host`; none touches `/boot`. The relabel exists only in `live_vm_host`
   (the self-hosted runner play) and `guest_image_prereqs` (the image play, which targets
   `remote_libvirt_hosts`). A local-libvirt host that builds images must therefore be fixed by
   hand, which AGENTS.md ("Provisioning parity is the extender's job") forbids.
2. **The docs name a remedy that does nothing.** `docs/operating/providers/local-libvirt.md`
   credits `examples/local-libvirt/install-host.sh` with the relabel and tells operators to
   re-run it after a kernel upgrade. That script is seven lines and only
   `exec just prepare-local-libvirt-host "$@"`, and `docs/operating/install.md` directs
   operators away from it.
3. **`just check-deps` never looks at `/boot`.** `scripts/check-setup-deps.sh` reports the
   libguestfs binding under a tier it declares warn-only and then prints "optional items above
   are not yet needed", while `baseline_kernel.py` raises `MISSING_DEPENDENCY` without it. The
   kernel mode is not probed at all, even though `check-local-libvirt.sh` already probes it —
   and that script's own remedy prescribes `chmod 0644`, which is wider than the mode
   `live_vm_host` chose for the same file.

Six diagnostics in the two preflight scripts also cite
`docs/operating/runbooks/four-method-live-run.md` "section 4b" / "§4b". That runbook is now a
redirect stub with one section and no numbered subsections, so the pointer resolves to nothing.

## Scope

**Provisioning first**, so the probe added afterwards reports against a path provisioning
satisfies.

- New `deploy/ansible/roles/local_worker_host/tasks/boot_kernels.yml`, imported from that
  role's `main.yml` after `worker_accounts.yml`. It finds `/boot/vmlinuz-*` and
  `/boot/vmlinux-*`, sets `group: kvm`, `mode: "0640"`, and then verifies as each worker
  account that every kernel is readable — all guarded to the Debian family. This composes
  decisions already in the tree rather than making new ones: `live_vm_host` chose
  `0640 root:kvm` over ADR-0222's prose `chmod 0644` so the mode stays group-scoped instead of
  undoing `/boot` hardening for every local uid, pairs it with a per-account read check
  (`live_vm_host/tasks/verify.yml`), and `guest_image_prereqs` guards the same file on the
  Debian family. Both the fixed worker accounts
  (`local_worker_host/tasks/worker_accounts.yml`) and the operator login account
  (`libvirt_stack/tasks/main.yml`) are already `kvm` members, so `0640 root:kvm` reaches both
  readers. The RedHat and Suse families ship these files world-readable and are left alone,
  which is also what keeps the RHEL/Rocky guestfs limit untouched.
- `scripts/check-setup-deps.sh` gains `BOOT_DIR` (overridable by `KDIVE_BOOT_DIR`, mirroring
  `check-local-libvirt.sh`) and a `probe_boot_kernels` future-tier entry whose remedy names
  `just prepare-local-libvirt-host` first and a direct `chgrp`/`chmod` second, and states that
  it reads as the invoking user. The manual-hint heading widens to cover a host-state entry,
  and the closing line stops calling the live and guest-image tier items "not yet needed". No
  tier reclassification and no exit-code change: ADR-0393's warn-only `future` tier is
  unchanged.
- `scripts/operations/check-local-libvirt.sh`'s host-kernel remedy moves from `chmod 0644` to
  the same recipe-first, `chgrp kvm` + `chmod 0640` guidance, so the repository states one mode
  for one failure.
- Docs: `local-libvirt.md` and `install.md` name the recipe that performs the relabel. The
  runbook's closing sentence is disambiguated to say it forbids copying a binding between
  *differing* Python versions — which is what `maybe_link_guestfs` already refuses — so the
  tooling and the runbook visibly agree. The six "section 4b" pointers in the two scripts name
  the section that exists.

Out of scope, with owners:

- Making `just setup` exit non-zero without the guestfs binding — ADR-0393's tier contract.
- build-fs on RHEL/Rocky — the `install.md` platform-support table.
- Restoring the four-method runbook to a runnable procedure — the toolset docs it redirects to.
  Only the pointers and that one ambiguous sentence change.
- The `live_vm_host` path — already correct.
- A preflight gate in `examples/local-libvirt/build-image.sh` — the example walkthrough.
- `guest_image_prereqs` task (a), which relabels the running kernel to `0644`. Its play targets
  `remote_libvirt_hosts` and runs the appliance as root, so it neither shares a host with this
  play nor needs the mode; changing it would add a `kvm`-group prerequisite that play does not
  establish. Owner: the image play.
- The `§4b` citations in accepted ADR-0214 and ADR-0393. Both sit in a `## Context` section,
  and `.github/scripts/profiles/adr.sh` makes every non-Status section of a merged record
  append-only, so correcting them takes a superseding record. Owner: those records.
- The four other `install-host.sh` attributions in `local-libvirt.md` (CRB, the Docker remedy,
  SELinux labelling, the python3.14 install) and the matching ADR-0640 reference. Owner: the
  example walkthrough.

No `src/kdive/` change: `baseline_kernel.py` is correct as written.

## Failure model

**Actors and deployments.** A local operator applying `just prepare-local-libvirt-host` to a
workstation or standalone local-libvirt host; the eight fixed `kdive-worker-N` accounts that
run build-fs there; an operator running `just check-deps` or `just check-local-libvirt` at a
terminal, as themselves or as root; CI running the pinned script tests with no `/boot` access.
No untrusted remote actor reaches any of this.

**Invariants and assets at stake.**

- `/boot` stays hardened against local uids outside `kvm`: no path here may reach `0644`.
- The RedHat and Suse families are neither narrowed from their world-readable default nor
  reddened by the new probe.
- `just check-deps` keeps exit 0 when only future-tier items are missing (ADR-0393).
- The role task is idempotent; a second apply reports no change.

**Accepted failure classes.**

- A Debian kernel *upgrade* installs a fresh `0600` file under a new name, so the relabel is
  not durable across upgrades. Accepted: the remedy is re-running the recipe, which is what
  `live_vm_host` already accepts and what the docs now say. `dpkg-statoverride` is keyed by
  path and does not cover a new version's filename either.
- A Debian host whose operator account is not yet in `kvm` still cannot read the kernels.
  Accepted: `libvirt_stack` adds the login user to `kvm` in the same play, and group membership
  needs a fresh login session, which `install.md` already states.
- `probe_boot_kernels` uses `[[ -r ]]`, which is true for uid 0 regardless of mode, so a root
  or `sudo`-wrapped `check-deps` run reports nothing about `/boot`. Accepted: the remedy string
  states that the probe reads as the invoking user, matching the caveat
  `check-local-libvirt.sh` already carries, and Success 2 below is bounded to a non-root run.
  The Ansible task, not the probe, is what makes the host correct.
- The probe reports rather than fixes. Accepted: `check-setup-deps.sh` remediates only distro
  packages and the guestfs symlink (ADR-0393); a privileged `/boot` mutation from a setup
  convenience is outside that contract.

**Covered elsewhere.** The guestfs binding's tier classification — ADR-0393. build-fs on
RHEL/Rocky — the `install.md` platform-support table. The runtime diagnostic mapping the
libguestfs stderr signature to a `CONFIGURATION_ERROR` — ADR-0222, unchanged.

## Threat model

**Boundaries.** None added. One widened: the mode of `/boot/vmlinuz-*` and `/boot/vmlinux-*` on
Debian-family hosts, from `root:root 0600` to `root:kvm 0640`. `probe_boot_kernels` only reads,
and reads a directory the invoking user already selects through `KDIVE_BOOT_DIR`.

**Actor model.** The untrusted party is a local unprivileged uid on the prepared host that is
not in `kvm`. The design trusts `kvm` membership, which the same play grants only to the named
operator account and the eight fixed worker accounts, and trusts root for the relabel, which
Ansible already holds in this play.

**Control per boundary.** The widened boundary is controlled by the group, not the mode: `0640
root:kvm` grants read to `kvm` members only, so a uid outside `kvm` gains nothing it did not
have at `0600`. The Debian-family `when` guard is the control preventing the same task
*narrowing* a RedHat or Suse host. Removing the `chmod 0644` remedy from
`check-local-libvirt.sh` is the control that stops an operator widening the boundary by hand.
The probe neither escalates nor mutates, and on failure prints a path it was given plus a
remedy — no file content.

**Out of scope.** Whether `kvm` is the right group for build-fs readers at all — that is
`live_vm_host`'s accepted decision and this change follows it. Durability across kernel
upgrades — accepted above. Hardening `/boot` beyond the distro default.

## Success

1. Applying `local-libvirt-host.yml` to a Debian-family host with `0600` kernels leaves every
   `/boot/vmlinuz-*` and `/boot/vmlinux-*` at `root:kvm 0640` and fails the play if any worker
   account still cannot read one; a second apply reports no change. The task changes nothing on
   a RedHat or Suse host.
2. `scripts/check-setup-deps.sh`, **run as a non-root user**, reports the host-kernel entry and
   names `just prepare-local-libvirt-host` when `BOOT_DIR` holds an unreadable kernel, and
   reports nothing for `/boot` when it holds a readable one or does not exist. Its exit status
   is unchanged in all three cases.
3. No `section 4b` or `§4b` pointer remains in `scripts/check-setup-deps.sh` or
   `scripts/operations/check-local-libvirt.sh`, and each replacement names a heading present in
   `four-method-live-run.md`.
4. `docs/operating/providers/local-libvirt.md` and `docs/operating/install.md` attribute the
   relabel to `just prepare-local-libvirt-host`, and neither attributes it to
   `examples/local-libvirt/install-host.sh`. No remedy in either preflight script prescribes
   `chmod 0644` for a host kernel.
5. `just ci` and `just records` pass.

## Validation

| Contract | Mode |
|---|---|
| `local_worker_host` declares the Debian-guarded `0640 root:kvm` relabel, the per-account read check, and the `main.yml` import | `focused-test` — `tests/deploy/test_live_worker_provisioning.py`, new cases reading the role YAML |
| `check-setup-deps.sh` reports an unreadable `BOOT_DIR` kernel with the recipe remedy, exit unchanged | `focused-test` — `tests/scripts/test_check_setup_deps.py`, new case driving `KDIVE_BOOT_DIR` |
| `check-setup-deps.sh` stays silent for a readable or absent `BOOT_DIR` | `focused-test` — `tests/scripts/test_check_setup_deps.py`, two new cases |
| The widened manual-hint heading | `focused-test` — updated assertion in `tests/scripts/test_check_setup_deps.py` |
| `check-local-libvirt.sh`'s host-kernel remedy names `chmod 0640` and `chgrp kvm`, not `chmod 0644` | `focused-test` — updated assertion in the existing `test_unreadable_host_kernel_fails_with_chmod_hint` |
| Both scripts' guestfs hints name a heading that exists in the runbook | `focused-test` — tightened pointer assertions in the existing venv-failure cases of `tests/scripts/test_check_setup_deps.py` and `tests/scripts/test_check_local_libvirt.py` |
| Prose corrections in `local-libvirt.md`, `install.md`, `four-method-live-run.md` | `task-test-not-applicable` — operator-facing prose with no executable or structural consumer; `just docs-links` already validates the link targets, and pinning wording would be a prose snapshot |
