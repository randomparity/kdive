# 0642 — Reusable local worker host preparation

## Status

Accepted

## Context

ADR-0387 defines an Ubuntu 26.04 self-hosted runner whose Python 3.14 system
interpreter matches `python3-guestfs`. The `live_vm_host` role enforces that
contract before installing anything, but it also owns worker packages, fixed
accounts, and data directories that a non-runner local-libvirt host needs.
Issue #2391 requires those tasks to run on Fedora and openSUSE Tumbleweed while
keeping the runner's package, account, unit, and task order.

## Decision

We will extract the worker package, fixed-account, and data-directory tasks
into a `local_worker_host` role. Its `main.yml` composes those task files for a
non-runner host; `live_vm_host` imports the same files at their current task
positions and supplies its runner service account as the operator identity.
Package tasks branch by Ansible OS family. The Ubuntu runner's early ADR-0387
assert and Python/guestfs venv setup remain in `live_vm_host`.

## Consequences

The new role needs an explicit existing operator account. Its standalone path
fails before mutation for an unsupported distribution or missing account.
Debian, Ubuntu, Fedora, and openSUSE Tumbleweed are the named distributions;
the latter two have separate family package lists. The runner keeps its
existing package list and apt action.
Shared task defaults, including the two authority opt-in values read by fixed
account creation, move to the reusable role. Other runner and dormant authority
defaults remain with `live_vm_host`. Static `import_role` exposes the
shared defaults before the runner's preflight and verification tasks.

The provider-authority service, runner checkout/venv, Docker, and systemd unit
installation remain the Ubuntu runner's contract. Issue #2393 may compose the
reusable role with its own install path; this decision does not add that path.

## Considered & rejected

- **One task file inside `live_vm_host`.** judgment: direct reuse would still
  inherit runner-named defaults and make the role's main entry point fail on
  non-Ubuntu hosts.
- **Move the entire `live_vm_host` role to a new role.** judgment: its checkout,
  guestfs binding, Docker, and runner-owned units rely on ADR-0387 and would
  widen this issue into a new deployment design.
- **Use one generic `package` action.** judgment: it cannot preserve the
  Ubuntu runner's current apt cache-update behavior as directly as explicit
  per-family tasks.
