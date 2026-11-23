# 0849 — Retire the unused Compose inspect proxy

## Status

Proposed

## Context

Compose starts an HTTP inspect proxy as root with a Docker socket mount and makes the server
depend on it. Build-use recovery instead requires the exact holder's durable termination row
([ADR-0533](0533-role-separated-worker-fence-evidence.md) and the recovery amendment in
[ADR-0722](0722-tool-cell-exposure-configuration-and-rejection-evidence.md)). The operator-side
`WorkerLifecycleGate` obtains its retained container evidence through injected local Docker
inspection. It does not use the HTTP proxy.

## Decision

Remove the proxy service, its private network, server dependency and endpoint override. Remove
the proxy-only HTTP implementation and executable entry point. Retain `WorkerLifecycleGate`
at its existing import path, the Docker marker constructor, and the legacy endpoint setting
and validation. These compatibility surfaces do not supply termination evidence.

## Consequences

The reference graph needs no inspect listener or socket-mounted proxy container. Recovery
registration and exact-holder witness enforcement remain. Existing supported upgrades use
`just compose-stop` before selecting the new configuration and image; its gated teardown
removes orphans and preserves named volumes. This does not authorize protocol-3 upgrades.

## Considered & rejected

- **Keep the unused proxy.** verified: on Linux x86_64 with Compose 5.6 at `cac476335`,
  `docker compose -f docker-compose.yml --profile managed-worker config --format json` includes
  the root proxy and its socket mount. ADR-0722's recovery amendment identifies the durable
  witness as the active proof; the proxy serves no recovery caller.
- **Remove the marker endpoint contract too.** judgment: unrelated API churn is unnecessary for
  retiring the service. Preserve the existing constructor and endpoint validation.
- **Replace the lifecycle gate with direct teardown.** verified: ADR-0533 requires registration and
  retained terminal evidence before container removal. Removing that gate would weaken recovery.
