# Container-engine daemon enablement — implementation plan

Goal: make `local_worker_host` leave a provisioned host with the container-engine daemon enabled and
running and the named operator account in the engine's socket group, and route the Debian-family
runner through the same tasks.

Architecture: one new reusable Ansible task file in `local_worker_host`, imported from that role's
`main.yml` and from `live_vm_host/tasks/main.yml` with the runner account substituted for the
operator variable. Three new role defaults name the unit path, the service, and the socket group.
The shared check-mode harness `deploy/ansible/tests/run-local-worker-host.py` gains gate, target,
and ordering assertions, and its exact-order runner fixture is regenerated.

Tech stack: Ansible (`ansible.builtin` modules, `profile: production` ansible-lint), Python 3.14 for
the harness, `just` recipes for guardrails.

Expected implementation size: 180–260 changed lines (M) — derived from the file map below: one new
~35-line task file, ~12 lines of defaults, ~10 lines across the two `main.yml` files, ~2 lines of
fixture, ~80 lines of harness tests, and ~25 lines of corrected prose.

Design: [spec](../specs/2026-09-16-container-engine-daemon-enablement-design.md),
[ADR-0663](../../adr/0663-provisioning-enables-the-container-engine-daemon.md).

## Global Constraints

- Ansible module names are fully qualified (`ansible.builtin.*`); ansible-lint runs
  `profile: production` with `offline: true` (`.ansible-lint`). Task names start with a capital.
- Role variables carry the `local_worker_host_` prefix or an explicit
  `# noqa: var-naming[no-role-prefix]`.
- `deploy/ansible/tests/run-local-worker-host.py:159-168` forbids the strings `Docker Engine`,
  `runner service account`, `fixture catalog`, and `authority service` in any standalone-role task
  name. New task names must contain none of them.
- Ruff line length 100; `just lint` formats Python inside Markdown fences too.
- Prose rule: no "critical", "robust", "comprehensive", "elegant"; "Milestone", never "Sprint".
- Never write a hostname, IP, username, or internal domain into a committed file.
- Guardrails: `just lint`, `just type`, `just lint-ansible`, `just test-ansible`,
  `git fetch origin main && just records`, and `just ci > <file> 2>&1 < /dev/null` before push.

## Task 1 — The reusable enable-and-grant task file

Creates `deploy/ansible/roles/local_worker_host/tasks/container_runtime.yml`.
Modifies `local_worker_host/defaults/main.yml`, `local_worker_host/tasks/main.yml`, and
`docs/operating/providers/local-libvirt.md`.

Where it fits: the whole behavioural change for the standalone role, with the prose it falsifies
corrected in the same task. Task 2 routes the runner through it; Task 3 proves it.

**Interfaces.** Consumes `local_worker_host_operator_user` (existing, `defaults/main.yml:3`,
asserted non-empty and present in passwd by `tasks/preflight.yml:17-38`). Defines, for later tasks:

- `local_worker_host_engine_service: docker.service`
- `local_worker_host_engine_unit_path: /usr/lib/systemd/system/docker.service`
- `local_worker_host_engine_socket_group: docker`
- registered variable `local_worker_host_engine_unit` (an `ansible.builtin.stat` result)
- task names, exactly: `Look for a packaged container-engine service unit`,
  `Enable and start the container-engine daemon`,
  `Add the local-worker operator to the container-engine socket group`
- tag `container_runtime` on all three.

**Verification.**
- Contract: the three tasks parse, carry the tag, and gate on the probe. Mode: `focused-test` —
  Task 3's `container_daemon_gate`; this task's local green command is `just lint-ansible`,
  expected exit 0 with no findings.
- Contract: the new defaults are valid YAML the task file consumes. Mode: `focused-test` — Task 3's
  `container_daemon_grant_target`; local green is `just lint-ansible`.
- Contract: the corrected prose. Mode: `task-test-not-applicable` — prose has no executable
  consumer and a wording snapshot would assert only that the words did not change. `just
  docs-links` covers its link mechanics.

**Steps.**

1. Append to `local_worker_host/defaults/main.yml`, after
   `local_worker_host_engine_packages_tumbleweed`:

   ```yaml
   # The engine's systemd identity, named once so tasks and tests share the literals. The probe
   # path is where every supported family's package installs the unit: Fedora's moby-engine and
   # Ubuntu's docker.io both ship /usr/lib/systemd/system/docker.service, and /lib is usr-merged on
   # every supported release. A host answering /usr/bin/docker through podman-docker has no such
   # unit, which is the state the probe exists to detect (ADR-0663).
   local_worker_host_engine_service: docker.service
   local_worker_host_engine_unit_path: /usr/lib/systemd/system/docker.service
   # The socket unit's SocketGroup; its socket is root:docker 0660, so a non-root operator outside
   # this group cannot reach the daemon and stack-services.sh fails at bring-up (#2557).
   local_worker_host_engine_socket_group: docker
   ```

