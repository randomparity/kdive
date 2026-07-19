# ADR 0387 — Self-hosted KVM runner host codification

- **Status:** Proposed <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-07-19
- **Deciders:** Maintainer (randomparity), Claude Code

## Context

Epic #1289 (directional ADR-0386) runs the native-KVM `live_vm` tier on per-arch
self-hosted Rocky Linux 10 runners. Sub-issue A (#1290) fixed the test-side
environment contract in code. Sub-issue B (#1291) must build the **host side** of
that contract — libvirt, qemu, the kernel-debug toolchain, a staged-rootfs
location with the right SELinux label, and a registered GitHub Actions runner —
as reproducible automation, not tribal knowledge.

The `deploy/ansible` tree already automates a *remote-libvirt provider* host
(TLS listener, PKI, gdbstub ACL). A CI runner is a *local-libvirt* host with a
different contract: no TLS, no raw-TCP gdbstub egress, but it does need the
`live_vm` quirks (session-mode-capable QEMU, short `XDG_RUNTIME_DIR`,
`virt_image_t`-labeled staging) and a registered runner gated to trusted events.

Two forces shape the decision. First, the epic's primary target is ppc64le;
x86_64 is the cost-effective proof of concept, so the host build must be
arch-additive. Second, a self-hosted runner that accepts fork pull requests is
arbitrary code execution on the host, so the trusted-events posture is a
security boundary, not a preference.

## Decision

We will codify the runner host as **Ansible roles under `deploy/ansible`**,
reusing `libvirt_stack` (qemu/libvirt/libguestfs install, RHEL→modular-daemon
switch, KVM assertion) and `libvirt_pool_net` (`default` pool + network) as-is,
and adding two new roles plus a `playbooks/runner.yml`:

The **entire contract targets one account** — `github_runner_user`, the account
the runner *service* runs as, deliberately distinct from the Ansible connection
user (`ansible_user_id`). `live_vm_host` adds *that* account to `kvm`/`libvirt`
(not the connection user `libvirt_stack` adds), owns its staging and linger, and
the gate runs as it — otherwise the service process is not in the groups and the
host is "green but not ready" one layer down.

- `live_vm_host` — the contract delta, all for `github_runner_user`: the
  kernel-debug toolchain (`drgn`, `crash`, `makedumpfile`, `kexec-tools`,
  `kdump-utils`, `gdb`, foreign qemu per arch); the project `.venv` the worker's
  `guestfs`/`drgn` import needs (`uv sync --python /usr/bin/python3 --group live`
  + the `libguestfs` symlink, ABI-matched to the system interpreter) at a **pinned
  persistent path** D reuses via `KDIVE_PYTHON` rather than rebuilding per job,
  since system packages are not importable from the venv; both a
  world-traversable `virt_image_t`-labeled live-VM staging dir and the
  install-staging dir the gate checks; `loginctl enable-linger` for a short
  `XDG_RUNTIME_DIR`; and a **two-part verification gate** run as the service
  account after a connection reset — `scripts/check-local-libvirt.sh` (KVM /
  daemon / venv-import / network) plus the role's own assertions for group
  membership, the staging-dir label, parent traversability, and the
  `/run/user/<uid>` + service-unit `XDG_RUNTIME_DIR` the script does not cover.
- `github_runner` — arch-selected runner asset + `[self-hosted, kvm, <arch>]`
  label, download verified against an operator-pinned SHA-256 (not a same-origin
  fetch), a `.runner`-marker idempotence guard so a re-run of an already-registered
  host is `0 changed` and needs no token, registration as the non-root
  `github_runner_user` systemd service (with `Environment=XDG_RUNTIME_DIR` set,
  since `enable-linger` alone does not export it) **installed stopped/disabled**
  until `github_runner_service_enabled` is set so a bare run leaves no unguarded
  listener, a `no_log` registration token that **fails closed** when empty in the
  first-time branch, and `KDIVE_SECRETS_ROOT` wired as the pointer to the
  provisioned-System family's S3 credentials (B sets the pointer; C/D or the
  operator populate the material).

Every host-build step resolves by architecture. The one step that is **not** a
free ppc64le drop-in — the `actions/runner` binary, for which upstream ships no
ppc64le asset — is made an explicit parameterized seam: `github_runner_tarball_url`
overrides the derived asset URL, and with neither an upstream asset nor an
override the role fails loud naming the gap. The trusted-events posture
(`schedule` + `workflow_dispatch` only; never fork PRs; `KDIVE_S3_*` on repo/org
secrets) is enforced by *ordering*, not documentation alone: because B is
sequenced before D, the runner service installs **stopped** and the runbook
requires the repo outside-collaborator-approval setting and D's `if:` guard
before the operator enables it — so a bare `runner.yml` run never leaves an
unguarded listener a fork PR could target. The runner binary cannot enforce
event trust itself; the workflow `if:` guard (sub-issue D) and the repo setting
do, and installing stopped keeps the window closed until they exist.

## Consequences

Easier:

- A runner host is reproducible from the automation; a fresh host (or a
  re-provision after failure) reaches the contract without from-memory steps.
- Adding the ppc64le POWER runner is one documented step (build/point-at a
  ppc64le `actions/runner`, set the override URL) plus the unchanged arch-keyed
  host build — the primary target is unblocked by construction.
- The host-contract check is codified: the role runs `check-local-libvirt.sh` and
  fails if the host is not ready, so "green but not actually ready" is caught.

Harder / new obligations:

- A self-hosted runner must be kept healthy and its trusted-events gating
  maintained (workflow `if:` guard in D, repo outside-collaborator-approval
  setting); the runbook and ADR record this as a security boundary.
- Two more Ansible roles and a `test-ansible` regression case to maintain.
- The ppc64le runner binary remains an operator responsibility (self-built) until
  upstream ships a ppc64le asset; the override seam bounds that cost.

No database migration; this is ops/infra only. The `live-vm` CI job itself
(matrix, schedule trigger, fail-loud env preflight) is sub-issue D, wired onto
the host and secrets model this decision produces.

## Alternatives considered

- **A standalone `scripts/setup-live-vm-runner.sh` shell script.** Rejected:
  weaker idempotence, no `ansible-lint`/`test-ansible` coverage, and
  arch-parameterization becomes hand-rolled `case` logic — the exact
  duplication the Ansible reuse avoids. A runner host is a multi-package,
  multi-service, must-be-idempotent build, which is Ansible's fit.
- **Extend the existing remote-libvirt `site.yml` with a runner toggle.**
  Rejected: it couples the local-CI-runner contract (no TLS) to the
  remote-provider contract (TLS/PKI/gdbstub ACL), muddying both; a runner would
  drag in a TLS listener and a firewall ACL it does not need, and a conditional
  toggle through those roles is more fragile than two focused new roles.
- **Fold the runner build into this sub-issue's CI job wiring.** Rejected: the
  epic sequences B (host) before D (CI job) precisely so the host contract is
  fixed before a job is wired onto it; doing both here overlaps D's scope and
  couples host provisioning to workflow YAML churn.
- **Hard-code x86_64 now, generalize to ppc64le later.** Rejected on the same
  ground as ADR-0386: retrofitting arch into a hardened host build is the
  expensive path, and ppc64le is the primary target. The arch-keyed maps and the
  runner-binary override seam make POWER a drop-in.
- **Skip the checksum verification on the runner download.** Rejected: the host
  will execute CI workloads; extracting an unverified tarball fetched over the
  network onto that host is an avoidable supply-chain exposure.
