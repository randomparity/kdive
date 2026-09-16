# 0663 — Provisioning enables the container-engine daemon

## Status

Accepted (2026-09-16)

## Context

`local_worker_host` declares a container engine and a compose plugin on Fedora and openSUSE
Tumbleweed (ADR-0642, #2505), and `live_vm_host` declares them on the Debian-family runner. No task
anywhere in `deploy/` or `scripts/` enabled, started, or granted access to that engine, and before
this change the role's own defaults said so, in the comment above
`local_worker_host_compose_packages_fedora`. The
consumer is `scripts/live-stack/stack-services.sh`, which refuses UID 0 at `:69-72` and then runs
`docker compose`, so a provisioned host whose daemon is down or whose operator is outside the socket
group fails at bring-up after provisioning has reported success (#2557).

Three facts decide the shape of the fix rather than merely motivating it.

The engine's units are not independent. On a Fedora 44 host with `moby-engine-29.7.2-1.fc44`,
`docker.service` carries `Requires=docker.socket` and `ExecStart=/usr/bin/dockerd -H fd://`: the
daemon takes its listening descriptor from the socket unit and cannot run without it. The socket
unit carries `SocketGroup=docker` and `SocketMode=0660`, and `/usr/lib/sysusers.d/moby-engine.conf`
creates that group with no members.

The two families disagree about which unit the vendor preset enables. On that same host,
`/usr/lib/systemd/system-preset/90-default.preset:372` reads `enable docker.socket` and names
`docker.service` nowhere, so `systemctl list-unit-files` reports `docker.service` with preset
`disabled` and `docker.socket` with preset `enabled`. On Ubuntu 26.04, `docker.io
29.1.3-0ubuntu4.1` reports `docker.service` preset `enabled`, and dpkg policy also starts it at
install time. The state a host reaches by installing the package alone is therefore not the same
state on the two families, and on neither is it a state this repository asked for.

Third, a packager has already answered the question this record exists to settle. openSUSE
Tumbleweed's `docker-29.7.2_ce-41.1` ships a `docker.socket` whose header reads: "We use `BindsTo`
in order to make sure that you cannot use socket-activation with Docker (Docker must always start
at boot if enabled, otherwise containers will not run until some administrator interacts with
Docker)." Whether to enable the service or lean on activation was left open when #2557 was filed,
and settling it is that issue's stated purpose.

## Decision

**We will enable and start `docker.service` from provisioning on any host where the packaged unit
file exists, and grant the engine's socket group to the role's single operator account. We will not
rely on socket activation.**

Enabling the service is a superset of enabling the socket: `Requires=docker.socket` pulls the socket
unit in whenever the service starts, so this decision can never leave a host with the daemon enabled
and its socket absent. The converse is not true, and that asymmetry is most of the argument. The
socket unit is *started* by that dependency rather than separately *enabled*, so
`systemctl is-enabled docker.socket` can still read `disabled` on a host where everything works;
what matters is that `systemctl start docker.service` alone brings `/run/docker.sock` up as
`root:docker 0660`, which it does. The rest is observability. Socket
activation defers the daemon's first start to the first client connection, so the play cannot
witness that start; a daemon that will fail to come up is indistinguishable at provisioning time
from one that will, and the failure surfaces later as a socket error inside `stack-services.sh`.
That is the "provisioning reports success against a host that cannot run the stack" shape #2557
reports, so a fix preserving it has not fixed the issue. Tumbleweed's packagers reached the same
conclusion for the same reason, in the `BindsTo` comment quoted above.

**The gate is unit presence, and nothing else.** The `stat` of
`/usr/lib/systemd/system/docker.service` asks the only question these two tasks need answered: is
there a daemon here to enable and a socket to grant. A host without that unit gets neither task and
the play succeeds — `podman-docker`, Enterprise Linux and SLES with no engine installed. A host
with it gets both, including one the role did not install: `docs/operating/providers/local-libvirt.md`
tells Enterprise Linux and SLES operators — whose runtime this repository does not package — that
one of the two remedies is Docker's own repository, and a `docker-ce` install puts its unit at
exactly that path. Such a host gets a working runtime rather than a documented manual step, which is
the outcome #2557 asks for. This repository still installs no engine there.

The grant targets `local_worker_host_operator_user` and only that account. `live_vm_host` reaches
the same tasks with its runner account substituted for that variable — the substitution its
`Import reusable worker installation paths` and `Import reusable shared provider directories` tasks
already make for other reusable task files.

## Consequences

Provisioning now fails on a host whose engine cannot start, at the task that starts it, with
systemd's own message. This is the intended trade: a longer play that stops at the cause, in place
of a shorter play that succeeds and defers the symptom.

Two consequences run the other way, and neither is hypothetical. The runner's socket-group grant,
previously unconditional (the `Add the runner service account to the docker group` task this change
replaces), becomes conditional on the same probe: a Debian that stopped shipping the packaged unit
would skip both tasks silently rather than fail. Its apt install of `docker.io` is unconditional and
the package ships the unit today, so the condition holds — but what checks that is a run of
`deploy/ansible/playbooks/runner.yml` against a real host, not the play itself. And a deliberately masked
`docker.service` still satisfies the probe, because masking writes a `/etc/systemd/system` symlink
and leaves the packaged unit in place: the play then fails at the enable with systemd's own
`Unit /etc/systemd/system/docker.service is masked`, verified on a Fedora 44 host. Unmasking, or
removing the engine, is the operator's call; an opt-out variable would be more surface than the
risk, and the charter's outcome is that a host carrying an engine ends up able to run the stack.

The Debian path stops depending on dpkg policy for its correctness. The enable is a no-op there
(`docker.service` is already preset-enabled and running), so it costs one `ok` task per run and
converts an accident of packaging into a stated requirement that a future packaging change would
break loudly.

An Enterprise Linux or SLES host that installed Docker from Docker's own repository is enabled and
granted like any other. This repository still packages no runtime for those families —
`preflight.yml:2-15` admits them and `packages_redhat.yml:31-33` gates the engine install away from
them — so what changes is that an engine they installed themselves is made usable, not that one is
installed. The operator guide says so outright. A `podman-docker` host has no `docker.service`, so
the gate skips and its socket path stays operator-owned.

The grant assumes the engine's package created the socket group, true on all three verified
families: `moby-engine`'s `/usr/lib/sysusers.d/moby-engine.conf`, Tumbleweed's
`/usr/lib/sysusers.d/docker.conf` (gid 483, no members), and Ubuntu's `docker.io` (gid 109). A host
carrying the unit without the group fails the grant with the `user` module's own `Group docker does
not exist`, the correct outcome for a half-installed engine. Group membership also does not reach a
session that already exists; the operator guide says to start a fresh one.

Socket-group membership is root-equivalent: a member can start a container that mounts the host
filesystem. On the standalone path this is the operator account's **first** root-equivalent group
from provisioning — `local_worker_host` creates `kdive-live-control` (`worker_groups.yml:16`) but
adds nobody to it, and the only membership grant for that group in the tree is `live_vm_host`'s
`Add the runner to the live-worker control and libvirt groups`, on the runner. So this record does not widen a list the
standalone operator was already on; it adds one, to the account `preflight.yml:17-38` already
requires the caller to name and `stack-services.sh:69-72` already requires to be non-root.

**This extends ADR-0575 rather than amending it.** ADR-0575 excludes the *fixed worker slot
accounts* (`live_vm_host_worker_accounts`) from `kdive-live-control`, sudo, and Docker groups, and
that exclusion is untouched: `tasks/worker_accounts.yml:17-21` still grants those accounts exactly
`kdive-live-libvirt` and `kvm`, and the runtime verifier at `live_vm_host/tasks/verify.yml:109`
still fails a worker found in `docker`. The principal this record grants is the one on the other
side of that boundary.

## Considered & rejected

- **Enable `docker.socket` and let activation start the daemon.** verified: on Fedora 44 with
  `moby-engine-29.7.2-1.fc44`, `/usr/lib/systemd/system-preset/90-default.preset:372` already reads
  `enable docker.socket`, so on that family this is close to what the RPM's `systemctl preset` does
  — and it is the state #2557 was filed against. Tumbleweed's `docker-29.7.2_ce-41.1` ships a
  `docker.socket` carrying `BindsTo=docker.service` with a header saying socket activation is
  deliberately unusable with Docker, because containers would not run until an administrator
  interacted with it.
- **Enable both units explicitly, or probe with `ansible.builtin.service_facts` instead of a
  `stat`.** verified: `docker.service` on the Fedora host carries `Requires=docker.socket`, so a
  second enable asserts what systemd already guarantees; and the role's existing container-runtime
  probe is a registered `stat` (`tasks/packages_redhat.yml:13-15`) that the check-mode harness
  drives by injecting the register, which a whole-host fact gather is not.
- **Gate the enable on distribution rather than on the unit.** verified:
  `docs/operating/providers/local-libvirt.md` directs Enterprise Linux and SLES operators to Docker's
  own repository, and a `docker-ce` install places `docker.service` at the probed path — so a
  distribution gate would leave exactly #2557's defect standing on a host that followed this
  repository's own advice.
- **Assert that a host this role installed an engine for now carries the unit.** verified: keyed to
  distribution, that assert fails the play on a Fedora or Tumbleweed host running `podman-docker` —
  `packages_redhat.yml:31-33` and `packages_suse.yml:23-25` skip the engine install when
  `/usr/bin/docker` already exists, and `dnf repoquery -l podman-docker` (Rocky 10.2) lists
  `/usr/bin/docker` and man pages with no systemd unit. That is the podman route
  `defaults/main.yml:103-105` calls "the podman route this project's own setup hint recommends", so
  the assert would break the configuration the repository advises. Keyed to the install register
  instead, it guards a state the `dnf`/`zypper` task would already have failed on.
- **Refuse with an actionable message instead of performing the steps.** verified: #2557's
  "Expected" admits this branch, but `AGENTS.md` ("Provisioning parity is the extender's job")
  requires the role to own a host dependency it introduced, and the engine packages were added to
  this role by #2505. A refusal would leave `systemctl enable --now docker` as a hand-run step on a
  cattle host reprovisioned from these roles.
- **Grant the socket group to the fixed worker slot accounts as well.** verified: ADR-0575's
  Decision states those accounts "remain excluded from `kdive-live-control`, sudo, and Docker
  groups", `deploy/systemd/README.md:63-65` restates it, and `live_vm_host/tasks/verify.yml:109`
  enforces it at runtime. Nothing in #2557 needs it — the compose backends are brought up by the
  operator through `stack-services.sh`, never by a worker.
- **Probe the daemon with `docker info` and enable only when it fails.** judgment: the probe is a
  connection, so on a socket-activated host it starts the daemon it is testing for; a check that
  mutates what it measures is worse than the unconditional enable it would guard.
- **Do nothing; keep both steps in the operator guide.** verified:
  `docs/operating/providers/local-libvirt.md` documents them today, and #2557 was filed anyway, by
  the implementer of the change that made them reachable.
