# Container-engine daemon enablement — implementation plan

Goal: make `local_worker_host` leave a provisioned host with the container-engine daemon enabled and
running and the named operator account in the engine's socket group, and route the Debian-family
runner through the same tasks.

Architecture: one new reusable Ansible task file in `local_worker_host`, imported from that role's
`main.yml` and from `live_vm_host/tasks/main.yml` with the runner account substituted for the
operator variable. Three new role defaults name the unit path, the service and the socket group. The
shared check-mode harness `deploy/ansible/tests/run-local-worker-host.py` gains gate, target,
binding, resolution and ordering assertions, and its exact-order runner fixture is regenerated.

Tech stack: Ansible (`ansible.builtin` modules, `profile: production` ansible-lint), Python 3.14 for
the harness, `just` recipes for guardrails.

Expected implementation size: 160–220 changed lines (M) — from the file map below: one new ~35-line
task file, ~12 lines of defaults, ~10 lines across the two `main.yml` files, 4 fixture lines,
~105 lines of harness tests, ~20 lines of corrected prose. ADR-0663 is excluded: it is already
committed.

Design: [spec](../specs/2026-09-16-container-engine-daemon-enablement-design.md),
[ADR-0663](../../adr/0663-provisioning-enables-the-container-engine-daemon.md).

## Global Constraints

- Ansible module names are fully qualified (`ansible.builtin.*`). ansible-lint runs from
  `deploy/ansible/.ansible-lint` (`profile: production`, `offline: true`), invoked by `justfile:453`
  as `-c deploy/ansible/.ansible-lint`. Task names start with a capital.
- Role variables carry the `local_worker_host_` prefix or an explicit
  `# noqa: var-naming[no-role-prefix]`.
- `deploy/ansible/tests/run-local-worker-host.py:159-168` forbids the strings `Docker Engine`,
  `runner service account`, `fixture catalog`, and `authority service` in any standalone-role task
  name. New task names must contain none of them.
- Ruff line length 100; `just lint` formats Python inside Markdown fences too.
- Prose rule: no "critical", "robust", "comprehensive", "elegant"; "Milestone", never "Sprint".
- Never write a hostname, IP, username, or internal domain into a committed file. Use the session
  scratchpad, not `/tmp`, for intermediate files.
- Guardrails: `just lint`, `just type`, `just lint-ansible`, `just test-ansible`, and
  `just ci > <file> 2>&1 < /dev/null` before push. `git fetch origin main && just records` gates
  ADR-0663, which is already committed; re-run it after any base refresh.
- `deploy/ansible/tests/fixtures/runner-tasks-2391.txt` is 314 lines on the current base and issue
  #2567 also edits it. Never hand-edit it and never hardcode its new length: regenerate it, and
  regenerate it again after the final base refresh before push.

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
  `Look up the container-engine socket grant's target account`,
  `Require the container-engine socket grant to name a non-worker account`,
  `Enable and start the container-engine daemon`,
  `Add the named operator account to the container-engine socket group`
- registered variable `local_worker_host_engine_operator_getent`
- tag `container_runtime` on all five.

**Verification.**
- Contract: the three tasks parse, carry the tag, and gate on the probe. Mode: `focused-test` —
  Task 3's `container_daemon_gate`; this task's local green command is `just lint-ansible`,
  expected exit 0 with no findings.
- Contract: every `{{ ... }}` name in the task file resolves in the role defaults. Mode:
  `focused-test` — Task 3's `container_daemon_grant_target`; local green is `just lint-ansible`.
- Contract: the corrected prose. Mode: `task-test-not-applicable` — prose has no executable
  consumer and a wording snapshot would assert only that the words did not change. `just
  docs-links` covers its link mechanics.

**Steps.**

1. Append to `local_worker_host/defaults/main.yml`, after
   `local_worker_host_engine_packages_tumbleweed`:

   ```yaml
   # The engine's systemd identity, named once so tasks and tests share the literals. The probe path
   # is where every family that packages an engine installs the unit — verified on Fedora 44
   # (moby-engine), Ubuntu 26.04 (docker.io) and Tumbleweed (docker) — and where a docker-ce install
   # from Docker's own repository puts it too (ADR-0663).
   local_worker_host_engine_service: docker.service
   local_worker_host_engine_unit_path: /usr/lib/systemd/system/docker.service
   # The socket unit's SocketGroup on all three: its socket is root:docker 0660, so a non-root
   # operator outside this group cannot reach the daemon and stack-services.sh fails (#2557).
   local_worker_host_engine_socket_group: docker
   ```