2. Create `local_worker_host/tasks/container_runtime.yml`:

   ```yaml
   ---
   # Declaring the engine packages is not a working runtime (ADR-0663, #2557). Fedora's preset
   # enables docker.socket and leaves docker.service disabled; the RPM's sysusers entry creates the
   # docker group empty. Debian reaches the working end state only through dpkg install-time
   # policy, which is packaging behaviour this repository does not control. Both mutating tasks are
   # gated on the packaged unit so the families preflight.yml admits without a declared engine —
   # Enterprise Linux, SLES — and podman-docker hosts skip rather than fail.
   - name: Look for a packaged container-engine service unit
     ansible.builtin.stat:
       path: "{{ local_worker_host_engine_unit_path }}"
     register: local_worker_host_engine_unit
     tags: [container_runtime]

   - name: Enable and start the container-engine daemon
     # The service, not the socket: docker.service carries Requires=docker.socket and
     # ExecStart=/usr/bin/dockerd -H fd://, so enabling the service pulls the socket in and the
     # play witnesses the daemon's first start. Socket activation would defer that start past the
     # end of provisioning, which is the failure #2557 reports.
     ansible.builtin.systemd_service:
       name: "{{ local_worker_host_engine_service }}"
       enabled: true
       state: started
     when: local_worker_host_engine_unit.stat.exists
     tags: [container_runtime]

   - name: Add the local-worker operator to the container-engine socket group
     # ADR-0575 keeps the fixed worker slot accounts out of Docker groups; this grant is for the
     # lifecycle-control operator only, a different principal. append: true so no existing
     # membership is removed.
     ansible.builtin.user:
       name: "{{ local_worker_host_operator_user }}"
       groups: "{{ local_worker_host_engine_socket_group }}"
       append: true
     when: local_worker_host_engine_unit.stat.exists
     tags: [container_runtime]
   ```

3. In `local_worker_host/tasks/main.yml`, insert between the Tumbleweed package import and
   `Prepare fixed worker groups`:

   ```yaml
   - name: Enable the container engine and grant the operator its socket
     ansible.builtin.import_tasks: container_runtime.yml
   ```

4. Replace the last paragraph of the `local_worker_host_compose_packages_fedora` comment block
   (`defaults/main.yml:96-99`) with:

   ```yaml
   # Fedora's RPM leaves docker.service disabled — its preset enables only docker.socket — and its
   # sysusers entry creates the docker group empty. tasks/container_runtime.yml enables and starts
   # the service and adds local_worker_host_operator_user to that group (ADR-0663, #2557), so the
   # packages below are a working runtime rather than a declared one.
   ```

   and replace `defaults/main.yml:139` (`# The same daemon-enable and socket-access caveat
   applies.`) with:

   ```yaml
   # tasks/container_runtime.yml enables the daemon and grants the socket group here too.
   ```

5. In `docs/operating/providers/local-libvirt.md`, replace the last three sentences of the
   "Container engine" bullet — from `On Fedora and openSUSE the packages alone` to
   `without both it fails on the socket.` — with:

   ```markdown
   `just prepare-local-libvirt-host` enables and starts `docker.service` and adds the operator
   account to the `docker` group on every family whose package installs the unit
   ([ADR-0663](../../adr/0663-provisioning-enables-the-container-engine-daemon.md)); the new group
   does not reach a login session that already existed, so start a fresh one before running
   `stack-services.sh`. Enterprise Linux, SLES, and a `podman-docker` host install no
   `docker.service`, so provisioning skips both steps there and the runtime stays operator-owned.
   ```

6. Run `just lint-ansible` and `just docs-links`. Expect both to exit 0.

**Acceptance.** The three task names appear, in order, in
`ansible-playbook deploy/ansible/tests/local_worker_host.yml -i localhost, --list-tasks` after
`Install the Tumbleweed compose plugin for the on-box stack and testcontainers`.
`rg -n 'remain operator steps|manual steps' deploy/ docs/operating/providers/local-libvirt.md`
returns no hit describing the enable or the group grant. `just lint-ansible` exits 0.

## Task 2 — Route the runner through the same tasks

Modifies `deploy/ansible/roles/live_vm_host/tasks/main.yml` and
`deploy/ansible/tests/fixtures/runner-tasks-2391.txt`.

Where it fits: closes #2557's "the Debian path should stop depending on dpkg policy", and removes
the duplicated grant policy.

**Interfaces.** Consumes the task file and defaults from Task 1, and `github_runner_user`
(existing, `live_vm_host/defaults/main.yml`). Produces a runner `--list-tasks` output three entries
longer and one entry shorter than the current baseline — net +2 — which Task 3's count reads.

