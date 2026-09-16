# 0663 — Provisioning enables the container-engine daemon

## Status

Accepted (2026-09-16)

## Context

`local_worker_host` declares a container engine and a compose plugin on Fedora and openSUSE
Tumbleweed (ADR-0642, #2505), and `live_vm_host` declares them on the Debian-family runner. No task
anywhere in `deploy/` or `scripts/` enables, starts, or grants access to that engine, and the role's
own defaults say so at `deploy/ansible/roles/local_worker_host/defaults/main.yml:96-99`. The
consumer is `scripts/live-stack/stack-services.sh`, which refuses UID 0 at `:69-72` and then runs
`docker compose`, so a provisioned host whose daemon is down or whose operator is outside the socket
group fails at bring-up after provisioning has reported success (#2557).

Two facts decide the shape of the fix rather than merely motivating it.

The engine's units are not independent. On Fedora 44, `moby-engine-29.7.2-1.fc44` ships
`docker.service` with `Requires=docker.socket` and `ExecStart=/usr/bin/dockerd -H fd://`: the daemon
takes its listening descriptor from the socket unit and cannot run without it. The socket unit
carries `SocketGroup=docker` and `SocketMode=0660`, and `/usr/lib/sysusers.d/moby-engine.conf`
creates that group with no members.

The two families disagree about which unit the vendor preset enables. On the same Fedora host,
`/usr/lib/systemd/system-preset/90-default.preset:372` reads `enable docker.socket` and names
`docker.service` nowhere, so `systemctl list-unit-files` reports `docker.service` with preset
`disabled` and `docker.socket` with preset `enabled`. On Ubuntu 26.04, `docker.io
29.1.3-0ubuntu4.1` reports `docker.service` preset `enabled`, and dpkg policy also starts it at
install time. So the state a host reaches by installing the package alone is not the same state on
the two families, and on neither family is it a state this repository asked for.

Whether to enable the service or to lean on the socket unit's activation was left open when #2557
was filed, and settling it is that issue's stated purpose.

## Decision

**We will enable and start `docker.service` from provisioning, on any host where the packaged unit
file exists, and grant the engine's socket group to the role's single operator account. We will not
rely on socket activation.**

The enable is gated on `/usr/lib/systemd/system/docker.service` being present, so the families
`preflight.yml:2-15` admits without a declared engine — Enterprise Linux, SLES — and a host that
answers `/usr/bin/docker` through `podman-docker` skip both tasks instead of failing on a unit or a
group that does not exist.

The grant targets `local_worker_host_operator_user` and only that account. `live_vm_host` reaches
the same tasks with its runner account substituted for that variable, which is the substitution
`deploy/ansible/roles/live_vm_host/tasks/main.yml:369-372` already makes for other reusable task
files.

Enabling the service is a superset of enabling the socket: `Requires=docker.socket` pulls the socket
unit in, so this decision can never leave a host with the daemon enabled and its socket absent. The
converse is not true, and that asymmetry is most of the argument.

The rest of the argument is observability. Socket activation defers the daemon's first start to the
first client connection, which means the play cannot witness that start. A daemon that will fail to
come up — a broken storage driver, an SELinux denial, a containerd that is not running — is
indistinguishable at provisioning time from one that will come up, and the failure surfaces later as
a socket error inside `stack-services.sh`. That is precisely the "provisioning reports success
against a host that cannot run the stack" shape #2557 reports, so a fix that preserves it has not
fixed the issue.

## Consequences

Provisioning now fails on a host whose engine cannot start, at the task that starts it, with
systemd's own message. This is the intended trade: a longer play that stops at the cause, in place
of a shorter play that succeeds and defers the symptom.

The Debian path stops depending on dpkg policy for its correctness. The enable is a no-op there
(`docker.service` is already preset-enabled and running), so it costs one `ok` task per run and
converts an accident of packaging into a stated requirement that a future packaging change would
break loudly.

A host running `podman-docker` gets neither task. It has `/usr/bin/docker` and no `docker.service`,
so the gate skips, and the podman socket path stays what
`docs/operating/providers/local-libvirt.md` already says it is: operator-owned. This record does not
extend to it.

Adding the operator to the socket group does not affect sessions that already exist. An operator who
provisions and then runs `stack-services.sh` in the same login shell still needs a fresh session or
`newgrp`; the operator guide says so.

Socket-group membership is root-equivalent: a member can start a container that mounts the host
filesystem. This record grants it to one named operator account — the account that already holds
`kdive-live-control` and runs the lifecycle-control entry points — and to no other principal.

**This extends ADR-0575 rather than amending it.** ADR-0575 excludes the *fixed worker slot
accounts* (`live_vm_host_worker_accounts`) from `kdive-live-control`, sudo, and Docker groups, and
that exclusion is untouched: `tasks/worker_accounts.yml:17-21` still grants those accounts exactly
`kdive-live-libvirt` and `kvm`, and ADR-0575's account verifier still fails a worker that gains a
forbidden group. The principal this record grants is a different one, on the other side of that
boundary — it is the operator ADR-0575's `kdive-live-control` exclusion exists to distinguish
workers *from*.

## Considered & rejected

- **Enable `docker.socket` and let activation start the daemon.** verified: on Fedora 44 with
  `moby-engine-29.7.2-1.fc44`, `/usr/lib/systemd/system-preset/90-default.preset:372` already reads
  `enable docker.socket`, so on that family this is close to what the RPM's `systemctl preset`
  does — and it is the state #2557 was filed against. It also leaves the daemon's first start
  unwitnessed by the play, which is the half of the defect that lets provisioning report success
  against a host that cannot run the stack.
- **Enable both units explicitly.** verified: `docker.service` on that host carries
  `Requires=docker.socket`, so the socket is pulled in by the service's own dependency; a second
  task asserts what systemd already guarantees and adds a unit whose enablement no consumer reads
  independently.
- **Refuse with an actionable message instead of performing the steps.** verified: #2557's
  "Expected" admits this branch, but `AGENTS.md` ("Provisioning parity is the extender's job")
  requires the role to own a host dependency it introduced, and the engine packages were added to
  this role by #2505. A refusal would leave `systemctl enable --now docker` as a hand-run step on a
  cattle host that is reprovisioned from these roles.
- **Grant the socket group to the fixed worker slot accounts as well.** verified: ADR-0575's
  Decision states those accounts "remain excluded from `kdive-live-control`, sudo, and Docker
  groups", and `deploy/systemd/README.md:63-65` restates it. Nothing in #2557 needs it — the
  compose backends are brought up by the operator through `stack-services.sh`, never by a worker.
- **Probe the daemon with `docker info` and enable only when it fails.** judgment: the probe is a
  connection, so on a socket-activated host it starts the daemon it is testing for; a check that
  mutates what it measures is worse than the unconditional enable it would be guarding.
- **Detect the unit with `ansible.builtin.service_facts` instead of a `stat`.** judgment: it would
  also see an `/etc/systemd/system` override, but the role's existing container-runtime probe is a
  registered `stat` (`tasks/packages_redhat.yml:13-15`), and the check-mode harness drives that
  shape by injecting the register — a whole-host fact gather is not drivable the same way.
- **Do nothing; keep both steps in the operator guide.** verified:
  `docs/operating/providers/local-libvirt.md` documents them today, and #2557 was filed anyway,
  by the implementer of the change that made them reachable.