2. Create `local_worker_host/tasks/container_runtime.yml`:

   ```yaml
   ---
   # Declaring the engine packages is not a working runtime (ADR-0663, #2557). Fedora's preset
   # enables docker.socket and leaves docker.service disabled; the sysusers entry creates the docker
   # group empty. Debian reaches the working end state only through dpkg install-time policy, which
   # is packaging behaviour this repository does not control.
   - name: Look for a packaged container-engine service unit
     ansible.builtin.stat:
       path: "{{ local_worker_host_engine_unit_path }}"
     register: local_worker_host_engine_unit
     tags: [container_runtime]

   - name: Look up the container-engine socket grant's target account
     ansible.builtin.getent:
       database: passwd
       key: "{{ local_worker_host_operator_user }}"
       fail_key: false
     when:
       - local_worker_host_engine_unit.stat.exists
       - local_worker_host_operator_user | length > 0
     register: local_worker_host_engine_operator_getent
     tags: [container_runtime]

   - name: Require the container-engine socket grant to name a non-worker account
     # Socket-group membership is root-equivalent and ADR-0575 keeps the fixed worker slot accounts
     # out of Docker groups. Its runtime verifier covers the runner path only, and a caller using
     # `tasks_from` skips preflight.yml entirely — so the check lives here. The existence clause is
     # load-bearing: the grant is ansible.builtin.user, whose default state is `present`, so an
     # absent name would otherwise be created and handed the socket group.
     ansible.builtin.assert:
       that:
         - local_worker_host_operator_user | length > 0
         - local_worker_host_operator_user not in live_vm_host_worker_accounts
         - >-
           (local_worker_host_engine_operator_getent.ansible_facts | default({}, true))
           .get('getent_passwd', {}).get(local_worker_host_operator_user) is not none
       fail_msg: >-
         local_worker_host_operator_user={{ local_worker_host_operator_user }} must be an existing
         host account outside live_vm_host_worker_accounts.
     when: local_worker_host_engine_unit.stat.exists
     tags: [container_runtime]

   - name: Enable and start the container-engine daemon
     # The service, not the socket: docker.service carries Requires=docker.socket and
     # ExecStart=/usr/bin/dockerd -H fd://, so enabling the service pulls the socket in and the play
     # witnesses the daemon's first start. Tumbleweed's own docker.socket says the same with
     # BindsTo=docker.service. A host with the unit but no engine from this role — Enterprise Linux
     # or SLES on the Docker-repository remedy the operator guide recommends — is enabled too. A
     # host without it (podman-docker owns /usr/bin/docker but ships no unit) is skipped, not
     # failed: packages_redhat.yml:31-33 deliberately leaves such a host the provider it chose.
     ansible.builtin.systemd_service:
       name: "{{ local_worker_host_engine_service }}"
       enabled: true
       state: started
     when: local_worker_host_engine_unit.stat.exists
     tags: [container_runtime]

   - name: Add the named operator account to the container-engine socket group
     # ADR-0575 keeps the fixed worker slot accounts out of Docker groups; this grant is for the
     # caller's single operator account, a different principal. append: true so no existing
     # membership is removed. The task names the variable, not a role, because live_vm_host rebinds
     # it to the runner account.
     ansible.builtin.user:
       name: "{{ local_worker_host_operator_user }}"
       groups: "{{ local_worker_host_engine_socket_group }}"
       append: true
     when: local_worker_host_engine_unit.stat.exists
     tags: [container_runtime]
   ```

3. Append to the end of `local_worker_host/tasks/main.yml`, after `Prepare shared provider data
   paths` — the fixed-worker contract does not depend on the engine, so an engine that cannot start
   must not cost the operator all worker provisioning:

   ```yaml
   - name: Enable the container engine and grant the operator its socket
     ansible.builtin.import_tasks: container_runtime.yml
   ```

4. Replace the last paragraph of the `local_worker_host_compose_packages_fedora` comment block
   (`defaults/main.yml:96-99`) with:

   ```yaml
   # Fedora's RPM leaves docker.service disabled — its preset enables only docker.socket — and its
   # sysusers entry creates the docker group empty. tasks/container_runtime.yml enables and starts
   # the service and adds the caller's operator account to that group (ADR-0663, #2557), so the
   # packages below are a working runtime rather than a declared one.
   ```

   and replace `defaults/main.yml:139` (`# The same daemon-enable and socket-access caveat
   applies.`) with:

   ```yaml
   # tasks/container_runtime.yml enables the daemon and grants the socket group here too.
   ```

