# External-boot authority host implementation plan

Goal: install and prove the ADR-0584 provider-host authority boundary without enabling provider
capability advertisement. The existing provider-neutral service and database roles remain the
authority semantics; this change adds the host runtime, systemd/Ansible ownership boundary,
readiness checks, and deployment/adversarial proof.

Tech stack: Python 3.14, asyncio/psycopg, systemd, Ansible, pytest.

## Global constraints

- Target architectures: x86_64 and ppc64le.
- Python 3.14 under `uv`; no new dependency.
- Provider adapters and capability advertisement remain owned by #2140.
- Lifecycle orchestration remains owned by #2118.
- Never expose credentials, DSNs, provider output, or journal bytes in diagnostics.
- Guardrails: focused tests while iterating; `just lint`; whole-tree `just type`; relevant
  `prek` hooks; `just ci` before delivery.

Expected implementation size: 450–750 changed lines (L) — derived from one runtime module, CLI
wiring, two systemd artifacts, Ansible provisioning/verification, tests, and runbook diagnostics.

## Task 1: Add the fail-closed host readiness runtime

Files:

- Create `src/kdive/providers/external_boot_authority/host.py`.
- Modify `src/kdive/__main__.py`.
- Create `tests/providers/external_boot_authority/test_host.py`.

Interfaces:

- `AuthorityHostConfig.from_environment() -> AuthorityHostConfig` consumes fixed `KDIVE_*`
  configuration and systemd credential-directory paths.
- `check_authority_host(config: AuthorityHostConfig) -> Awaitable[None]` validates identity,
  protected paths, journal restoration, database role shape, and provider socket access.
- `run_authority_host(config: AuthorityHostConfig) -> Awaitable[None]` performs the same check,
  reports readiness through process state, and remains supervised.
- CLI handlers expose `external-boot-authority-host` and
  `check-external-boot-authority-host`; Task 3's unit and Ansible probe rely on those names.

Verification:

- Mode: focused-test. Contract: credential and journal paths must be regular, authority-owned,
  non-symlinked, and exact-mode. Cases: `test_host_rejects_unsafe_credentials` and
  `test_host_rejects_invalid_journal_tree`; red observation is missing module/import; green command
  is `uv run python -m pytest tests/providers/external_boot_authority/test_host.py -q`.
- Mode: focused-test. Contract: role shape and diagnostics fail closed without secret values.
  Cases: `test_host_rejects_privileged_database_role` and
  `test_host_diagnostics_are_bounded_and_secret_free`; same focused green command.

Steps:

1. Add the focused tests and run the command; expect collection/import failure.
2. Implement the immutable config and filesystem checks using `os.open`/`stat` without following
   symlinks; run the focused command and expect filesystem cases green.
3. Implement the async psycopg role/function checks and bounded error type; run the focused command
   and expect all host tests green.
4. Wire the two CLI commands and add parser tests; run the focused command plus the existing CLI
   parser tests and expect green.
5. Commit as `feat(authority): add fail-closed host readiness`.

Acceptance: no caller-selected path or provider definition crosses the boundary; a failed check
exits non-zero with only component/reason; restart repeats all checks.

## Task 2: Split provider observation from mutation authority

Files:

- Modify `deploy/systemd/libvirtd-live.conf`.
- Modify `deploy/systemd/system/kdive-live-worker@.service`.
- Modify `deploy/ansible/roles/live_vm_host/defaults/main.yml`.
- Modify `deploy/ansible/roles/live_vm_host/tasks/main.yml`.
- Modify `deploy/ansible/roles/live_vm_host/tasks/verify.yml`.
- Modify `tests/deploy/test_live_worker_provisioning.py` and `tests/deploy/test_systemd_units.py`.

Interfaces:

- `live_vm_host_worker_observe_group` names the read-only libvirt group.
- `live_vm_host_authority_account` and `live_vm_host_authority_group` name the sole mutation
  principal.
- `live_vm_host_worker_libvirt_uri` points to `libvirt-sock-ro`; Task 3's authority config points
  separately to `libvirt-sock`.

Verification:

- Mode: focused-test. Contract: fixed workers have the observation group, no authority group, no
  `kvm`, and only the read-only socket URI. Cases in `test_live_worker_provisioning.py` and
  `test_systemd_units.py`; red observation is assertions matching the old mutation group/URI;
  green command is `uv run python -m pytest tests/deploy/test_live_worker_provisioning.py tests/deploy/test_systemd_units.py -q`.
