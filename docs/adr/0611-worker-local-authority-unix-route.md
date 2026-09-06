# 0611 — Configure the worker-local authority route separately

## Status

Accepted (2026-09-06)

## Context

ADR-0606 binds a remote-libvirt Resource to a numeric IPv4 authority route. A worker deployed
alongside an authority needs the same closed framed protocol without accepting a request-selected
destination. The authority host already owns an AF_UNIX request socket, but host settings cannot
be imported as required worker configuration because the two processes have separate deployment
inputs.

## Decision

The worker has an optional all-or-none local authority binding: authority instance, one absolute
non-abstract AF_UNIX pathname, and server-CA, worker-certificate, and worker-key secret
references. These settings are worker-only and have no defaults. An entirely absent tuple disables
the local route; a partial tuple fails closed. The pathname must fit the AF_UNIX pathname limit.

The local sender reuses the closed authority envelope and typed sender. It borrows the active worker
credential only while encoding a request, resolves TLS material only for that call, uses TLS 1.3
and the configured authority server identity, and spends one absolute deadline across connect,
frame exchange, and shutdown. The sender accepts no socket, authority identity, or TLS input from a
job or request, and it never falls back to the remote Resource route.

## Consequences

Worker deployment must explicitly supply a local client tuple whose socket agrees with the
authority host's provisioned request socket. The AF_UNIX and remote IPv4 routes remain separate
closed configurations that share only TLS material loading and the authority protocol. #2214 owns
authority-server and worker assembly selection; this decision adds no listener or automatic route
advertisement.

## Considered & rejected

- **Reuse authority-host settings in the worker.** They carry host-required values into a separate
  process validation boundary and can silently enable a client route.
- **Permit a socket in the authority payload.** A caller-controlled local pathname would be a
  credential-forwarding destination boundary.
- **Fall back between local and remote routes.** A failed configured route must be observable; a
  fallback could send the active credential to a different authority.