**Verification.**
- Contract: the runner play lists the three new tasks in order and its exact-order baseline still
  matches. Mode: `focused-test` — `run-local-worker-host.py:61` and its fixture. Red: the run fails
  with `runner listed 316 baseline tasks, expected 314`. Green: `just test-ansible`.

**Steps.**

1. In `live_vm_host/tasks/main.yml`, replace the whole task currently at `:61-65`:

   ```yaml
   - name: Add the runner service account to the docker group
     ansible.builtin.user:
       name: "{{ github_runner_user }}"
       groups: docker
       append: true
   ```

   with:

   ```yaml
   - name: Enable the container engine and grant the runner account its socket
     # One owner for this policy (ADR-0663). The runner previously took the group grant here and
     # relied on dpkg to start the daemon; the reusable file does both, so a future packaging
     # change fails loudly instead of silently.
     ansible.builtin.import_role:
       name: local_worker_host
       tasks_from: container_runtime.yml
     vars:
       local_worker_host_operator_user: "{{ github_runner_user }}"
   ```

2. Regenerate `deploy/ansible/tests/fixtures/runner-tasks-2391.txt` with the harness's own
   extraction rather than by hand, so its `\tTAGS:` form, the `local_worker_host : ` →
   `live_vm_host : ` relabel, and the removal of the single `Read the runner system Python version
   used by uv` line before the Ubuntu guard all stay exact:

   ```sh
   cd /home/dave/src/kdive-worktrees/feat-enable-container-engine-daemon-2557
   ANSIBLE_CONFIG=deploy/ansible/ansible.cfg ANSIBLE_ROLES_PATH=deploy/ansible/roles \
     ansible-playbook deploy/ansible/playbooks/runner.yml -i localhost, --list-tasks \
     > /tmp/runner-listing.txt
   ```

   then apply `run-local-worker-host.py`'s own `tasks(..., include_tags=True)` filter and its
   `python_guard` removal to that file and write the result to the fixture.

3. Run `wc -l deploy/ansible/tests/fixtures/runner-tasks-2391.txt`. Expect `316`.

**Acceptance.** The fixture is 316 lines; the three new names appear consecutively where the removed
grant task was; no other line changed.

## Task 3 — Prove the gate, the target, and the order

Modifies `deploy/ansible/tests/run-local-worker-host.py`.

Where it fits: the check-mode proof. It cannot prove a daemon starts; the real-host run below does.

**Interfaces.** Consumes the task names, tag, register name, and defaults from Task 1 and the
fixture from Task 2. Uses this module's existing helpers, confirmed present with these signatures:
`playbook(path: Path, *args: str) -> subprocess.CompletedProcess[str]` (`:21`),
`require(condition: bool, message: str) -> None` (`:32`),
`container_section(output: str, name: str, context: str) -> str` (`:274`), and the module-level
names `ROOT` (`:13`), `probe` (`:145`), `listed` (`:148`), `operator` (`:184`), and the imports
`json` (`:4`) and `yaml` (`:10`).

**Verification.**
- Contract: both arms of the unit gate, for both mutating tasks. Mode: `focused-test` — new
  `container_daemon_gate`. Red before Task 1: `container_section` raises `SystemExit` because the
  enable heading is absent. Green: `just test-ansible`.
- Contract: the grant names only the operator variable and appends. Mode: `focused-test` — new
  `container_daemon_grant_target`. Red before Task 1: `FileNotFoundError` on the task file.
  Green: `just test-ansible`.
- Contract: probe, enable, grant, in order, after the compose-plugin install. Mode: `focused-test` —
  new `container_daemon_order`. Red before Task 1: the names are absent from `listed.stdout`.
  Green: `just test-ansible`.
- Contract: the updated exact-order count. Mode: `focused-test` — the edited literal at `:61`.

**Steps.**

1. Change the two `314` literals at `:61` and the `print` at `:64` to `316`.

