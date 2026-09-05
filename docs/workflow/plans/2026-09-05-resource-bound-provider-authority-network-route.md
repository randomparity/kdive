# Resource-bound provider-authority network route implementation plan

Goal: add an opt-in Resource-bound mTLS route to the provider-host authority while preserving its
AF_UNIX behavior and closed protocol.

Architecture: inventory produces one frozen route binding per remote-libvirt Resource. A private
deadline-bound transport closes only over that binding and resolved TLS material. A typed
worker-owned sender is its sole caller and borrows the assembly-owned incarnation credential only
while encoding one closed envelope. The authority optionally serves the same closed dispatcher over
TCP and publishes readiness only after both listeners and their TLS evidence pass.

Tech stack: Python 3.14, asyncio streams, `ssl`, Pydantic, Ansible, systemd, nftables/ufw-compatible
host firewall ownership already used by the role, pytest, `just`.

Expected implementation size: 900–1500 changed lines (L) — derived from six executable contracts,
their trust-boundary tests, deployment carrier, and live-proof harness.

## Global constraints

- Preserve the existing AF_UNIX listener and both existing operation envelopes byte-for-byte.
- TLS is exactly version 1.3 with mandatory client certificates and derived server-name checking;
  authority endpoints are IPv4-only and reject IPv6 explicitly.
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

- Mode: focused-test — complete/all-absent binding, partial tuple, canonical IPv4 destination,
  explicit IPv6/hostname/URI rejection, port, secret-ref and extra-field validation in the named
  test files; observe new cases fail before the models change, then pass with `just test-verbose tests/inventory/test_loader.py
  tests/providers/remote_libvirt/test_config.py`.

Steps:

1. Add failing table-driven inventory and config-mapping tests, including two Resources whose
   authority endpoints cannot be substituted.
2. Add strict all-or-none fields and canonical IPv4-address validation to
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

- Private `_AuthorityNetworkTransport(binding, tls_material)` closes over destination and TLS
  authority only and has no credential field.
- Private `_request_frame(envelope: bytes, *, deadline: float) -> bytes` is called only by the typed
  sender defined in Task 4; no module export accepts raw bytes.

Verification:

- Mode: focused-test — fixed IPv4 destination, explicit IPv6 rejection, TLS/name validation, one
  deadline over connect/write/read/close, stalled peer, malformed response, cleanup, and redaction
  through a test-only typed harness rather than a public raw-byte API; observe the new file fail
  first, then pass `just test-verbose
  tests/providers/external_boot_authority/test_network_client.py`.

Steps:

1. Add a real IPv4-loopback TLS fixture with anonymous certificate metadata and failing transport
   cases, including IPv6 rejection.
2. Implement strict TLS material loading using the existing secret backend/redaction registry and
   temporary-file ownership pattern.
3. Implement the connector with one remaining-budget calculation and guaranteed writer cleanup.
4. Run the focused command; expect all selected tests to pass.
5. Commit as `feat(authority): add a resource-bound network client`.

## Task 4: Compose a non-retaining worker sender and readiness

Files: `src/kdive/assembly.py`, `src/kdive/jobs/assembly.py`,
`src/kdive/providers/assembly/composition.py`,
`src/kdive/providers/remote_libvirt/composition.py`, remote diagnostics sources, and corresponding
assembly/diagnostic tests.

Interfaces:

- `WorkerHandlerAssembly` remains the sole process-lifetime owner of the current `SecretStr`;
  server and reconciler builds remain credential-free.
- A worker-owned `AuthorityRequestSender` exposes only `health` and typed operation methods; it
  reads the existing assembly field only inside those methods, encodes the closed envelope, and
  gives bytes to its private credential-free transport.
- Resource rebinding constructs the private transport and sender only from
  `RemoteLibvirtConfig.authority`, the existing `SecretBackend`, and the assembly-owned borrowing
  accessor.
- Remote worker diagnostics expose a closed readiness result and no network identity.

Verification:

- Mode: focused-test — process-role separation, sender/transport objects without credential fields,
  no exported raw-byte call, exact active credential borrowing during typed `health`,
  active/inactive authentication, cancellation/replacement without a copied secret, per-Resource
  rebinding, missing binding/credential and redacted diagnostics; observe new assertions fail, then
  pass `just test-verbose
  tests/jobs/test_assembly.py tests/providers/remote_libvirt tests/diagnostics`.

Steps:

1. Add assembly tests proving only `WorkerHandlerAssembly` owns the credential and route instances
   retain no credential before, during, or after request cancellation and worker replacement.
2. Add a worker-owned typed request sender that borrows the assembly field during envelope
   construction; keep the byte transport private and extend provider-composition inputs with the
   sender factory, failing closed when a bound operation lacks it.
3. Add the remote worker readiness builder and health call without exposing an application action.
4. Run the focused command; expect all selected tests to pass.
5. Commit as `feat(providers): compose worker authority routes`.

## Task 5: Provision and verify the opt-in route

Files: new `deploy/ansible/roles/provider_authority_host/`, `deploy/ansible/site.yml`,
`deploy/ansible/roles/gdbstub_acl/defaults/main.yml`,
`deploy/ansible/roles/gdbstub_acl/tasks/main.yml`, shared authority task files where extraction is
needed, `deploy/ansible/roles/live_vm_host/` only as a consumer of shared ownership,
`deploy/systemd/system/kdive-external-boot-authority.service`,
`src/kdive/providers/external_boot_authority/settings.py`,
`src/kdive/providers/external_boot_authority/host.py`, corresponding host-setting tests, and
`tests/deploy/test_live_worker_provisioning.py`.

Interfaces:

- `provider_authority_host` owns opt-in listen address/port, authority service installation,
  credentials, environment, readiness and disabled cleanup on `remote_libvirt_hosts`.
- `gdbstub_acl` consumes an optional authority port and its existing IPv4 worker CIDR and owns
  source-scoped allow/deny creation plus stale source/port removal on firewalld and ufw.
- Authority denied identities are 1 through 32 unique canonical ASCII account names. Existing
  `live_vm_host` deployments retain `kdive-worker-1` through `kdive-worker-8` and `kdive` as the
  default; `provider_authority_host` supplies its pre-existing `ansible_user_id` plus explicit
  additional identities and creates no placeholder accounts. Missing identities and unsafe group
  membership continue to fail closed with redacted readiness diagnostics.

Verification:

- Mode: focused-test — `site.yml` role application, defaults disabled, partial input rejection,
  rendered environment, IPv6 rejection, Debian and Red Hat IPv4 firewall source/port/protocol,
  denied-identity bounds/default compatibility/production rendering/missing-account failure,
  service confinement, drift, idempotence, and enable-then-disable stale-rule removal; observe
  assertions fail, then pass
  `just test-verbose tests/deploy/test_live_worker_provisioning.py` and the existing
  `deploy/ansible/tests` firewall harness.

Steps:

1. Add deployment contract tests for production play application, disabled, complete, partial,
   unsafe, drift, idempotent, Debian/Red Hat, and disable-after-enable configurations.
2. Extract the smallest shared authority-host tasks from `live_vm_host`, build the narrow production
   role, apply it to `remote_libvirt_hosts`, and render the conditional listener and bounded
   host-specific denied-identity environment.
3. Extend `gdbstub_acl` with the source-scoped authority-port rules and explicit stale-rule removal
   without widening systemd privileges or disturbing its existing protected ports.
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
3. Provision the authorized Ubuntu host through `deploy/ansible/site.yml`, run it again to prove
   idempotence, run the proof, then disable and re-enable the listener to prove firewall cleanup;
   expect the four fixed proof outcomes and no identifying values in retained/public evidence.
4. Run `just ci > /tmp/kdive-2252-ci.log 2>&1 < /dev/null`; expect exit 0.
5. Commit any proof-carrier source as `test(providers): prove authority network isolation`.

## Rollback

Disable the opt-in listener variables and rerun the role: the network listener and owned firewall
rule disappear while AF_UNIX remains. Reverting application commits removes the unused binding and
connector without persisted-data migration. Never remove operator TLS sources outside the role's
declared ownership.
