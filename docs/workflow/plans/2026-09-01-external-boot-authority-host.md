# External-boot authority host implementation plan

Goal: install and prove the ADR-0584 provider-host authority boundary without enabling provider
capability advertisement. The existing provider-neutral service and database roles remain the
authority semantics; this change adds the dormant authenticated request host, systemd/Ansible
ownership boundary, readiness checks, and deployment/adversarial proof.

Tech stack: Python 3.14, asyncio/psycopg, systemd, Ansible, pytest.

## Global constraints

- Target architectures: x86_64 and ppc64le.
- Python 3.14 under `uv`; no new dependency.
- Provider adapters and capability advertisement remain owned by #2140.
- Migration `0125` is the only assigned migration and supplies the bounded trusted-head inventory.
- Preserve current fixed-worker provider/KVM access until #2140 replaces it atomically.
- Host bounded TLS 1.3 mutual authentication over an ACL-isolated Unix stream, but reject all
  provider requests before service/adapter dispatch until #2140 supplies concrete adapters.
- Lifecycle orchestration remains owned by #2118.
- Never expose credentials, DSNs, provider output, or journal bytes in diagnostics.
- Guardrails: focused tests while iterating; `just lint`; whole-tree `just type`; relevant
  `prek` hooks; `just ci` before delivery.

Expected implementation size: 7,000–8,000 changed lines (L) — derived from migration 0125, the host
runtime and bounded transport wrapper, CLI wiring, systemd artifacts, Ansible
provisioning/verification, tests, and diagnostics.

## Task 1: Add least-privilege head inventory and peer authentication

Files:

- Create `src/kdive/db/schema/0125_external_boot_authority_head_inventory.sql`.
- Modify `src/kdive/db/external_boot_authority_journal.py`.
- Create `tests/db/test_external_boot_authority_head_inventory_migration.py` and update exact
  migration-tail assertions generated from the schema inventory.

Interfaces:

- SQL `list_external_boot_authority_journal_heads(text)` returns only authority instance, System,
  sequence, digest, phase, authority, generation, and operation identity for the supplied instance,
  with a trust-boundary `LIMIT 4097`; row 4097 signals the fixed 4096-lane ceiling was exceeded.
- SQL `authenticate_external_boot_authority_peer(bytea)` accepts only a 32-byte credential hash and
  returns only the matching active fence-protocol-4 worker incarnation identifier.
- `list_journal_heads(conn, authority_instance: str) -> tuple[JournalHead, ...]` is Task 2's exact
  inventory input.
- `authenticate_authority_peer(conn, credential: SecretStr) -> AuthenticatedPeer` hashes the
  credential before SQL and exposes no raw credential or worker row.

Verification:

- Mode: focused-test. Contract: only `kdive_provider_authority` can execute the security-definer
  inventory and authenticate an active worker credential; 0125 grants no new direct table access;
  results are instance-filtered and limited to 4097 rows; peer authentication returns only an
  incarnation identifier; and no lifecycle write is possible. The shared role-wide visibility is the
  same accepted scope as its existing binding SELECTs. Red observation: migration 0125 is absent.
  Green command:
  `uv run python -m pytest tests/db/test_external_boot_authority_head_inventory_migration.py -q`.

Steps:

1. Add migration privilege, isolation, empty, multi-instance, 4096/4097-boundary, invalid-hash,
   inactive-worker, wrong-fence-protocol, and valid-worker tests; confirm they fail because 0125 is
   absent.
2. Add migration 0125 and the typed repository reader; run the focused command and expect green.
3. Regenerate/update migration inventory assertions and run the migration-order guard.
4. Commit as `feat(db): expose bounded authority head inventory`.

Acceptance: the authority can discover a trusted head whose local lane is absent and derive an
authenticated peer only from a valid active worker credential, without direct table access or
unrelated tenant/lifecycle data.

## Task 2: Add the fail-closed host readiness runtime

Files:

- Create `src/kdive/providers/external_boot_authority/host.py`.
- Create `src/kdive/providers/external_boot_authority/transport.py`.
- Modify `src/kdive/__main__.py`.
- Create `tests/providers/external_boot_authority/test_host.py`.
- Create `tests/providers/external_boot_authority/test_transport.py`.

Interfaces:

- `AuthorityHostConfig.from_environment() -> AuthorityHostConfig` consumes fixed `KDIVE_*`
  configuration and systemd credential-directory paths.
- `check_authority_host(config: AuthorityHostConfig, listener: AuthorityListener) -> Awaitable[None]`
  validates identity, protected paths, journal restoration, database role shape, provider socket
  access, and the bound listener's socket/TLS evidence.
- `serve_authority_transport(config, authenticate_peer, service=None)` binds the configured Unix
  stream with TLS 1.3, requires a client certificate, bounds one length-prefixed closed envelope,
  authenticates its worker credential through Task 1, and rejects with `provider-not-configured`
  while `service` is absent.
