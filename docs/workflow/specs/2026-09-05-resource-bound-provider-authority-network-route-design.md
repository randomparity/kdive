# Resource-bound provider-authority network route design

Issue: #2252
Decision: [ADR-0606](../../adr/0606-resource-bound-provider-authority-network-route.md)

## Goal and scope

Add an opt-in, resource-bound mutual-TLS network route from a remote-libvirt worker to the
provider-host authority. The route projects the current worker-incarnation credential only while a
closed authority request is sent. It preserves the AF_UNIX transport and adds neither a
caller-selected destination nor generic execution.

Identity request models and lookup belong to #2250. External-boot client semantics belong to
#2215; coordinator and IO construction belong to #2200/#2216. This design changes no database
schema and requires no native ppc64le proof.

## Configuration contract

`RemoteLibvirtInstance` gains six optional fields that form one all-or-none binding:

- `authority_instance`: non-empty authority identity;
- `authority_address`: a canonical numeric IPv4 or IPv6 address, not unspecified, multicast, or a
  URI/hostname;
- `authority_port`: integer 1–65535;
- `authority_server_ca_ref`, `authority_client_cert_ref`, and `authority_client_key_ref`: distinct
  non-empty secret references.

The numeric-address rule makes endpoint parsing unambiguous and keeps DNS out of route selection.
The authority certificate is still checked against
`authority_server_name(authority_instance)`. Pydantic validates all-or-none configuration and
rejects extra fields. `_build_config` maps a complete declaration to frozen
`RemoteAuthorityBinding`; absent configuration remains `None`. `RemoteLibvirtConfig` never accepts
raw request-time overrides.

The provider-host settings add an opt-in numeric listen address and port. Both must be present or
absent. The address may be unspecified for a server bind, because firewall and operator
configuration constrain exposure; clients still reject unspecified destinations. A configured
listener uses the same authority instance and server certificate as AF_UNIX.

## Transport and composition

The existing frame codec, envelope decoder, peer authenticator, and dispatcher remain the sole
application boundary. `serve_authority_transport` continues to create the AF_UNIX listener. A new
`serve_authority_network_transport` creates one `asyncio.start_server` listener with the same
handler and TLS context. The returned network-listener evidence stores the exact configured bind,
socket family, TLS fingerprints, and serving state, and can validate and close independently.

`AuthorityHost` starts neither network listener nor network readiness unless both network settings
are configured. When configured it validates static inputs, binds both listeners before publishing
readiness, starts both, proves both TLS handshakes, and closes both on every exit. Periodic checks
reconstruct listener identity and credential fingerprints; any drift raises a bounded
`HostReadinessError`, causing readiness withdrawal and service restart.

`AuthorityNetworkRoute` is constructed from exactly one `RemoteAuthorityBinding` and a resolved TLS
material owner. It retains no incarnation credential. Its request method receives only an
already-authenticated closed envelope and a positive absolute monotonic deadline. The remaining
budget covers TCP connect, TLS handshake, write, response read, and close. It uses TLS 1.3, verifies
the server certificate and derived name, and maps all errors to bounded redacted categories. It
exposes no raw reader/writer, host, port, TLS, command, path, argument, or environment input.

`WorkerHandlerAssembly` remains the sole process-lifetime incarnation-credential owner. It creates
a worker-owned request sender that references that existing field, reads it only while calling
`encode_request_envelope`, drops the method-local plaintext after encoding, and passes the encoded
frame to the route. The route object has no credential field, and cancellation or worker
replacement leaves no copied `SecretStr`. Resource rebinding selects one config by Resource name
and creates the route only from that config's closed binding. Secret values resolve through
`SecretBackend` and register with `SecretRegistry`; temporary certificate files follow the existing
remote-libvirt TLS material pattern and are removed after context construction.

## Authentication-only readiness

The network envelope gains a closed `health` operation with an empty versioned request and a
versioned acknowledgement. The authority authenticates TLS and the incarnation credential, checks
that the credential names a currently active worker, returns the acknowledgement, and invokes no
provider service. AF_UNIX accepts the same additive operation so dispatch behavior is identical;
existing acknowledgement and mutation operations remain byte-compatible.

Worker readiness for a bound Resource builds its route and calls `health` within the existing
diagnostic budget. Missing binding is reported as unadvertised rather than healthy. Missing secret
material, certificate rejection, server-name mismatch, inactive/replaced credential, timeout, or
transport failure yields a redacted readiness failure. No address, authority instance, secret ref,
credential, TLS diagnostic, or peer output enters errors or durable state.

