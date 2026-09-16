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
owning four tasks: a registered `stat` of the packaged unit path; an assert that a *declared* engine
left that unit behind; an enable-and-start of `docker.service`; and an append of the socket group to
`local_worker_host_operator_user`. The last two are gated on the `stat`, the assert on
`local_worker_host_engine_declared`. `local_worker_host/tasks/main.yml` imports the file after the
family package files.

Those are two deliberately different predicates. `local_worker_host_engine_declared` says this
repository installed an engine for this host (Fedora, Tumbleweed, and the Debian runner via an
override); the `stat` says the host has one. Declared-and-absent fails loudly. Undeclared-and-present
— an Enterprise Linux or SLES host that took the Docker-repository remedy
`docs/operating/providers/local-libvirt.md` recommends — is enabled and granted like any other,
because that is a working runtime rather than a documented manual step.

`live_vm_host/tasks/main.yml` replaces its existing standalone group-grant task with an
`import_role ... tasks_from: container_runtime.yml`, substituting `github_runner_user` for
`local_worker_host_operator_user` — the substitution that file already makes at `:367-372` and
`:844-848` for other reusable task files — and setting `local_worker_host_engine_declared: true`,
since its apt install at `:52-59` is unconditional. That is the ownership move: the grant policy
lives in one place instead of two, and the Debian runner gains the enable it lacks. The role's only
other consumer is `deploy/ansible/playbooks/local-libvirt-host.yml:150`, which applies the whole
role and so reaches the new tasks through `main.yml` without being edited.

Four new defaults carry the unit path, the service name, the socket group, and the declared-engine
predicate, so tasks and tests share the literals.

The stale comments at `defaults/main.yml:96-99` and `:139`, and the "Container engine" bullet in
`docs/operating/providers/local-libvirt.md`, are corrected in this change: automating a step the
docs call manual makes that prose false the moment the code lands.