- Closed response envelopes return either one existing acknowledgement/observation model or one
  bounded transport/service error category; the server closes after one request/response pair.
- `run_authority_host(config: AuthorityHostConfig) -> Awaitable[None]` performs the same check,
  creates the request listener with `start_serving=False`, validates it, begins serving, sends
  `READY=1`, repeats the equivalent live check every 30 seconds, then sends `STOPPING=1` and exits
  on drift. The one-shot command uses the same factory/check against a fixed sibling probe socket.
- `check_tls_health(listener, config)` uses an authority-owned health-client certificate with no
  incarnation credential to complete a real mutual-TLS self-handshake against the deterministic
  server name, closing before any application frame. Startup and every interval thereby re-evaluate
  current server/client/CA validity and EKUs through the standard-library TLS stack.
- CLI handlers expose `external-boot-authority-host` and
  `check-external-boot-authority-host`; Task 4's unit and Ansible probe rely on those names.

Verification:

- Mode: focused-test. Contract: credentials must form one complete provenance profile: either
  authority-owned mode-`0400` source files under protected root/authority parents, or root-owned
  mode-`0440` systemd projections under exclusively root-owned protected parents. Links, mixed
  profiles, writable or foreign ancestry, and other owners/modes fail closed; journal paths remain
  authority-owned, non-symlinked, and exact-mode. Cases `test_host_rejects_unsafe_credentials`,
  `test_host_accepts_systemd_projected_credentials`,
  `test_host_rejects_unsafe_systemd_projection`,
  `test_host_rejects_systemd_projection_under_unsafe_ancestry`,
  `test_host_rejects_mixed_source_and_projected_credentials`, and
  `test_host_rejects_invalid_journal_tree`; red observation is missing module/import; green command
  is `uv run python -m pytest tests/providers/external_boot_authority/test_host.py -q`.
- Mode: focused-test. Contract: role shape and diagnostics fail closed without secret values.
  Cases: `test_host_rejects_privileged_database_role` and
  `test_host_diagnostics_are_bounded_and_secret_free`; same focused green command.
- Mode: focused-test. Contract: the database inventory and local lanes are a bijection, exact heads
  match, and periodic socket/ACL/role/journal drift exits the service. Cases:
  `test_host_rejects_missing_or_extra_lane` and `test_host_exits_when_boundary_drifts`; same command.
- Mode: focused-test. Contract: readiness is absent during an initial slow/failing check, appears
  only after success, and is retracted before drift exit. Case `test_host_notifies_readiness_state`;
  same focused command.
- Mode: focused-test. Contract: the listener cannot serve or publish readiness before its bound
  socket and loaded TLS evidence pass; wrong initial TLS/socket state suppresses `READY=1`, and
  later live-listener or credential-fingerprint drift sends `STOPPING=1`. Cases
  `test_host_validates_listener_before_ready` and `test_host_retracts_on_listener_drift`; same host
  command.
- Mode: focused-test. Contract: a time advance across the unchanged server, health-client, or CA
  certificate validity boundary makes the next health handshake fail and sends `STOPPING=1`; the
  health client has no incarnation credential and never constructs `AuthenticatedPeer`. Case
  `test_host_retracts_when_transport_certificate_expires`; same host command.
- Mode: focused-test. Contract: the Unix stream requires TLS 1.3 client authentication and bounded
  canonical framing; invalid certificates fail before request bytes are read, invalid lengths and
  envelopes are rejected without allocation or secret disclosure, and an invalid worker credential
  never constructs `AuthenticatedPeer`. Cases `test_transport_requires_mutual_tls`,
  `test_transport_rejects_oversize_before_read`, and
  `test_transport_authenticates_incarnation_before_dispatch`; green command:
  `uv run python -m pytest tests/providers/external_boot_authority/test_transport.py -q`.
- Mode: focused-test. Contract: the server certificate has `serverAuth` EKU and a DNS SAN equal to
  the deterministic base32-SHA-256 authority-instance name, while client certificates require
  `clientAuth` EKU. Wrong-instance, wrong-EKU, expired, and untrusted certificates fail before an
  envelope is sent. Case `test_transport_binds_certificate_purpose_and_instance`; same transport
  command.
- Mode: focused-test. Contract: a valid proof client and active incarnation receive only
  `provider-not-configured`, and no `ExternalBootAuthorityService` or adapter method runs. Case
  `test_dormant_transport_refuses_before_provider_dispatch`; same transport command.
- Mode: focused-test. Contract: one-shot probes serialize through a nonblocking authority-owned
  lock, remove only an unlocked stale authority-owned socket after an inode recheck, and reject a
  live, foreign, replaced, or symlink path. Cases `test_probe_recovers_owned_stale_socket` and
  `test_probe_rejects_concurrent_or_foreign_socket`; same host command.

Steps:

1. Add the focused tests and run the command; expect collection/import failure.
2. Implement the immutable config and filesystem checks using `os.open`/`stat` without following
   symlinks; run the focused command and expect filesystem cases green.
