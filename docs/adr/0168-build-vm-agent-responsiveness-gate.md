# ADR 0168 — An active guest-ping gate makes a build-VM agent ready, and a post-readiness code 86 is deterministic

- **Status:** Accepted <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-06-18
- **Deciders:** KDIVE maintainers
- **Builds on (does not supersede):** [ADR-0100](0100-ephemeral-libvirt-build-vm.md) (the
  ephemeral build VM whose readiness this strengthens),
  [ADR-0159](0159-guest-agent-deterministic-failure-classification.md) (the guest-agent error
  classifier this refines per-context), and
  [ADR-0167](0167-diagnostics-ephemeral-buildhost-agent-check.md) (the build-host agent
  diagnostic whose verdict mapping this updates).

## Context

Every ephemeral-libvirt build on the remote-libvirt deployment fails with libvirt code
`86 = VIR_ERR_AGENT_UNRESPONSIVE` ("QEMU guest agent is not connected"), deterministically (4/4),
on two images — including `fedora-kdive-remote-base-43.qcow2`, the same image whose agent works
fine for target *provisioning* (issue #552). The defect is in build-time agent readiness, not the
image.

`EphemeralBuildVm.session` provisions the build VM, calls `wait_for_agent`
(`lifecycle/readiness.py`), then *immediately* runs an in-guest network probe through
`GuestExecBuildTransport`. `wait_for_agent` polls the live domain XML until the guest-agent
channel reaches `state="connected"`. That state flips when the guest **opens the virtio-serial
port** (early in boot), not when the `qemu-guest-agent` daemon starts answering commands. The
first `guest-exec` races into the window between the two and gets code 86.

The provisioning path uses the identical `wait_for_agent` and works only because it does not exec
immediately: the first real `guest-exec` is a later, separate worker job, by which time the agent
daemon is up. The XML channel state is necessary but not sufficient as a readiness signal; the
build path is the one caller that exposes the gap.

ADR-0159 made `_classify_libvirt_error` map code 86 → `TRANSPORT_FAILURE` (`retryable=true`) and
**explicitly rejected** treating it as deterministic, because without a readiness gate code 86
covers a configured-but-mid-reconnect agent a bare retry can clear. That classifier is the single
choke point shared by the build, install, retrieve, and debug planes, so its global contract must
not change for the planes that have no active readiness gate. `retryable` is a pure function of
`error_category` (ADR-0118): `PROVISIONING_FAILURE` and `TRANSPORT_FAILURE` are `retryable=true`;
`CONFIGURATION_ERROR` is `retryable=false`.

## Decision

Two coupled changes, both scoped to the build path:

**1. An active guest-ping readiness gate.** Add `wait_for_agent_responsive` to
`lifecycle/readiness.py`: poll `{"execute":"guest-ping"}` through the injected `agent_command`
until the agent answers (the call returns without a `libvirtError`).
`EphemeralBuildVm.session` calls it immediately after `wait_for_agent`, before using the
transport. `wait_for_agent` is kept as a cheap pre-check that also detects "domain exited during
boot" — a distinct, faster failure with a distinct category.

The gate classifies a `libvirtError` with the **base** ADR-0159 deterministic set (no code 86):

| Outcome | Meaning | Action |
|---|---|---|
| call returns | agent answered guest-ping | ready — proceed |
| base deterministic code (`ARGUMENT_UNSUPPORTED`, `ACCESS_DENIED`, `OPERATION_DENIED`, `NO_SUPPORT`, `OPERATION_UNSUPPORTED`, `CONFIG_UNSUPPORTED`) | agent not configured / denied | raise `CONFIGURATION_ERROR` now (polling cannot clear it) |
| code 86 / bare drop / other transient | agent mid-boot | keep polling |
| deadline reached | agent never became responsive | raise `CONFIGURATION_ERROR` (`retryable=false`) with `agent_readiness="unresponsive"` |

So a healthy-but-slow agent is waited for; a permanently-broken agent fails **non-retryable** with
an actionable message rather than a misleading retryable one.

**2. The build transport treats a post-readiness code 86 as deterministic.** Because the build
transport runs only *after* the gate confirmed the agent answers, a subsequent code 86 is no
longer the mid-boot transient ADR-0159 protects — it is a deterministic dead-agent condition.
`GuestAgentExec.__init__` gains a `deterministic_codes` parameter (default: the base set);
`_classify_libvirt_error` is extracted to `classify_agent_libvirt_error(domain, exc, *,
deterministic_codes)`. `GuestExecBuildTransport` builds its per-call exec with
`BUILD_DETERMINISTIC_CONFIG_CODES = base | {VIR_ERR_AGENT_UNRESPONSIVE}`. Every other consumer
keeps the default base set, so ADR-0159's global contract is unchanged.

**3. Diagnostic verdict mapping.** The ADR-0167 build-host agent diagnostic maps a pre-yield
`PROVISIONING_FAILURE` (channel never connected) to `AGENT_UNREACHABLE` (FAIL). The new gate adds a
second "agent never usable" shape: a `CONFIGURATION_ERROR` carrying `agent_readiness="unresponsive"`.
`_blocking_probe` maps that marker to `AGENT_UNREACHABLE` too, so an unresponsive agent surfaces as
the operator-actionable FAIL the diagnostic exists to report (#544). An unmarked
`CONFIGURATION_ERROR` (absent pool / base image) stays `HOST_UNREACHABLE`.

This is a per-context refinement of ADR-0159, **not** a reversal: the global classifier default and
its rejection of treating code 86 as deterministic stand for every plane without an active
readiness gate. Only the build path — which now has that gate as a precondition — narrows code 86.

## Consequences

- **No new field, column, schema, or migration.** A new readiness helper, a constructor parameter
  with a backward-compatible default, an extracted classifier function, and one diagnostic branch.
- **The build path now waits for an answering agent.** A healthy image whose agent is slow to come
  up now builds instead of failing on the first exec; a broken image fails fast and
  **non-retryable** with an actionable message and the `agent_readiness` marker.
- **Failure-contract change, scoped to the build transport.** A post-readiness code 86 on the
  build path is now `configuration_error`/`retryable=false`. The same code 86 on install,
  retrieve, or debug is unchanged (`transport_failure`/`retryable=true`).
- **The `wait_network=False` diagnostic gains a real responsiveness check.** It now exercises
  guest-ping, so it fails an agent that opens the channel but never answers — exactly Part A's
  condition — without burning a build.
- **Two sequential readiness gates on the build path.** `wait_for_agent` (XML) then
  `wait_for_agent_responsive` (ping). The extra cost is one ping round-trip on the happy path; the
  benefit is attributable failure points (channel-never-connected vs agent-never-answered).

## Alternatives considered

- **Add code 86 to the global `_DETERMINISTIC_CONFIG_CODES` set.** Rejected: it reverses an
  accepted decision (ADR-0159) in place and re-breaks the install/retrieve/debug planes, which
  have no readiness gate and can legitimately see a transient code 86 a retry clears. The
  per-context (`deterministic_codes` parameter) approach narrows code 86 only where a readiness
  gate makes it deterministic.
- **Make the readiness-gate timeout raise `PROVISIONING_FAILURE`** to match `wait_for_agent` and
  avoid touching the diagnostic. Rejected: `PROVISIONING_FAILURE` is `retryable=true`, which fails
  the issue's requirement that a never-ready agent be non-retryable. The honest non-retryable
  category is `CONFIGURATION_ERROR` (the build image's agent is misconfigured/broken); the
  diagnostic is updated to recognize it.
- **Probe readiness with a trivial `guest-exec` (e.g. `/bin/true`) instead of `guest-ping`.**
  Rejected: `guest-exec` is the heavier two-phase spawn/poll protocol and depends on an allowlisted
  program; `guest-ping` is libvirt's canonical agent-liveness command, single round-trip, no
  in-guest program required.
- **Drop `wait_for_agent` and rely solely on the ping gate.** Rejected: `wait_for_agent` also
  detects a domain that exits during boot (a distinct `provisioning_failure`) and gives a faster,
  cheaper first signal; keeping both yields precise failure attribution for one ping round-trip.
- **Retry code 86 inside the build transport instead of a pre-yield gate.** Rejected: it spreads
  readiness logic across every transport call and muddies the post-readiness deterministic signal
  that Part B relies on. A single pre-yield gate is the clean seam.
</content>