Out: the engine and plugin package sets (#2505); installing a runtime on Enterprise Linux or SLES,
which stays operator-owned; socket-group membership for fixed worker slot accounts (ADR-0575,
preserved unchanged); rootless and podman socket paths; `local-libvirt-host.yml` (#2498, #2506);
the `/boot` relabel (#2544, #2567). `scripts/check-setup-deps.sh` and
`scripts/operations/check-local-libvirt.sh` gain no daemon probe — they are report-only preflights
and the play itself now fails at the cause.

## Failure model

**Actors and deployments.** A local operator running `just prepare-local-libvirt-host` against
their own workstation or a standalone worker host; a CI or operator run of
`deploy/ansible/playbooks/runner.yml` against the self-hosted KVM runner. Both run the role with
`become: true`. There is no unauthenticated or remote actor: Ansible reaches these hosts over an
operator-owned connection, and the role already requires root.

**Invariants and assets at stake.**
- ADR-0575's exclusion of `live_vm_host_worker_accounts` from Docker groups. Socket-group
  membership is root-equivalent, so a grant reaching a worker slot account would dissolve the
  boundary that ADR holds. `live_vm_host/tasks/verify.yml:109` is its runtime enforcement.
- The single-operator identity: exactly one account per invocation gains the group, and it is the
  one the caller named. On the runner that name arrives through a `vars:` rebinding at the call
  site, which is the one place it can go wrong.
- The exact-order runner task baseline in `deploy/ansible/tests/fixtures/runner-tasks-2391.txt`,
  which issue #2567 also edits.

**Accepted failure classes.**
- A host answering `/usr/bin/docker` through `podman-docker` gets no daemon and no group. Accepted:
  it has no `docker.service`, the podman socket path is operator-owned by
  `docs/operating/providers/local-libvirt.md`, and #2557 excludes it.
- An operator's existing login session does not gain the new group until it is replaced. Accepted:
  this is how supplementary groups work on every supported host; the remedy is a sentence in the
  operator guide, not code.
- A host carrying `docker.service` but not the socket group fails the grant with the `user` module's
  `Group docker does not exist`. Accepted: all three families that package an engine create the
  group from a sysusers entry — verified on Fedora 44 (`moby-engine.conf`), Tumbleweed
  (`docker.conf`, gid 483) and Ubuntu 26.04 (gid 109) — so this state is a half-installed engine,
  which should fail rather than be skipped past.
- A `docker.service` installed only as an `/etc/systemd/system` override, with no packaged unit, is
  not detected. Accepted: the packaged unit is at `/usr/lib/systemd/system/docker.service` on all
  three families that package an engine, verified on Fedora 44, Ubuntu 26.04 and a Tumbleweed
  container image.
- CI proves gating, ordering, grant target and call-site binding in check mode only; it cannot
  prove a daemon starts. Accepted: covered by the plan's real-host run, not by CI.

**Covered elsewhere.** Engine and plugin package selection — #2505 (closed). Enterprise Linux and
SLES runtime installation — `docs/operating/providers/local-libvirt.md`, operator-owned. Fixed
worker account authority — ADR-0575 and `live_vm_host/tasks/verify.yml:109`.

## Threat model

**Boundary inventory.** This change adds no entry point, parses no external input, and builds no
command, path, or URL from a non-literal. It changes one thing: the membership list of the host's
engine socket group, whose socket is `root:docker 0660`. On the runner path that widens a grant
`live_vm_host/tasks/main.yml:61-65` already makes. On the standalone path it is a new membership —
`local_worker_host` grants no Docker group today, and it creates `kdive-live-control`
(`worker_groups.yml:16`) without adding anyone to it — so it is the operator account's first
root-equivalent group from provisioning.

**Actor model.** The trusted parties are the operator account the caller names and root. The
untrusted parties on a provisioned host are the fixed worker slot accounts: unprivileged, no shell,
no sudo, reachable by a compromised provider operation. The design places its trust in the caller's
choice of `local_worker_host_operator_user`, which `tasks/preflight.yml:17-38` asserts is non-empty
and present in passwd before any mutation on the `main.yml` path.

**Control per boundary.** The grant names exactly one account, from one variable, with
`append: true` so no unrelated membership is removed. Nothing in the change reads
`live_vm_host_worker_accounts`, and a structural test asserts both that the task file never does
and that the runner call site rebinds the operator variable to `github_runner_user`. ADR-0575's
verifier at `live_vm_host/tasks/verify.yml:109` remains the runtime control that fails a worker
which gained a forbidden group.

**Explicitly out of scope.** Whether the named operator should hold root-equivalent authority at
all. That is a standing requirement of the deployment rather than a choice this change makes:
`stack-services.sh:69-72` refuses root and then needs the socket, and ADR-0575 and
`deploy/systemd/README.md:63-65` already place this operator on the privileged side of the
worker boundary. Also out: rootless engine configurations, and any hardening of the socket itself.

## Success

1. On a host with `/usr/lib/systemd/system/docker.service`, the play leaves `docker.service`
   enabled and active, and the named operator in the socket group.
2. On a host without that unit and with no declared engine, both mutating tasks skip and the play
   still succeeds. Where an engine *was* declared, the play fails naming the probed path.
3. The grant names `local_worker_host_operator_user` in `container_runtime.yml`, and its two call
   sites — `local_worker_host/tasks/main.yml` and `live_vm_host/tasks/main.yml` — bind that
   variable to the operator and the runner account respectively and to no other account.
4. The runner play gains the enable; its listed task order is otherwise unchanged.
5. A second run of the play reports `changed=0` for the new tasks.
6. `defaults/main.yml:96-99` and `:139` and the "Container engine" bullet in
   `docs/operating/providers/local-libvirt.md` no longer describe the enable or the group grant as
   a manual operator step.

Verification of every contract above is inventoried per task in the
[implementation plan](../plans/2026-09-16-container-engine-daemon-enablement.md); the one contract
no repository gate can carry — that the daemon actually starts and the socket is reachable without
sudo — is proved by that plan's real-host run.