3. Implement the closed envelope, four-byte length bound, deterministic TLS server name, strict TLS
   contexts, authority-owned health client, listener evidence, serialized probe cleanup, credential
   redaction, and dormant dispatcher using only the standard library and existing protocol models;
   run the transport command and expect green.
4. Implement the async psycopg role/function checks and bounded error type; run both focused files
   and expect green.
5. Wire the two CLI commands and add parser tests; run the focused commands plus the existing CLI
   parser tests and expect green.
6. Commit as `feat(authority): host authenticated dormant boundary`.

Acceptance: no caller-selected path or provider definition crosses the boundary; a failed check
exits non-zero with only component/reason; startup and periodic checks detect complete-lane loss and
post-start drift; and a mutually authenticated request cannot reach provider code before #2140.

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
- `live_vm_host_authority_client_group` alone traverses the separate authority request-socket
  parent; existing worker and reconciler identities are not members.
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
- Create `deploy/ansible/playbooks/authority_host_teardown.yml`.
- Create `scripts/operations/prove-external-boot-authority-host.sh`.
- Modify the live-VM host Ansible defaults, tasks, and verification files.
- Modify `tests/deploy/test_systemd_units.py` and `tests/deploy/test_live_worker_provisioning.py`.

Interfaces:

- The `Type=notify`, `NotifyAccess=main` systemd unit runs
  `/opt/kdive-provider-authority/.venv/bin/python -m kdive
  external-boot-authority-host`, loads `database-dsn` and `service-credential` through
  `LoadCredential=` together with the server certificate/CA, worker-client CA, and health-client
  certificate/key, uses
  `User=kdive-provider-authority`, and hardens filesystem/network access. `service-credential` is
  the TLS server private key rather than an unused sentinel.
- Ansible installs the venv, authority-owned mode-`0400` credential sources under their protected
  directory, journal and runtime directories, request-client group, unit, configuration, and
  readiness probe in clean-host order. `LoadCredential=` presents the supervised process only with
  root-owned mode-`0440` projections beneath systemd's protected root-owned directory. Production
  workers receive no client material; provisioning creates a transient proof identity and
  short-lived client certificate, proves the complete authentication path, then removes both and
  retires the proof worker incarnation.

Verification:

- Mode: focused-test. Contract: the unit has retry, credential, identity, and hardening directives.
  Case `test_external_boot_authority_unit_is_isolated_and_supervised`; red observation is missing
  unit; green command is `uv run python -m pytest tests/deploy/test_systemd_units.py -q`.
- Mode: focused-test. Contract: Ansible creates paths before start, starts the service, and runs
  every positive and negative readiness proof. Case
  `test_ansible_installs_authority_in_clean_host_order`; red observation is missing declarations;
  green command is `uv run python -m pytest tests/deploy/test_live_worker_provisioning.py -q`.
- Mode: focused-test. Contract: `scripts/operations/prove-external-boot-authority-host.sh` creates
  and transfers a Git bundle containing the exact local HEAD, installs it in a SHA-named proof
  directory on the authorized clean Ubuntu carrier, bootstraps a peer-authenticated local
  PostgreSQL database and real `kdive` denial identity, overrides `live_vm_repo_url` with the bundle
  and `live_vm_repo_version` with the immutable full SHA, asserts the installed revision,
  creates a proof-only CA/server/client chain and active worker-incarnation credential,
  starts/restarts both services, verifies server/client/worker authentication and dormant dispatch
  denial, injects drift, and observes readiness retract.
  Carrier: `dave@ub26-big.dev.pdx.drc.nz`; green command:
  `scripts/operations/prove-external-boot-authority-host.sh dave@ub26-big.dev.pdx.drc.nz $(git rev-parse HEAD)`.

Steps:

1. Add the unit and provisioning structural tests and confirm the expected red failures.
2. Add the systemd unit and example configuration.
3. Add authority identity, client group, directories, TLS/database credentials, venv installation,
   unit install/start, and readiness verification to Ansible in dependency order.
4. Run both focused deployment files and `just lint-ansible`; expect green.
5. Write the runbook, proof script, and idempotent teardown play before execution. Resolve exact
   pre-existing remote targets read-only; deploy from the transferred Git bundle with both source
   overrides; verify the installed revision equals the supplied full SHA; then run the teardown
   twice and prove both passes leave the dormant units/endpoint absent, LOGIN revoked, retained
   journal/credential state intact, proof identity/material absent, and the existing worker provider
   path usable. Retain bounded evidence and clean only SHA-named/proof-labeled artifacts created by
   this command.
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
  mutual-TLS/request-socket diagnosis, group/socket inspection, and the explicit
  capability-advertisement hold.

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
staged file set, the exact clean-host proof, and `just ci`. All must exit zero before delivery.
Rollback first runs the idempotent authority-host teardown, leaving migration 0125 applied and
inert; retain authority journals and credentials for operator-controlled cleanup.
