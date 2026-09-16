# Container-engine daemon enablement — design

Issue: #2557. Decision record: [ADR-0663](../../adr/0663-provisioning-enables-the-container-engine-daemon.md).

## Problem

`local_worker_host` installs a container engine and compose plugin on Fedora and Tumbleweed, and
`live_vm_host` installs them on the Debian-family runner, but nothing enables the daemon and nothing
grants the socket group. `scripts/live-stack/stack-services.sh:69-72` refuses UID 0 and then runs
`docker compose` against a `root:docker 0660` socket, so provisioning can report success against a
host that cannot bring the stack up. The role documents its own gap at
`deploy/ansible/roles/local_worker_host/defaults/main.yml:96-99` and
`docs/operating/providers/local-libvirt.md` repeats it as manual operator work.

## Scope

One reusable task file, `deploy/ansible/roles/local_worker_host/tasks/container_runtime.yml`,
owning three tasks: a registered `stat` of the packaged unit path, an enable-and-start of
`docker.service`, and an append of the socket group to `local_worker_host_operator_user`. Both
mutating tasks are gated on the probe. `local_worker_host/tasks/main.yml` imports it after the
family package files.

`live_vm_host/tasks/main.yml` replaces its existing standalone group-grant task with an
`import_role ... tasks_from: container_runtime.yml`, substituting `github_runner_user` for
`local_worker_host_operator_user` — the substitution that file already makes at `:369-372` and
`:845-848` for other reusable task files. That is the ownership move: the grant policy lives in one
place instead of two, and the Debian runner gains the enable it lacks. The role's only other
consumer is `deploy/ansible/playbooks/local-libvirt-host.yml:150`, which applies the whole role and
so reaches the new tasks through `main.yml` without being edited.

Three new defaults carry the unit path, the service name, and the socket group so the harness can
name them without duplicating literals.

The stale comments at `defaults/main.yml:96-99` and `:139`, and the "Container engine" bullet in
`docs/operating/providers/local-libvirt.md`, are corrected in this change: automating a step the
docs call manual makes that prose false the moment the code lands.

