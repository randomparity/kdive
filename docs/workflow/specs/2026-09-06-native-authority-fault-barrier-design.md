# Native authority fault barrier design

## Scope

Issue #2151's installed x86_64 carrier needs deterministic restart/takeover evidence. The caller
authorized only a disabled-by-default authority proof socket, not a provider control API. The
barrier is described by [ADR-0622](../../adr/0622-native-authority-fault-proof-barrier.md).

## Contract

`live_vm_host_authority_fault_proof_enabled` defaults false. When true, deployment configures the
fixed `/run/kdive/provider-authority/proof-control/control.sock`; otherwise neither its directory
nor socket is created. The authority owns both, and the socket is mode `0600`. The sibling avoids
the transient client-proof directory, which has a distinct unprivileged owner and lifecycle.

The peer kernel credential must have uid zero. Each bounded JSON request is exactly one of:

- `arm` with an existing System UUID, its exact Run UUID, an `AuthorityOperation`, and
  `before-provider` or `after-provider`;
- `release` for the one armed checkpoint; or
- `status`.

Malformed, oversized, non-root, and concurrent arm requests are rejected. A mismatched service
checkpoint does not pause. An arm can block only its exact
`(System, Run, operation, checkpoint)` at the two service checkpoints. It neither creates a
provider call nor alters a request. Authority shutdown aborts the checkpoint and leaves the
ordinary recovery path to establish what, if anything, completed.

## Verification

Focused unit tests cover disabled construction, peer and frame rejection, bounded one-arm state,
exact checkpoint matching, checkpoint order, and shutdown abort. Deployment tests pin the default
off and conditional write paths. The native carrier uses the socket only after its usual revision,
fixture, and confinement gates; unsupported journal-loss or stale-write actions remain loud
failures until their exact operator-safe mechanism exists.