- Mode: focused-test. Contract: libvirtd creates distinct RO and RW sockets with disjoint groups.
  Case `test_libvirt_configuration_splits_observation_and_mutation`; same focused green command.

Steps:

1. Change structural tests to require the new groups, sockets, and unit membership; run the focused
   command and expect failures against the old configuration.
2. Update libvirtd configuration, defaults, account/group tasks, worker unit, and URI publication.
3. Replace worker mutation-access verification with positive read-only observation and negative
   mutation/group/path probes; run the focused command and expect green.
4. Commit as `feat(deploy): isolate authority provider access`.

Acceptance: fixed workers and the reconciler cannot reach the authority mutation socket,
credential, helper, journal, or runtime paths while worker observation remains usable.

## Task 3: Install and supervise the authority service

Files:

- Create `deploy/systemd/system/kdive-external-boot-authority.service`.
- Create `deploy/systemd/provider-authority.env.example`.
- Modify the live-VM host Ansible defaults, tasks, and verification files.
- Modify `tests/deploy/test_systemd_units.py` and `tests/deploy/test_live_worker_provisioning.py`.

Interfaces:

- The systemd unit runs `/opt/kdive-provider-authority/.venv/bin/python -m kdive
  external-boot-authority-host`, loads `database-dsn` and `service-credential` through
  `LoadCredential=`, uses `User=kdive-provider-authority`, and hardens filesystem/network access.
- Ansible installs the venv, root/authority-owned credentials, journal and runtime directories,
  unit, configuration, and readiness probe in clean-host order.

Verification:

- Mode: focused-test. Contract: the unit has retry, credential, identity, and hardening directives.
  Case `test_external_boot_authority_unit_is_isolated_and_supervised`; red observation is missing
  unit; green command is `uv run python -m pytest tests/deploy/test_systemd_units.py -q`.
- Mode: focused-test. Contract: Ansible creates paths before start, starts the service, and runs
  every positive and negative readiness proof. Case
  `test_ansible_installs_authority_in_clean_host_order`; red observation is missing declarations;
  green command is `uv run python -m pytest tests/deploy/test_live_worker_provisioning.py -q`.

Steps:

1. Add the unit and provisioning structural tests and confirm the expected red failures.
2. Add the systemd unit and example configuration.
3. Add authority identity, directories, credentials, venv installation, unit install/start, and
   readiness verification to Ansible in dependency order.
4. Run both focused deployment files and `just lint-ansible`; expect green.
5. Commit as `feat(deploy): supervise external boot authority`.

Acceptance: clean provisioning fails before completion if service recovery, database least
privilege, provider mutation access, or worker/reconciler denial is unproven.

## Task 4: Close adversarial and operator evidence

Files:

- Modify `tests/adversarial/test_external_boot_authority_journal.py`.
- Modify `docs/operating/runbooks/self-hosted-kvm-runner.md` and
  `docs/operating/runbooks/live-testing.md` only where each owns deployment diagnosis.

Interfaces:

- Provider-neutral tests use existing `_service`, controllable adapter, and repository doubles; no
  provider adapter is added.
- Runbooks name the one-shot readiness command, bounded failure components, journal restoration,
  group/socket inspection, and the explicit capability-advertisement hold.

Verification:

- Mode: focused-test. Contract: unresolved calls, takeover, restart, valid-prefix journal loss,
  independently anchored heads, and stale provider/core writes remain fenced. Add explicit
  adversarial cases and run `uv run python -m pytest tests/adversarial/test_external_boot_authority_journal.py -q`; expect the new assertions to fail before supporting fixes, if any, and all cases green afterward.
- Mode: task-test-not-applicable. Surface: human-readable diagnostic/runbook prose. Reason: no
  executable consumer validates prose semantics; doc-link and style guardrails validate structure
  but cannot meaningfully prove the operational instructions.

Steps:

1. Add only missing provider-neutral adversarial scenarios and confirm each controlled fault makes
   its new assertion fail before restoring the fault.
2. Update the two owning runbooks with bring-up, readiness, diagnosis, retained evidence, and the
   #2140 advertisement hold.
3. Run the focused adversarial file and relevant doc guards; expect green.
4. Commit as `test(authority): prove deployed authority boundaries`.

Acceptance: every issue criterion maps to an executable or deployment proof; no live provider
adapter or lifecycle orchestration enters the diff.

## Final verification

Run `just lint`, `just type`, focused deployment/provider/adversarial tests, `prek run` on the
staged file set, and `just ci`. All must exit zero before delivery. The rollback is a git revert;
retain authority journals and credentials for operator-controlled cleanup.
