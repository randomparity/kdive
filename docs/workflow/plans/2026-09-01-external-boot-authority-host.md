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
- Migration `0125` is the only assigned migration and supplies the bounded trusted-head inventory.
- Preserve current fixed-worker provider/KVM access until #2140 replaces it atomically.
- Lifecycle orchestration remains owned by #2118.
- Never expose credentials, DSNs, provider output, or journal bytes in diagnostics.
- Guardrails: focused tests while iterating; `just lint`; whole-tree `just type`; relevant
  `prek` hooks; `just ci` before delivery.

Expected implementation size: 650–950 changed lines (L) — derived from migration 0125, one runtime
module, CLI wiring, systemd artifacts, Ansible provisioning/verification, tests, and diagnostics.

## Task 1: Add the least-privilege trusted-head inventory

Files:

- Create `src/kdive/db/schema/0125_external_boot_authority_head_inventory.sql`.
- Modify `src/kdive/db/external_boot_authority_journal.py`.
- Create `tests/db/test_external_boot_authority_head_inventory_migration.py` and update exact
  migration-tail assertions generated from the schema inventory.

Interfaces:

- SQL `list_external_boot_authority_journal_heads(text)` returns only authority instance, System,
  sequence, digest, phase, authority, generation, and operation identity for the supplied instance.
- `list_journal_heads(conn, authority_instance: str) -> tuple[JournalHead, ...]` is Task 2's exact
  inventory input.

Verification:

- Mode: focused-test. Contract: only `kdive_provider_authority` can execute the security-definer
  inventory, runtime roles have no table access, results are instance-scoped and bounded, and no
  lifecycle write is possible. Red observation: migration 0125 is absent. Green command:
  `uv run python -m pytest tests/db/test_external_boot_authority_head_inventory_migration.py -q`.

Steps:

1. Add migration privilege, isolation, empty, multi-instance, and malformed-input tests and confirm
   they fail because 0125 is absent.
2. Add migration 0125 and the typed repository reader; run the focused command and expect green.
3. Regenerate/update migration inventory assertions and run the migration-order guard.
4. Commit as `feat(db): expose bounded authority head inventory`.

Acceptance: the authority can discover a trusted head whose local lane is absent without direct
table access or unrelated tenant/lifecycle data.

## Task 2: Add the fail-closed host readiness runtime

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
  repeats it at the configured bounded interval, and exits on drift.
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
- Mode: focused-test. Contract: the database inventory and local lanes are a bijection, exact heads
  match, and periodic socket/ACL/role/journal drift exits the service. Cases:
  `test_host_rejects_missing_or_extra_lane` and `test_host_exits_when_boundary_drifts`; same command.

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
exits non-zero with only component/reason; startup and periodic checks detect complete-lane loss and
post-start drift.

## Task 3: Provision a distinct dormant authority endpoint

Files:

- Create `deploy/systemd/libvirtd-external-boot-authority.conf`.
- Modify `deploy/ansible/roles/live_vm_host/defaults/main.yml`.
- Modify `deploy/ansible/roles/live_vm_host/tasks/main.yml`.
- Modify `deploy/ansible/roles/live_vm_host/tasks/verify.yml`.
- Modify `tests/deploy/test_live_worker_provisioning.py`.

Interfaces:

- `live_vm_host_authority_account` names the owner of a separate session libvirtd under
  `/run/kdive/provider-authority/libvirt`.
- Existing worker accounts, groups, unit, URI, and KVM access remain byte-for-byte unchanged.
- Task 4's authority service config points only to the distinct authority socket.

Verification:

- Mode: focused-test. Contract: Ansible creates a distinct authority Unix identity/session/socket,
  workers and reconciler cannot traverse it, and current worker/KVM configuration is unchanged.
  Cases `test_authority_endpoint_is_a_distinct_session` and
  `test_existing_worker_provider_contract_is_preserved`; red observation is missing authority
  endpoint; green command is
  `uv run python -m pytest tests/deploy/test_live_worker_provisioning.py -q`.

Steps:

1. Add structural tests for the separate authority account/session and unchanged worker contract;
   run the focused command and expect the missing-endpoint failures.
2. Add authority libvirtd configuration, defaults, account, protected paths, and user unit without
   editing the existing worker unit/configuration.
3. Add negative worker/reconciler traversal probes and positive unchanged-worker probes; run the
   focused command and expect green.
4. Commit as `feat(deploy): provision dormant authority endpoint`.

Acceptance: fixed workers and the reconciler cannot reach the distinct authority mutation socket,
credential, helper, journal, runtime, or objects; their current provider workload remains usable.

## Task 4: Install and supervise the authority service

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
- Mode: focused-test. Contract: the authorized clean Ubuntu carrier executes provisioning from a
  clean KDIVE state, starts/restarts both services, verifies access denial, and injects drift that
  retracts readiness. Carrier: `dave@ub26-big.dev.pdx.drc.nz`; expected green command is the scoped
  playbook/proof command recorded in the runbook and first-run evidence.

Steps:

1. Add the unit and provisioning structural tests and confirm the expected red failures.
2. Add the systemd unit and example configuration.
3. Add authority identity, directories, credentials, venv installation, unit install/start, and
   readiness verification to Ansible in dependency order.
4. Run both focused deployment files and `just lint-ansible`; expect green.
5. Resolve exact pre-existing remote targets read-only, execute clean-host provisioning and every
   positive/negative/drift proof, then clean only proof-owned KDIVE development artifacts.
6. Commit as `feat(deploy): supervise external boot authority`.

Acceptance: clean provisioning fails before completion if service recovery, database least
privilege, provider mutation access, or worker/reconciler denial is unproven.

## Task 5: Close adversarial and operator evidence

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
