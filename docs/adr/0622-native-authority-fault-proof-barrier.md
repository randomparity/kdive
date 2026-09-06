# 0622 — Native authority fault proof barrier

## Status

Accepted (2026-09-06)

## Context

The native authority carrier needs deterministic evidence for a provider effect interrupted before
the authority journals `provider-returned`. Timing a process restart cannot prove that interval.
The installed authority must not gain a general operator command or a new provider mutation path.

## Decision

When the explicit live-VM fault-proof option is enabled, the authority binds one fixed mode-`0600`
AF_UNIX socket at `/run/kdive/provider-authority/proof-control/control.sock` in its owner-only
proof runtime directory. Linux `SO_PEERCRED` admits only uid 0. Its bounded closed JSON protocol
has only `arm`, `release`, and `status`: an arm names one existing System UUID, its exact Run UUID,
one existing authority operation, and either `before-provider` or `after-provider`. Only one arm
exists at a time. The sibling directory deliberately does not overlap the transient unprivileged
client-proof material.

The authority service awaits a matching arm immediately before the adapter commit and immediately
after that commit, before it anchors `provider-returned`. The listener never selects a provider,
filesystem path, command, request, or destination, and never performs a mutation. Service shutdown
aborts a waiting checkpoint; it does not release it or imply that the provider effect completed.

## Consequences

The native x86_64 carrier can restart or take over a precisely paused authority operation and
assert the durable recovery path without timing luck. The socket is absent by default,
inaccessible to workers, and is an explicit operator proof surface rather than a product control
API. It does not itself create journal-loss or stale-write evidence.

The native carrier may use separate root-only, fixed-target helpers to stop or continue one
fixed worker and temporarily rename the selected System's authority-owned journal lane. Those
helpers retain and verify exact inode and content-digest evidence before restoring the lane. The
held lane lives in a fixed authority-owned directory outside the inventoried journal root, and the
carrier stops a failed or auto-restarting authority before restoring it. They do not expose
credentials, accept paths, or add an authority listener or mutation API.

## Considered & rejected

- **Restart the service at an arbitrary time.** It cannot establish the interval between a
  provider effect and its journal evidence.
- **Expose generic provider pause or shell execution.** judgment: caller-selected execution or
  destinations would widen the authority boundary beyond a native proof seam.
- **Release a waiting checkpoint during shutdown.** verified: that can run the provider commit
  after the operator requested service stop, so it cannot distinguish abort from completion.
