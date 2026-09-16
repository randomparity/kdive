# Pin the local-libvirt host play to the system interpreter (#2498)

## Problem

`just prepare-local-libvirt-host` executes modules under the ephemeral `uv run --with ansible-core`
environment, which has no `lxml`, so `community.libvirt.virt_pool` dies at
`roles/libvirt_pool_net/tasks/main.yml:2:3` and the entry point exits 2 on every family. The cause
is the interpreter binding: implicit localhost binds it to whichever Python launched the playbook.

## Scope

Pin `ansible_python_interpreter: /usr/bin/python3` in the `vars:` block of
`deploy/ansible/playbooks/local-libvirt-host.yml`. Measured: that alone resolves
`/usr/bin/python3`, because a play var outranks the implicit host var.

Leave `deploy/ansible/inventory/hosts.yml` alone. Declaring `localhost` there is neither necessary
nor safe: it is shared with `pki.yml` and the localhost plays under `deploy/ansible/tests/`, and a
declared host swaps their explicit launcher interpreter for `PATH` discovery — measured as a
`community.crypto` import failure under a restricted `PATH`.

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
  agree on which Python is the system Python; no other play's interpreter changes.
- Accepted: a `/usr/bin/python3` that is absent, or older than ansible-core's 3.9 target floor,
  fails closed on Ansible's own error at fact gathering, with no pre-check. Each supported family's
  `/usr/bin/python3` is the interpreter `libvirt_stack` installs its libvirt/lxml bindings for.
- Covered elsewhere: collection prerequisites (#2499); check-mode smoke testing (out of batch).

### Threat model

- Boundaries: one existing boundary narrowed, which binary Ansible executes as root in this play;
  none added. Actors: the local operator, who already holds sudo, and no untrusted party.
- Control: a literal root-owned absolute path in a reviewed tracked file, replacing one under
  the invoking user's writable `uv` cache. Out of scope: sudo policy, and the become password.

## Success

1. The play resolves `/usr/bin/python3`; no other play's interpreter changes.
2. `just test-ansible` fails if the pin is removed, set to another path, or moved to the inventory.
3. `docs/operating/install.md` states the interactive become-password requirement and its reason.

## Validation

- Interpreter resolution and pin placement. Mode: `focused-test` — `run-local-libvirt-host.py`
  resolves the value through a probe play built from the real play's vars; red under each fault in
  Success 2; green via `just test-ansible`.
- The runbook's become-password statement. Mode: `focused-test` —
  `test_install_topology_contract.py` couples it to the recipe's real `--ask-become-pass` flag;
  red before the `install.md` edit; green via `just test`.
