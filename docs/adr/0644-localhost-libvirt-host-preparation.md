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
`local_worker_host`. The playbook owns only the remaining workstation contract:
operator `uv sync --group live`, the root lifecycle installer, local rootfs directory,
source-tree traversal, and the project-venv guestfs binding. It invokes the lifecycle installer with the
witness DSN through Ansible `stdin`, never a command argument. A just recipe
runs the playbook through uv-pinned ansible-core.

## Consequences

The playbook requires an explicit source checkout, operator account, and witness
DSN. It can alter the local host only when invoked with privilege escalation.
The guestfs ABI mismatch is visible but non-fatal, matching the existing installer.

## Considered & rejected

- **Call `install-host.sh` from the playbook.** verified: issue #2393 explicitly
  forbids this direction because the example becomes a thin caller.
- **Put all behavior in `local_worker_host`.** judgment: checkout, lifecycle DSN,
  and venv wiring are a workstation composition contract, not reusable worker setup.
