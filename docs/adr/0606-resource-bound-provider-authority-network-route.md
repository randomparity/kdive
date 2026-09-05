# 0606 — Bind provider-authority network routes to Resources

## Status

Accepted (2026-09-05)

## Context

The provider-host authority established by ADR-0584 currently accepts mutually authenticated
requests only through an AF_UNIX socket. A remote-libvirt worker runs outside that host namespace,
so ADR-0603's remote device-identity client cannot reach the authority. Allowing a request or a
generic connector to select a network destination would turn a narrow provider port into an SSRF
and credential-forwarding boundary.

## Decision

Each remote-libvirt Resource may carry one optional, complete `RemoteAuthorityBinding`: a canonical
numeric IPv4 address, port, authority instance, and distinct server-CA, worker-certificate, and
worker-key secret references. IPv6 is rejected explicitly in this version. Inventory validates
this binding as a closed value and runtime composition closes over the selected Resource's value.
Requests cannot supply or replace any destination, TLS identity, or credential.

The provider-host authority may opt into an additive TCP listener beside its existing AF_UNIX
listener. Both listeners share the closed framed protocol, TLS 1.3 server context, mandatory client
certificate verification, active-incarnation authentication, bounded sessions, and dispatcher.
The network listener binds only an operator-configured IPv4 address and port. Its certificate is
verified against the stable name derived from the configured authority instance.

Worker construction resolves the selected binding's three TLS values through the existing secret
backend. A private resource-bound byte transport accepts one encoder-produced frame and a deadline
and retains no incarnation credential. It is reachable only through a typed worker-owned request
sender whose methods correspond to closed protocol operations. The worker assembly, which already
owns the active process credential, supplies a borrowing accessor; the sender reads it only while
encoding each envelope. No second standing copy is placed in inventory, provider runtime state,
logs, or results.

Authority-host readiness reconstructs both listeners and performs an authority-owned TLS health
handshake when the network listener is configured. Worker readiness constructs the selected route
and performs an authentication-only health exchange that dispatches no provider operation.
The remote-provider-host play installs a narrow provider-authority role owning the opt-in listener,
credentials, service policy, and readiness. The existing cross-distribution `gdbstub_acl` role owns
the additional source-scoped firewall rule and stale-rule removal. Partial configuration fails
closed and the default deployment exposes no network listener.

The authority's denied local identities are a host-specific, non-empty setting rather than an
assumption that every provider host has the live-runner accounts. It contains 1 through 32 unique
canonical ASCII account names matching `[a-z_][a-z0-9_-]{0,31}`. Readiness still requires every
listed account to exist and rejects root, the authority identity, or membership in either authority
group. `live_vm_host` keeps `kdive-worker-1` through `kdive-worker-8` and `kdive` as its unchanged
default. `provider_authority_host` supplies its existing Ansible control identity plus any explicit
additional local identities; it does not create placeholder worker accounts. Parsing, lookup, and
readiness failures remain bounded and redact account names.

## Consequences

Remote workers can reach the exact authority paired with their allocated Resource without gaining
a general network connector. The AF_UNIX route and its protocol remain compatible. Network
deployment adds certificate, firewall, readiness, and rotation obligations; drift retracts
readiness. A provider-host deployment must name the real local identities whose exclusion it
proves; a missing named identity is a failed deployment, not a skipped check. #2250 and #2200 can
add closed operations over this route without changing its binding.
IPv6-only provider hosts remain unsupported until a separately designed family-aware firewall
contract exists.

## Considered & rejected

- **Colocate workers with every provider host.** judgment: this changes fleet scheduling and
  couples worker placement to provider topology to avoid one narrow transport boundary.
- **Accept a destination per operation.** judgment: caller-selected routing would permit endpoint
  substitution and forwarding of an active worker credential.
- **Expose a generic command or byte-stream service.** verified: ADR-0584 confines the authority to
  a closed provider protocol and explicitly denies generic execution.
- **Replace AF_UNIX with TCP.** judgment: local deployments need no network exposure, and removing
  the existing route would break the accepted colocated deployment contract.
- **Support IPv4 and IPv6 in the first route.** verified: the existing cross-distribution firewall
  owner emits IPv4 firewalld rules and has one IPv4 worker CIDR; dual-stack support would require a
  separate address-family and stale-rule lifecycle rather than a transport-only field extension.
