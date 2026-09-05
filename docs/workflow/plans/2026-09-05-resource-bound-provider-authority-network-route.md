# Resource-bound provider-authority network route implementation plan

Goal: add an opt-in Resource-bound mTLS route to the provider-host authority while preserving its
AF_UNIX behavior and closed protocol.

Architecture: inventory produces one frozen route binding per remote-libvirt Resource. Worker
composition closes a deadline-bound connector over that binding, secret material, and the active
incarnation credential. The authority optionally serves the same closed dispatcher over TCP and
publishes readiness only after both listeners and their TLS evidence pass.

Tech stack: Python 3.14, asyncio streams, `ssl`, Pydantic, Ansible, systemd, nftables/ufw-compatible
host firewall ownership already used by the role, pytest, `just`.

Expected implementation size: 900–1500 changed lines (L) — derived from six executable contracts,
their trust-boundary tests, deployment carrier, and live-proof harness.

## Global constraints

- Preserve the existing AF_UNIX listener and both existing operation envelopes byte-for-byte.
- TLS is exactly version 1.3 with mandatory client certificates and derived server-name checking.
- Routing is selected only from the allocated Resource; callers cannot provide an endpoint,
  credential, command, path, arguments, environment, or generic stream.
- The active worker-incarnation credential is supplied at request time and never persisted.
- All input, IO, framing, authentication, cleanup, readiness, and diagnostics are bounded and
  redacted.
- Default and partial deployments expose no network listener. No database migration is permitted.
- x86_64 is the live host architecture; project targets also include ppc64le, but native ppc64le
  live proof is explicitly excluded.
- Use `just lint`, `just type`, focused `just test-verbose <path>`, and final `just ci`.

## Task 1: Validate and bind Resource configuration

Files: `src/kdive/inventory/model.py`, `src/kdive/providers/remote_libvirt/config.py`,
`tests/inventory/test_loader.py`, `tests/providers/remote_libvirt/test_config.py`.

Interfaces:

- Define frozen `RemoteAuthorityBinding(authority_instance: str, address: str, port: int,
  server_ca_ref: str, client_cert_ref: str, client_key_ref: str)`.
- `RemoteLibvirtConfig.authority: RemoteAuthorityBinding | None` is consumed by Task 4.
- `remote_config_for_resource(resource_name: str)` remains the only Resource selector.

Verification:

- Mode: focused-test — complete/all-absent binding, partial tuple, numeric destination, port,
  secret-ref and extra-field validation in the named test files; observe new cases fail before the
  models change, then pass with `just test-verbose tests/inventory/test_loader.py
  tests/providers/remote_libvirt/test_config.py`.

Steps:

1. Add failing table-driven inventory and config-mapping tests, including two Resources whose
   authority endpoints cannot be substituted.
2. Add strict all-or-none fields and canonical numeric-address validation to
   `RemoteLibvirtInstance`.
3. Add `RemoteAuthorityBinding` and map it in `_build_config` without changing unbound behavior.
4. Run the focused command; expect all selected tests to pass.
5. Commit as `feat(providers): bind authority routes to resources`.

## Task 2: Add the optional network listener and health operation

Files: `src/kdive/providers/external_boot_authority/protocol.py`,
`src/kdive/providers/external_boot_authority/transport.py`,
`src/kdive/providers/external_boot_authority/settings.py`,
`src/kdive/providers/external_boot_authority/host.py`, and corresponding files under
`tests/providers/external_boot_authority/`.

Interfaces:

- Add closed `health` request/response protocol models.
- Add optional `AuthorityHostConfig.network_address/network_port`.
- Add `serve_authority_network_transport(config, authenticate_peer, service=None)` returning
  independently validatable/closable listener evidence.
- Existing `serve_authority_transport` and `AuthorityListener` AF_UNIX signatures remain stable.

Verification:

- Mode: focused-test — closed health dispatch, mTLS network bind, certificate rejection, server
  name, framing bounds, listener drift, dual-listener cleanup, and AF_UNIX compatibility; first run
  each added case to observe failure, then run `just test-verbose
  tests/providers/external_boot_authority` and expect all selected tests to pass.

Steps:

1. Add protocol and transport tests that retain the two existing operation encodings and prove a
   no-service health dispatch only after authentication.
2. Add host setting/config tests for both-or-neither network bind values.
3. Implement shared session dispatch and a separate `asyncio.start_server` listener evidence type.
4. Integrate dual listeners into startup, periodic validation, health, and all cleanup paths.
5. Run the focused command; expect all selected tests to pass.
6. Commit as `feat(authority): add an authenticated network listener`.

## Task 3: Build a deadline-bound Resource connector

Files: new `src/kdive/providers/external_boot_authority/network_client.py`, new
`tests/providers/external_boot_authority/test_network_client.py`.

Interfaces:

- `AuthorityNetworkRoute(binding, tls_material, incarnation_credential)` closes over all authority.
- `async request(operation: Operation, request: dict[str, object], *, deadline: float) -> bytes`
  accepts no endpoint or credential.
