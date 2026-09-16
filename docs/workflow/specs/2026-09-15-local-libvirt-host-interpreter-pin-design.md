# Pin the local-libvirt host play to the system interpreter (#2498)

## Problem

`deploy/ansible/inventory/hosts.yml` declares no `localhost`, so `just prepare-local-libvirt-host`
runs against implicit localhost and executes modules under the ephemeral `uv run --with
ansible-core` environment. That environment has no `lxml`, so `community.libvirt.virt_pool` dies at
`roles/libvirt_pool_net/tasks/main.yml:2:3` and the documented entry point exits 2 on every family.

## Scope

Pin `ansible_python_interpreter: /usr/bin/python3` in the `vars:` block of
`deploy/ansible/playbooks/local-libvirt-host.yml`, and declare `localhost` in `hosts.yml` with
`ansible_connection: local` and no interpreter. The pin is load-bearing alone: without it `auto`
discovery selects the launcher's interpreter, not the path the play reads its Python minor from.

The play owns the pin because `hosts.yml` is shared. `playbooks/pki.yml` is also `hosts: localhost`
and documented to run with no `-i`, so an inventory host var would retarget its `community.crypto`
tasks onto a distro `python3-cryptography` nothing installs — this issue's defect, relocated.
Measured: pki resolution is unchanged here, and forced to `/usr/bin/python3` under the rejection.

Extend `deploy/ansible/tests/run-local-libvirt-host.py`, which `just test-ansible` runs inside
`just ci`, to assert resolution rather than key presence. `install.md` gains the become-password
requirement and its reason; note the pin in `providers/local-libvirt.md` and the `justfile` comment.

Out: the Galaxy-collection path (#2499); `--check` smoke-testability and `local-libvirt-host.yml`
~160-181 (#2506, out of batch); converting the recipe off `--ask-become-pass` (documented, per
triage). No ownership transition: each play already owns its execution vars.

### Failure model

- Actors: a local operator running the recipe on a supported family, at a terminal, with sudo. No
  CI job applies this play, and no untrusted input reaches it.
- Invariants: root executes the pinned interpreter; module execution and the guestfs ABI check
  agree on which Python is the system Python; no other localhost play inherits the pin.
- Accepted: a host lacking `/usr/bin/python3` fails closed on Ansible's own interpreter error, not
  reachable in the `install.md` support table. An `-i` override keeps today's behaviour.
- Covered elsewhere: collection prerequisites (#2499); check-mode smoke testing (out of batch).

### Threat model

- Boundaries: one existing boundary narrowed, which binary Ansible executes as root in this play;
  none added. Actors: the local operator, who already holds sudo, and no untrusted party.
- Control: a literal root-owned absolute path in a reviewed tracked file, replacing one under
  the invoking user's writable `uv` cache. Out of scope: sudo policy, and the become password.

## Success

1. The play resolves `/usr/bin/python3`, and the inventory pins no interpreter for `localhost`.
2. `just test-ansible` fails when the pin is removed, moved to the inventory, or `localhost` is
   dropped or grouped.
3. `docs/operating/install.md` states the interactive become-password requirement and its reason.

## Validation

- Interpreter resolution and inventory placement. Mode: `focused-test` —
  `run-local-libvirt-host.py` resolves through a probe play and reads back `ansible-inventory`;
  red under each fault in Success 2; green via `just test-ansible`.
- Runbook become-password statement. Mode: `focused-test` — `test_install_topology_contract.py`
  couples it to the recipe's `--ask-become-pass`; red before the edit, green via `just test`.