2. Append, after the existing `container_runtime_order()` call at `:388-389`:

   ```python
   CONTAINER_TASKS = "deploy/ansible/roles/local_worker_host/tasks/container_runtime.yml"
   DAEMON_PROBE = "Look for a packaged container-engine service unit"
   DAEMON_ENABLE = "Enable and start the container-engine daemon"
   DAEMON_GRANT = "Add the local-worker operator to the container-engine socket group"


   def container_daemon_gate(*, unit_exists: bool) -> None:
       """Both mutating tasks follow the packaged-unit probe, never the runner's own state.

       The register is injected as an extra-var, which outranks it, so the skip arm proves the
       gate even on a runner that has docker installed (#2557, ADR-0663).

       Each task is read from its own invocation started at that task. In the entered arm the
       modules themselves run — check mode, non-root, against whatever systemd and group state the
       runner has — so an enable that fails there would otherwise stop the play before the grant is
       reached. The engine-install gate above starts at its probe for the same reason.
       """
       facts = {
           "ansible_facts": {
               "distribution": "Fedora",
               "distribution_version": "probe",
               "os_family": "RedHat",
           },
           "local_worker_host_operator_user": operator,
           "local_worker_host_engine_unit": {"stat": {"exists": unit_exists}},
       }
       state = f"unit present: {unit_exists}"
       for name in (DAEMON_ENABLE, DAEMON_GRANT):
           result = playbook(
               probe,
               "--check",
               "--tags",
               "container_runtime",
               "--start-at-task",
               name,
               "-e",
               json.dumps(facts),
           )
           section = container_section(result.stdout, name, state)
           skipped = "skipping: [localhost]" in section
           require(
               skipped != unit_exists,
               f"{state} got the wrong arm of {name!r}: skipped={skipped}",
           )


   for engine_unit_exists in (False, True):
       container_daemon_gate(unit_exists=engine_unit_exists)
   print("ok container daemon: the packaged-unit probe gates both the enable and the grant")


   def container_daemon_grant_target() -> None:
       """The socket group goes to the operator variable and to nothing else.

       Socket-group membership is root-equivalent and ADR-0575 keeps the fixed worker slot accounts
       out of it, so a grant naming live_vm_host_worker_accounts would dissolve that boundary
       silently. The enable is read here too: enabled without started leaves the defect in place.
       """
       source = (ROOT / CONTAINER_TASKS).read_text()
       require(
           "live_vm_host_worker_accounts" not in source,
           "the container-engine socket grant reaches the fixed worker accounts (ADR-0575)",
       )
       by_name = {task["name"]: task for task in yaml.safe_load(source)}
       grant = by_name[DAEMON_GRANT]["ansible.builtin.user"]
       require(
           grant["name"] == "{{ local_worker_host_operator_user }}",
           "the container-engine socket grant does not target the operator variable",
       )
       require(grant["append"] is True, "the container-engine socket grant replaces memberships")
       enable = by_name[DAEMON_ENABLE]["ansible.builtin.systemd_service"]
       require(
           enable["enabled"] is True and enable["state"] == "started",
           "the container-engine daemon task does not both enable and start the service",
       )


   container_daemon_grant_target()
   print("ok container daemon: the socket grant targets only the named operator account")


   def container_daemon_order() -> None:
       """Probe, enable, grant — all three after the compose plugin that resolves the engine."""
       ordered = (
           "Install the Tumbleweed compose plugin for the on-box stack and testcontainers",
           DAEMON_PROBE,
           DAEMON_ENABLE,
           DAEMON_GRANT,
       )
       positions = []
       for name in ordered:
           require(name in listed.stdout, f"container daemon task missing from listing: {name}")
           positions.append(listed.stdout.index(name))
       require(
           positions == sorted(positions),
           "container daemon tasks are out of order: packages, probe, enable, then grant",
       )


   container_daemon_order()
   print("ok container daemon: the enable and grant follow the engine install")
   ```

3. Run `just lint`, `just type`, then `just test-ansible`. Expect all three to exit 0 and
   `test-ansible` to print the three new `ok container daemon:` lines. That recipe deliberately
   exercises negative paths, so `[ERROR]` and `failed:` strings in its output are expected; judge
   by the exit code.

**Acceptance.** `just test-ansible` exits 0. Removing Task 1's `when:` from the grant task makes
`container_daemon_gate` fail; restoring it makes it pass.

## Final verification — the real-host proof

`just test-ansible` proves gating, target, and order in check mode. It cannot prove the daemon
starts, the socket is reachable without sudo, or that the play converges, and AGENTS.md makes that
proof the extender's job. Run it after Task 3 is green, against three operator-supplied hosts, and
report which arms ran. Never write a hostname into a committed file.

1. **Fedora — the changed path.** Return the host to the pre-fix state:
   `sudo systemctl disable --now docker.service docker.socket` and `sudo gpasswd -d <operator>
   docker`; confirm `systemctl is-enabled docker.service` is `disabled` and `id -nG <operator>`
   has no `docker`. Run the role with `local_worker_host_operator_user` set to that account. Expect
   the enable and grant tasks `changed`, then `systemctl is-enabled docker.service` `enabled`,
   `systemctl is-active docker.service` `active`, and, in a fresh session as the operator with no
   sudo, `docker info --format '{{.ServerVersion}}'` printing a version.
2. **Idempotence.** Re-run the play unchanged. Expect the enable and grant tasks `ok`, not
   `changed`.
3. **Ubuntu — the no-op path.** Run the role against a host where `docker.service` is already
   preset-enabled and running. Expect the enable task `ok` on the first run.
4. **Rocky — the skip arm.** Run the role against a host with no engine package. Expect both
   mutating tasks `skipping` and the play to succeed.
