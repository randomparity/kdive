# Pin the local-libvirt host play to the system interpreter (#2498)

## Problem

`deploy/ansible/inventory/hosts.yml` declares no `localhost`, so `just prepare-local-libvirt-host`
runs against implicit localhost and executes modules under the ephemeral `uv run --with
ansible-core` environment. That environment has no `lxml`, so `community.libvirt.virt_pool` dies at
`roles/libvirt_pool_net/tasks/main.yml:2:3` and the documented entry point exits 2 on every family.

## Scope

Declare `localhost` in `deploy/ansible/inventory/hosts.yml` with `ansible_connection: local` and
`ansible_python_interpreter: /usr/bin/python3`. Both keys are load-bearing: explicit `localhost`
alone leaves `auto` discovery on the launcher's interpreter. The pinned path is the one the play
already reads its system Python minor from, keeping modules and that ABI check on one interpreter.

Add the inventory contract to `deploy/ansible/tests/run-local-libvirt-host.py`, which
`just test-ansible` already runs inside `just ci`. State in `docs/operating/install.md` that the
recipe needs an interactive become password and why; note the pin in
`docs/operating/providers/local-libvirt.md` and the `justfile` recipe comment.

Out: the Galaxy-collection developer path (#2499), `--check` smoke-testability of the worktree
assertion (out of batch), converting the recipe off `--ask-become-pass` (documented, per triage),
`local-libvirt-host.yml` ~160-181 (#2506). No ownership transition: the inventory already owns
control-node connection facts. Rejected: the interpreter as a play var — verified: resolves
correctly, but leaves implicit localhost for every other play reading this inventory. `lxml` added
to the recipe's `uv run --with` list — judgment: the play mutates the host as root and must use the
host's own distro-packaged bindings, not a per-invocation copy.

### Failure model

- Actors: a local operator running the recipe on a supported family, at a terminal, with sudo. No
  CI job applies this play, and no untrusted input reaches it.
- Invariants: root executes the pinned interpreter; module execution and the guestfs ABI comparison
  must agree on which Python is the system Python.
- Accepted: a host lacking `/usr/bin/python3` fails closed on Ansible's own interpreter error, not
  reachable in the `install.md` support table. A non-default `-i` bypasses the inventory; every
  `deploy/ansible/tests/run-*` harness passes `-i localhost,` and is unaffected.
- Covered elsewhere: collection prerequisites (#2499); check-mode smoke testing (out of batch).

### Threat model

- Boundaries: one existing boundary narrowed, which binary Ansible executes as root on the control
  node; none added. Actors: the local operator, who already holds sudo, and no untrusted party.
- Control: a literal root-owned absolute path in a reviewed tracked file, replacing one under
  the invoking user's writable `uv` cache. Out of scope: sudo policy, and the become password.

## Success

1. The play resolves `/usr/bin/python3` for `localhost` under the repo `ansible.cfg` inventory.
2. `just test-ansible` fails when either inventory key is removed.
3. `docs/operating/install.md` states the interactive become-password requirement and its reason.

## Validation

- Inventory pins the system interpreter for `localhost`. Mode: `focused-test` —
  `run-local-libvirt-host.py` asserts both keys; red by deleting either; green via
  `uv run --with 'ansible-core==2.21.1' python3 deploy/ansible/tests/run-local-libvirt-host.py`.
- Documentation states the become-password requirement. Mode: `task-test-not-applicable` — prose
  for a human reader, validated by no executable consumer; a wording snapshot tests nothing.