5. In `docs/operating/providers/local-libvirt.md`, replace the last two sentences of the
   "Container engine" bullet — from `On Fedora and openSUSE the packages alone` through
   `without both it fails on the socket.` — with:

   ```markdown
   `stack-services.sh` refuses to run as root, so the operator needs the socket, and the packages
   alone do not give it: the RPM leaves `docker.service` disabled and creates the `docker` group
   empty. `just prepare-local-libvirt-host` now enables and starts `docker.service` and adds the
   operator account to the `docker` group
   ([ADR-0663](../../adr/0663-provisioning-enables-the-container-engine-daemon.md)). It does that on
   any host carrying `/usr/lib/systemd/system/docker.service`, which includes an Enterprise Linux or
   SLES host that took the Docker-repository remedy above — this repository still installs no engine
   there, it only makes one you installed usable. A `podman-docker` host has no such unit, so both
   steps skip and its socket path stays yours. The new group does not reach a login session that
   already existed, so start a fresh one before running `stack-services.sh`.
   ```

6. Run `just lint-ansible` and `just docs-links`. Expect both to exit 0.

**Acceptance.** The five task names appear, in order, in
`ansible-playbook deploy/ansible/tests/local_worker_host.yml -i localhost, --list-tasks` after
`Install the Tumbleweed compose plugin for the on-box stack and testcontainers`.
`rg -n 'manual step|operator step|remain operator steps' deploy/ docs/operating/ scripts/` returns
no hit describing the enable or the group grant. (`docs/operating/install.md:190` describes adding
a developer's own user to `docker` for `just setup` on a workstation, which this role does not
provision; leave it.) `just lint-ansible` exits 0.

## Task 2 — Route the runner through the same tasks

Modifies `deploy/ansible/roles/live_vm_host/tasks/main.yml`,
`deploy/ansible/tests/fixtures/runner-tasks-2391.txt`, and
`deploy/ansible/tests/run-local-worker-host.py` (the baseline count only).

Where it fits: closes #2557's "the Debian path should stop depending on dpkg policy", and removes
the duplicated grant policy.

**Interfaces.** Consumes the task file and defaults from Task 1, and `github_runner_user`
(existing, `live_vm_host/defaults/main.yml`). Produces a runner `--list-tasks` output five entries
longer and one entry shorter than the current baseline — net +4 — and updates
`run-local-worker-host.py`'s baseline literal to match, so the count contract stays green at the end
of this task rather than at the end of Task 3.

