# 0644 — Localhost libvirt host preparation

## Status

Proposed

## Context

`runner.yml` composes the local-libvirt roles for a remote CI-runner inventory.
The example installer prepares an operator workstation, but is a shell script and
does not expose the roles' cross-distribution support. Issue #2393 requires a
declarative localhost route without making Ansible a host prerequisite.

## Decision

Add a localhost playbook that composes `libvirt_stack`, `libvirt_pool_net`, and
`local_worker_host`. Its RedHat and Suse package routes also admit RHEL-compatible
and SLES distributions, and the lifecycle installer selects the shared modular daemon
tuple for SLES, with structural coverage; live proof remains separate. The playbook owns
only the remaining workstation contract:
operator `uv sync --locked --group live`, the root lifecycle installer, local rootfs directory,
source-tree traversal, and the project-venv guestfs binding. It invokes the lifecycle installer with the
witness DSN through Ansible `stdin`, never a command argument. A just recipe
runs the playbook through uv-pinned ansible-core.

## Consequences

The playbook requires an explicit, non-symlinked Git checkout with a resolvable revision,
the manifest, lifecycle installer and manifest builder, and local-libvirt fixtures; an operator
account; and a witness DSN.
It checks those conditions before role mutation. A dry-run `uv sync` provides the
change receipt for the locked live dependency synchronization. It can alter the local
host only when invoked with privilege escalation.
The guestfs ABI mismatch is visible but non-fatal, matching the existing installer.

## Considered & rejected

- **Call `install-host.sh` from the playbook.** verified: issue #2393 explicitly
  forbids this direction because the example becomes a thin caller.
- **Put all behavior in `local_worker_host`.** judgment: checkout, lifecycle DSN,
  and venv wiring are a workstation composition contract, not reusable worker setup.