## Provisioning and live proof

The remote-provider-host play in `deploy/ansible/site.yml` gains a narrow
`provider_authority_host` role owning the authority account, install, credentials, environment,
systemd unit, listener readiness, and clean removal when disabled. Shared task files may be reused
by `live_vm_host`, but runner-specific provisioning does not own production remote hosts. The
role's preflight requires the complete listener tuple and rejects unsafe or empty values.

The existing `gdbstub_acl` role remains the cross-distribution firewall owner. It accepts the
optional authority port as another protected TCP port, adds source-CIDR allow and lower-priority
deny rules on firewalld and ordered allow/deny rules on ufw, and prunes rules for stale sources and
ports. Disabling the authority listener removes its formerly managed allow/deny rules without
touching libvirt TLS, gdbstub, or management access. The systemd unit continues to permit only
AF_UNIX/AF_INET/AF_INET6 and gains no executable or filesystem authority.

Role verification proves `site.yml` applies both owners, idempotent configuration, listener
ownership, service readiness, Debian and Red Hat firewall shape, and enable-then-disable stale-rule
removal. The authorized Ubuntu host is provisioned from `site.yml`, not manually. A redacted live
carrier proves a configured worker route succeeds, a client without the trusted certificate fails,
a client aimed at a non-configured destination fails, and the local AF_UNIX route still works. No
endpoint, certificate, credential, host identifier, or private network detail is posted.

## Error contract

- Inventory or partial host configuration: `CONFIGURATION_ERROR` with fixed field-category text.
- Missing/unreadable TLS secret: existing secret-resolution category, redacted by registration.
- TLS, authentication, inactive incarnation, endpoint, timeout, framing, or peer failure:
  `READINESS_FAILURE` for readiness and `INFRASTRUCTURE_FAILURE` for operation use, with closed
  reason vocabulary.
- Malformed frames remain `invalid-request`; authentication happens before dispatch.
- Cleanup is best-effort only after the primary bounded failure is fixed; descriptors, writers,
  servers, and temporary TLS files close on success, cancellation, timeout, and errors.

## Threat model

### Boundary inventory

Added boundaries are inventory-to-route endpoint selection, secret-reference-to-TLS context,
worker-to-provider-host TCP/TLS, incarnation credential in the request envelope, and
operator-config-to-listener/firewall provisioning. The existing AF_UNIX listener is widened only by
the additive health operation.

### Actors and trust

Untrusted actors are authenticated tenants able to request provider work, network peers able to
reach the provider-host address, a worker process with a stale or replaced credential, and an
operator supplying malformed inventory. The operator provisioning the Resource binding, authority
identity, CA roots, and firewall source is trusted. The secret backend and worker-incarnation
authority remain trusted existing boundaries.

### Controls

- Tenants cannot choose routing: Resource allocation selects a frozen binding, and request APIs
  accept no endpoint or credential.
- Network peers need both a certificate chaining to the configured worker CA and a current active
  incarnation credential; failure occurs before operation dispatch.
- Stale workers fail database-backed active-incarnation authentication even when their TLS
  certificate remains valid.
- Inputs have closed shapes, bounded frames and credentials, numeric addresses, bounded ports,
  TLS 1.3, certificate-name verification, and one deadline spanning every IO stage.
- Secret material is by reference, registered for redaction, short-lived in temporary files where
  required, never serialized into config, and absent from diagnostics. The route does not retain
  the incarnation credential; its worker-owned sender borrows the assembly-owned value only while
  constructing one envelope.
- The listener is opt-in, source-firewalled, minimally confined, periodically revalidated, and
  removed from readiness on drift.

### Out of scope threats

Compromise of the trusted provider host, secret backend, CA issuer, or worker process with its
currently active credential remains an operator/platform risk. Operation-specific authorization and
provider mutation semantics belong to the closed operation owners. This route does not add rate
limiting beyond existing session/frame/time bounds and systemd/firewall controls.

## Verification

Tests cover binding completeness and validation; resource rebinding and endpoint substitution;
TLS version, certificate trust, server-name mismatch, expired/untrusted certificates; inactive and
replaced credentials; deadline and stalled-peer behavior; bounded framing; redaction; AF_UNIX and
existing-operation compatibility; listener and credential drift; cleanup; worker composition; role
defaults/preflight/rendering/firewall/idempotence; and the redacted authorized-host live carrier.