Out: the engine and plugin package sets (#2505); Enterprise Linux and SLES runtime provisioning
(operator-owned); socket-group membership for fixed worker slot accounts (ADR-0575, preserved
unchanged); rootless and podman socket paths; `deploy/ansible/playbooks/local-libvirt-host.yml`
(#2498, #2506); the `/boot` relabel (#2544, #2567). `scripts/check-setup-deps.sh` and
`scripts/operations/check-local-libvirt.sh` gain no daemon probe here — they are report-only
preflights and the play itself now fails at the cause.

### Failure model

**Actors and deployments.** A local operator running `just prepare-local-libvirt-host` against
their own workstation or a standalone worker host; a CI or operator run of
`deploy/ansible/playbooks/runner.yml` against the self-hosted KVM runner. Both run the role with
`become: true`. There is no unauthenticated or remote actor: Ansible reaches these hosts over an
operator-owned connection, and the role already requires root.

**Invariants and assets at stake.**
- ADR-0575's exclusion of `live_vm_host_worker_accounts` from Docker groups. Socket-group
  membership is root-equivalent, so a grant that reached a worker slot account would silently
  dissolve the boundary that ADR exists to hold.
- The single-operator identity: exactly one account per invocation gains the group, and it is the
  one the caller named.
- The exact-order runner task baseline in `deploy/ansible/tests/fixtures/runner-tasks-2391.txt`,
  which issue #2567 also edits.

**Accepted failure classes.**
- A host answering `/usr/bin/docker` through `podman-docker` gets no daemon and no group. Accepted:
  it has no `docker.service`, the podman socket path is operator-owned by
  `docs/operating/providers/local-libvirt.md`, and #2557 excludes it.
- An operator's existing login session does not gain the new group until it is replaced. Accepted:
  this is how supplementary groups work on every supported host; the remedy is a sentence in the
  operator guide, not code.
- A `docker.service` installed only as an `/etc/systemd/system` override, with no packaged unit, is
  not detected. Accepted: every supported family's package installs the unit under
  `/usr/lib/systemd/system`, verified on Fedora 44 and Ubuntu 26.04.
- CI proves gating, ordering, and grant target in check mode only; it cannot prove a daemon starts.
  Accepted: covered by the real-host proof below, not by CI.

**Covered elsewhere.** Engine and plugin package selection — #2505 (closed). Enterprise Linux and
SLES runtime install — `docs/operating/providers/local-libvirt.md`, operator-owned. Fixed worker
account authority — ADR-0575 and its account verifier.

### Threat model

**Boundary inventory.** This change adds no boundary. It widens exactly one existing grant: the
membership list of the host's engine socket group, whose socket is `root:docker 0660`. It adds no
entry point, parses no external input, and builds no command, path, or URL from a non-literal.

**Actor model.** The trusted parties are the operator account the caller names and root. The
untrusted parties on a provisioned host are the fixed worker slot accounts: unprivileged, no shell,
no sudo, reachable by a compromised provider operation. The design places its trust in the caller's
choice of `local_worker_host_operator_user`, which `tasks/preflight.yml:17-38` already asserts is
non-empty and present in passwd before any mutation.

**Control per boundary.** The grant names exactly one account, from one variable, with
`append: true` so no unrelated membership is removed. Nothing in the change reads
`live_vm_host_worker_accounts`, and a structural test asserts that the task file never does.
ADR-0575's existing account verifier remains the runtime control that fails a worker which gained a
forbidden group.

**Explicitly out of scope.** Whether the operator account should hold root-equivalent authority at
all — ADR-0642 settled that it is the lifecycle-control operator, and `stack-services.sh` already
requires it. Rootless engine configurations. Any hardening of the socket itself.

## Success

1. On a host with `/usr/lib/systemd/system/docker.service`, the play leaves `docker.service`
   enabled and active, and the named operator in the socket group.
2. On a host without that unit, both mutating tasks skip and the play still succeeds.
3. The grant names `local_worker_host_operator_user` and no other account, on every code path.
4. The runner play gains the enable; its listed task order is otherwise unchanged.
5. A second run of the play reports `changed=0` for the three new tasks.
6. No repository prose describes the enable or the group grant as a manual operator step.

## Validation

- **Contract: the unit probe gates both mutating tasks.** Mode: `focused-test`.
  `deploy/ansible/tests/run-local-worker-host.py`, a new `container_daemon_gate` driving both arms
  by injecting the probe's register as an extra-var, as `container_runtime_gate` already does for
  the engine-install probe. Red before the task file exists: the enable task heading is absent.
  Green: `just test-ansible`.
- **Contract: the grant targets only the operator variable.** Mode: `focused-test`. Same file, a
  structural YAML read of `container_runtime.yml` asserting the `user` task's `name` is the
  operator variable and that the rendered file references no worker-account variable. Red: the
  file does not exist. Green: `just test-ansible`.
- **Contract: probe, then enable, then grant, and all three after the engine install.** Mode:
  `focused-test`. Same file, index comparison over the standalone `--list-tasks` output. Red:
  headings absent. Green: `just test-ansible`.
- **Contract: the exact-order runner baseline.** Mode: `focused-test`. The existing assertion at
  `run-local-worker-host.py:61` and `fixtures/runner-tasks-2391.txt`, both updated to the new
  count. Red: the run fails naming the old count. Green: `just test-ansible`.
- **Contract: the daemon is enabled, active, reachable without sudo, and the play is idempotent.**
  Mode: `task-test-not-applicable` for CI — no repository gate can start a systemd service, and the
  check-mode harness runs unprivileged. Proved instead by running the role against a real Fedora
  host (declared engine, preset-disabled service), a real Ubuntu host (declared engine,
  preset-enabled service), and a real Rocky host (no engine, skip arm), recording
  `systemctl is-enabled`, `systemctl is-active`, `docker info` as the operator without sudo, and a
  second-run `changed=0`.
- **Contract: the corrected prose.** Mode: `task-test-not-applicable`. Prose has no executable
  consumer and a wording snapshot would test nothing; `just docs-links` and `just lint` cover its
  mechanics.
