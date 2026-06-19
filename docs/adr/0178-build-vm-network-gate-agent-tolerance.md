# ADR 0178 — The build-VM network-readiness gate tolerates a transient agent drop

- **Status:** Accepted
- **Date:** 2026-06-18
- **Deciders:** KDIVE maintainers

## Context

The ephemeral-libvirt build path boots a throwaway build VM and, before cloning the kernel,
runs two readiness gates over the qemu-guest-agent (`EphemeralBuildVm.session`): first
`wait_for_agent` polls `guest-ping` until the agent answers, then `_wait_for_network` runs an
in-guest default-route probe until the guest has network.

The build transport classifies agent errors with `BUILD_DETERMINISTIC_CONFIG_CODES`, which adds
`VIR_ERR_AGENT_UNRESPONSIVE` (libvirt code 86) to the deterministic-config set (ADR-0168): once
the agent has answered, a later code-86 during the *build* is a permanent failure, not a
retryable blip, so the build fails fast with an actionable `configuration_error` instead of
hanging.

`_wait_for_network` runs *between* the two — after the agent is confirmed responsive but while
the guest is still bringing its network up. On a real Fedora build image, NetworkManager brings
the interface up ~50–60 s into boot, and while it does so it briefly churns the agent's
virtio-serial channel, so a code-86 surfaces mid-probe. Because the network probe ran through the
build transport's classification, that transient code-86 was raised as the same fatal
`configuration_error` ("qemu-guest-agent is not usable on this build host"), aborting the build
before the network ever came up. The 120 s network deadline was never reached — one agent blip
killed the run. The failure was intermittent (a run whose probe timing missed the churn window
succeeded), which is exactly the live #572 coverage-campaign symptom (#584).

## Decision

The network-readiness gate treats a transient agent-channel drop as "not ready, keep polling"
within its existing deadline, rather than fatal. `_wait_for_network`'s probe catches
`CategorizedError` and, via `_is_transient_agent_drop`, returns `False` (retry) when the error is
a `TRANSPORT_FAILURE` (a bare/transport-level drop) or a `CONFIGURATION_ERROR` whose
`libvirt_error_code` is `VIR_ERR_AGENT_UNRESPONSIVE`. Any other deterministic config code (agent
not installed, command denied) still propagates immediately. On the deadline, the timeout detail
surfaces the last agent drop (when no route result was ever observed) so a genuinely wedged agent
remains diagnosable.

This scopes the code-86 relaxation to the network gate only; the build phase keeps ADR-0168's
fail-fast classification unchanged.

## Consequences

- A build VM whose agent blips while NetworkManager brings the interface up no longer fails the
  build; it polls until the route appears or the 120 s network deadline elapses.
- A persistent agent problem during the network gate still fails, now after the deadline with the
  last drop surfaced — bounded, not fast, but diagnosable.
- The build phase's code-86 = fatal contract (ADR-0168) is untouched; only the network gate, an
  inherently agent-churning window, is relaxed.

## Considered & rejected

- **Global-flip code 86 to transient everywhere.** Rejected: code 86 during the actual build is a
  real permanent failure (ADR-0168) across four planes with a committed test; flipping it
  globally reintroduces the hang ADR-0168 fixed.
- **Lengthen the network deadline.** Rejected: the deadline was never the limit — one blip aborted
  the run well inside 120 s. More time without tolerating the blip changes nothing.
- **Make the build base image network come up faster.** A real improvement (and worth doing), but
  it only shrinks the churn window; it does not remove the misclassification, so a blip could
  still abort a build. Orthogonal to this fix.