**Verification.**
- Contract: the runner play lists the four new tasks in order and its exact-order baseline matches.
  Mode: `focused-test` — `run-local-worker-host.py:61` and its fixture. Red: after step 1 and before
  step 2, the run fails with `runner listed 318 baseline tasks, expected 314`. Green:
  `just test-ansible`.

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
     # relied on dpkg to start the daemon; the reusable file does both, so a future packaging change
     # fails loudly instead of silently. The apt install above is unconditional and Debian's
     # docker.io ships the unit, so the probe holds here; the runner real-host arm proves it.
     ansible.builtin.import_role:
       name: local_worker_host
       tasks_from: container_runtime.yml
     vars:
       local_worker_host_operator_user: "{{ github_runner_user }}"
   ```

2. Regenerate the fixture with the harness's own filter, so its `\tTAGS:` form, the
   `local_worker_host : ` → `live_vm_host : ` relabel and the single system-Python-probe removal stay
   exact. `run-local-worker-host.py` cannot be imported for this — its filename is hyphenated and
   lines 46-64 run the whole check at module scope — so the filter is re-stated inline. Dedent the
   block before running it: its heredoc terminator must sit at column 0. From the worktree root:

   ```sh
   ANSIBLE_CONFIG=deploy/ansible/ansible.cfg ANSIBLE_ROLES_PATH=deploy/ansible/roles \
     ansible-playbook deploy/ansible/playbooks/runner.yml -i localhost, --list-tasks \
     > "${TMPDIR:-/tmp}/runner-listing.txt"
   python3 - "${TMPDIR:-/tmp}/runner-listing.txt" <<'PY'
   import sys
   from pathlib import Path

   lines = [
       line.strip().replace("local_worker_host : ", "live_vm_host : ")
       for line in Path(sys.argv[1]).read_text().splitlines()
       if "\tTAGS:" in line and not line.lstrip().startswith("play #")
   ]
   g = next(i for i, t in enumerate(lines) if "Assert the runner host is Ubuntu/Debian" in t)
   probe = "live_vm_host : Read the runner system Python version used by uv\tTAGS: []"
   assert lines[g - 1] == probe, lines[g - 1]
   del lines[g - 1]
   Path("deploy/ansible/tests/fixtures/runner-tasks-2391.txt").write_text("\n".join(lines) + "\n")
   print(len(lines))
   PY
   ```

   Expect `318` on the current base. Whatever it prints is the count — if #2567 has landed it will
   differ, and the literal below follows the regeneration rather than the reverse.

3. Set the two `314` literals at `run-local-worker-host.py:61` and the `print` at `:64` to the count
   step 2 printed.

4. Run `just test-ansible`. Expect exit 0.

**Acceptance.** `wc -l` on the fixture equals the count in `run-local-worker-host.py:61`; the four
new task names appear consecutively where the removed grant task was; no other fixture line changed.
`just test-ansible` exits 0.

## Task 3 — Prove the gate, the target, the binding and the order

Modifies `deploy/ansible/tests/run-local-worker-host.py`.

Where it fits: the check-mode proof. It cannot prove a daemon starts; the real-host run below does.

**Interfaces.** Consumes the task names, tag, register name, defaults and call-site binding from
Tasks 1 and 2. Uses this module's existing helpers, confirmed present with these signatures:
`playbook(path: Path, *args: str) -> subprocess.CompletedProcess[str]` (`:21`),
`require(condition: bool, message: str) -> None` (`:32`),
`container_section(output: str, name: str, context: str) -> str` (`:274`), and the module-level
names `ROOT` (`:13`), `ANSIBLE` (`:14`), `probe` (`:145`), `listed` (`:148`), `defaults` (`:66`,
the role defaults already parsed), `operator` (`:184`), and the imports `json` (`:4`) and `yaml`
(`:10`). Adds `re` to the imports.

Write each function in the style of the module's existing `container_runtime_gate` — a docstring
saying what the arm proves and why the injection is needed, `require` for every assertion, one
`print("ok container daemon: ...")` per function after its call.

**Verification.**
- Contract: both arms of the unit gate, for both mutating tasks. Mode: `focused-test` —
  `container_daemon_gate`. Red before Task 1: `container_section` raises `SystemExit` because the
  enable heading is absent. Green: `just test-ansible`.
- Contract: the grant targets the operator variable, references no worker account, resolves every
  name it uses, and is rebound at the runner call site. Mode: `focused-test` —
  `container_daemon_grant_target`. Red before Task 1: `FileNotFoundError` on the task file.
- Contract: probe, enable, grant, in order, after the compose-plugin install. Mode:
  `focused-test` — `container_daemon_order`. Red before Task 1: names absent from `listed.stdout`.

**Steps.**

1. Add `import re` to the imports, in alphabetical position.

2. Add the three task-name constants and a shared `container_daemon_facts(*, unit_exists: bool) ->
   dict[str, object]` helper returning the extra-vars payload: `ansible_facts` with
   `distribution: "Fedora"`, `distribution_version: "probe"`, `os_family: "RedHat"`;
   `local_worker_host_operator_user: operator`; and `local_worker_host_engine_unit: {"stat":
   {"exists": unit_exists}}`. The register is injected as an extra-var because extra-vars outrank
   it, so both arms are drivable on a runner whatever its own docker state — the same reason
   `container_runtime_gate` injects `local_worker_host_docker_provider_redhat`.

   ```python
   CONTAINER_TASKS = "roles/local_worker_host/tasks/container_runtime.yml"
   DAEMON_PROBE = "Look for a packaged container-engine service unit"
   DAEMON_ENABLE = "Enable and start the container-engine daemon"
   DAEMON_GRANT = "Add the named operator account to the container-engine socket group"
   ```

3. `container_daemon_gate(*, unit_exists: bool)` — for each of `DAEMON_ENABLE` and `DAEMON_GRANT`,
   run `playbook(probe, "--check", "--tags", "container_runtime", "--start-at-task", name, "-e",
   json.dumps(facts))`, take `container_section(result.stdout, name, state)`, and `require` that
   `("skipping: [localhost]" in section) != unit_exists`. One invocation per task, each started at
   that task: in the entered arm the modules themselves run, check mode and non-root, so an enable
   that failed there would otherwise stop the play before the grant is reached. Call it for
   `unit_exists` in `(False, True)`. The `unit_exists=False` arm is the `podman-docker` case — a
   Fedora host whose engine install `packages_redhat.yml:31-33` skipped — and it must be a clean
   skip, never a failure.

4. `container_daemon_grant_target()` — read `(ANSIBLE / CONTAINER_TASKS).read_text()` and `require`:

   - the guard task's `that:` list carries all three clauses — the non-empty check, the
     `not in live_vm_host_worker_accounts` exclusion, and the `getent_passwd` existence check.
     The task file names the worker-account list precisely in order to exclude it, so assert that
     clause is PRESENT; asserting its absence would require deleting the guard;
   - on the `yaml.safe_load`ed task whose `name` is `DAEMON_GRANT`, that
     `["ansible.builtin.user"]["name"] == "{{ local_worker_host_operator_user }}"` and
     `["append"] is True`;
   - on the `DAEMON_ENABLE` task, that `["ansible.builtin.systemd_service"]` has `enabled is True`
     and `state == "started"` — enabled without started leaves the defect in place;
   - that `set(re.findall(r"\{\{\s*(local_worker_host_[a-z_]+)", source))` is a subset of
     `set(defaults) | {"local_worker_host_engine_unit"}`, so a typo in any new default fails here
     rather than on a host;
   - on `yaml.safe_load` of `ANSIBLE / "roles/live_vm_host/tasks/main.yml"`, that the task whose
     `["ansible.builtin.import_role"]["tasks_from"] == "container_runtime.yml"` carries
     `vars["local_worker_host_operator_user"] == "{{ github_runner_user }}"`. That call site is the
     only place the
     operator variable is rebound, and `import_role` with `tasks_from` does not run `preflight.yml`,
     so a lost `vars` key would fall back to the role default of `""` rather than to a refusal.

5. `container_daemon_order()` — mirror the existing `container_runtime_order` exactly: collect
   `listed.stdout.index(name)` for `("Install the Tumbleweed compose plugin for the on-box stack and
   testcontainers", DAEMON_PROBE, DAEMON_ENABLE, DAEMON_GRANT)`, `require` each name is present, and
   `require(positions == sorted(positions))`.

6. Run `just lint`, `just type`, then `just test-ansible`. Expect all three to exit 0 and
   `test-ansible` to print the three new `ok container daemon:` lines. That recipe deliberately
   exercises negative paths, so `[ERROR]` and `failed:` strings in its output are expected; judge by
   the exit code.

**Acceptance.** `just test-ansible` exits 0. Removing Task 1's `when:` from the grant task makes
`container_daemon_gate` fail; restoring it makes it pass.

## Final verification — the real-host proof

`just test-ansible` proves gating, the grant target, the call-site binding and ordering in check
mode. It cannot prove the daemon starts, the socket is reachable without sudo, or that the
play converges, and AGENTS.md makes that proof the extender's job. Run it after Task 3 is green
against the operator-supplied hosts, and report which arms ran. Never write a hostname into a
committed file.

1. **Fedora — the changed path.** Run this arm only on a host the operator can afford to leave
   disabled. Return it to the pre-fix state: `sudo systemctl disable --now docker.service
   docker.socket` and `sudo gpasswd -d <operator> docker`; confirm `systemctl is-enabled
   docker.service` is `disabled` and `id -nG <operator>` has no `docker`. Run the role with
   `local_worker_host_operator_user` set to that account. Expect the enable and grant tasks
   `changed`, then `systemctl is-enabled docker.service` `enabled`, `systemctl is-active
   docker.service` `active`, and, in a fresh session as the operator with no sudo, `docker info
   --format '{{.ServerVersion}}'` printing a version. **If the play fails, restore by hand before
   moving on:** `sudo systemctl enable --now docker.service` and `sudo gpasswd -a <operator>
   docker`.
2. **Idempotence.** Re-run the play unchanged. Expect the enable and grant tasks `ok`, not
   `changed`.
3. **Ubuntu — the no-op path, through the runner play.** Run
   `deploy/ansible/playbooks/runner.yml --tags container_runtime` (non-check) against a host where
   `docker.service` is already preset-enabled and running. This is the arm that exercises the
   call-site rebinding and the replaced grant, so record `id -nG <runner account>` containing
   `docker` and `systemctl is-active docker.service` `active`. Expect every task `ok` and
   `changed=0`. A full non-check `runner.yml` run reprovisions the whole runner and is not
   performed here; say so rather than claiming it.

5. **The guard's refusals.** On the Fedora host, re-run the standalone task file with
   `local_worker_host_operator_user` set to a fixed worker slot account, to an empty string, and to
   a name the host does not have. Each must fail at the guard before the enable task is reached,
   and the absent name must not be created — check `id <name>` afterwards.
4. **Rocky — the skip arm.** Run the standalone role against a host with no engine package. Expect
   both mutating tasks to report `skipping` and the play to succeed.
6. **Tumbleweed.** No host exists in the project's test-host set. Its three literals were verified
   from the `docker-29.7.2_ce-41.1` package in an `opensuse/tumbleweed` container image; the play
   itself ships unrun there. Report that as the residual rather than claiming the arm.