- `async health(*, deadline: float) -> None` is the worker readiness probe.

Verification:

- Mode: focused-test — fixed destination, TLS/name validation, active/inactive credential, one
  deadline over connect/write/read/close, stalled peer, malformed response, cleanup, and redaction;
  observe the new file fail first, then pass `just test-verbose
  tests/providers/external_boot_authority/test_network_client.py`.

Steps:

1. Add a real-loopback TLS fixture with anonymous certificate metadata and failing route cases.
2. Implement strict TLS material loading using the existing secret backend/redaction registry and
   temporary-file ownership pattern.
3. Implement the connector with one remaining-budget calculation and guaranteed writer cleanup.
4. Run the focused command; expect all selected tests to pass.
5. Commit as `feat(authority): add a resource-bound network client`.

## Task 4: Compose the current worker credential and readiness

Files: `src/kdive/assembly.py`, `src/kdive/jobs/assembly.py`,
`src/kdive/providers/assembly/composition.py`,
`src/kdive/providers/remote_libvirt/composition.py`, remote diagnostics sources, and corresponding
assembly/diagnostic tests.

Interfaces:

- Provider composition receives the current `SecretStr` only in worker assembly; server and
  reconciler builds remain credential-free.
- Resource rebinding constructs `AuthorityNetworkRoute` only from
  `RemoteLibvirtConfig.authority` and the existing `SecretBackend`.
- Remote worker diagnostics expose a closed readiness result and no network identity.

Verification:

- Mode: focused-test — process-role separation, exact active credential identity, per-Resource
  rebinding, missing binding/credential, inactive authentication and redacted diagnostics; observe
  new assertions fail, then pass `just test-verbose tests/jobs/test_assembly.py
  tests/providers/remote_libvirt tests/diagnostics`.

Steps:

1. Add assembly tests proving only worker construction receives the credential and it reaches the
   route unchanged.
2. Extend provider-composition inputs with a role-specific optional route factory, failing closed
   when a bound operation lacks worker authority.
3. Add the remote worker readiness builder and health call without exposing an application action.
4. Run the focused command; expect all selected tests to pass.
5. Commit as `feat(providers): compose worker authority routes`.

## Task 5: Provision and verify the opt-in route

Files: `deploy/ansible/roles/live_vm_host/defaults/main.yml`,
`deploy/ansible/roles/live_vm_host/tasks/authority_preflight.yml`,
`deploy/ansible/roles/live_vm_host/tasks/main.yml`,
`deploy/ansible/roles/live_vm_host/tasks/verify.yml`, role handlers/templates as needed,
`deploy/systemd/system/kdive-external-boot-authority.service`,
`tests/deploy/test_live_worker_provisioning.py`.

Interfaces:

- Add opt-in listen address, port, worker source, and firewall variables.
- Render both network settings only for a complete opt-in tuple.
- Install exactly one source-scoped TCP allow rule owned by the role.

Verification:

- Mode: focused-test — defaults disabled, partial input rejection, rendered environment, firewall
  source/port/protocol, service confinement, drift and idempotence structure; observe assertions
  fail, then pass `just test-verbose tests/deploy/test_live_worker_provisioning.py`.

Steps:

1. Add deployment contract tests for disabled, complete, partial, unsafe, drift, and idempotent
   configurations.
2. Add defaults and preflight assertions, then render the conditional listener environment.
3. Add the source-scoped firewall rule and verification without widening systemd privileges.
4. Run the focused command; expect all selected tests to pass.
5. Commit as `feat(provisioning): deploy authority network routes`.

## Task 6: Run the authorized-host proof and guardrails

Files: live proof carrier under `deploy/ansible/roles/live_vm_host/` or `tests/live_vm/` as selected
by the existing runbook pattern; no ppc64le carrier.

Interfaces:

- The proof consumes operator-supplied anonymous runtime values and emits only named boolean
  outcomes and fixed reason codes.
- It proves configured success, untrusted-client denial, non-configured-destination denial, and
  preserved AF_UNIX success.

Verification:

- Mode: focused-test — carrier redaction and four-outcome contract; observe its structural test
  fail, then pass the exact focused test selected from `tests/deploy` or `tests/live_vm`.

Steps:

1. Add a redaction-safe proof carrier and a structural test for its fixed output vocabulary.
2. Run `just lint`, `just type`, and all focused commands above; expect zero failures.
3. Provision the authorized Ubuntu host with the owning role and run the proof; expect four true
   outcomes and no identifying values in retained/public evidence.
4. Run `just ci > /tmp/kdive-2252-ci.log 2>&1 < /dev/null`; expect exit 0.
5. Commit any proof-carrier source as `test(providers): prove authority network isolation`.

## Rollback

Disable the opt-in listener variables and rerun the role: the network listener and owned firewall
rule disappear while AF_UNIX remains. Reverting application commits removes the unused binding and
connector without persisted-data migration. Never remove operator TLS sources outside the role's
declared ownership.
