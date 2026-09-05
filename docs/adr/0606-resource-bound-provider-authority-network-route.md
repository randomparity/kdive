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

Each remote-libvirt Resource may carry one optional, complete `RemoteAuthorityBinding`: a numeric
network address, port, authority instance, and distinct server-CA, worker-certificate, and
worker-key secret references. Inventory validates this binding as a closed value and runtime
composition closes over the selected Resource's value. Requests cannot supply or replace any
destination, TLS identity, or credential.

The provider-host authority may opt into an additive TCP listener beside its existing AF_UNIX
listener. Both listeners share the closed framed protocol, TLS 1.3 server context, mandatory client
certificate verification, active-incarnation authentication, bounded sessions, and dispatcher.
The network listener binds only an operator-configured numeric address and port. Its certificate is
verified against the stable name derived from the configured authority instance.

Worker construction resolves the selected binding's three TLS values through the existing secret
backend. A resource-bound connector accepts only an operation payload and deadline; it supplies the
active process incarnation credential at send time. No standing copy is placed in inventory,
provider runtime state, logs, or results.

Authority-host readiness reconstructs both listeners and performs an authority-owned TLS health
handshake when the network listener is configured. Worker readiness constructs the selected route
and performs an authentication-only health exchange that dispatches no provider operation.
Provisioning owns the opt-in listener variables, credentials, service policy, and source-scoped
firewall rule. Partial configuration fails closed and the default deployment exposes no network
listener.

## Consequences

Remote workers can reach the exact authority paired with their allocated Resource without gaining
a general network connector. The AF_UNIX route and its protocol remain compatible. Network
deployment adds certificate, firewall, readiness, and rotation obligations; drift retracts
readiness. #2250 and #2200 can add closed operations over this route without changing its binding.

## Considered & rejected

- **Colocate workers with every provider host.** judgment: this changes fleet scheduling and
  couples worker placement to provider topology to avoid one narrow transport boundary.
- **Accept a destination per operation.** judgment: caller-selected routing would permit endpoint
  substitution and forwarding of an active worker credential.
- **Expose a generic command or byte-stream service.** verified: ADR-0584 confines the authority to
  a closed provider protocol and explicitly denies generic execution.
- **Replace AF_UNIX with TCP.** judgment: local deployments need no network exposure, and removing
  the existing route would break the accepted colocated deployment contract.
