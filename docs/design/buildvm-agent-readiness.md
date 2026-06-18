# Build-VM guest-agent readiness and post-readiness code-86 classification

- **Status:** Draft
- **Issue:** [#552](https://github.com/randomparity/kdive/issues/552)
- **ADR:** [ADR-0168](../adr/0168-build-vm-agent-responsiveness-gate.md)
- **Builds on:** [ADR-0100](../adr/0100-ephemeral-libvirt-build-vm.md) (the ephemeral build VM),
  [ADR-0159](../adr/0159-guest-agent-deterministic-failure-classification.md) (the guest-agent
  error classifier this refines, does not supersede), and
  [ADR-0167](../adr/0167-diagnostics-ephemeral-buildhost-agent-check.md) (the build-host agent
  diagnostic whose verdict mapping this updates).

## Problem

No MCP-only build path reaches a booted kernel on the remote-libvirt deployment: every
ephemeral-libvirt build fails with the qemu-guest-agent never connecting (libvirt code
`86 = VIR_ERR_AGENT_UNRESPONSIVE`, "QEMU guest agent is not connected"). The failure is
deterministic — 4/4 across two images, including the `fedora-kdive-remote-base-43.qcow2` image
whose agent works fine for *target provisioning*. So the defect is in build-time agent
readiness, not the image.

Two coupled defects:

### Part A — the build path execs before the agent answers

`EphemeralBuildVm.session` (`lifecycle/build_vm.py`) provisions the build VM, calls
`wait_for_agent` (`lifecycle/readiness.py`), then *immediately* runs an in-guest network-readiness
probe through `GuestExecBuildTransport`. `wait_for_agent` polls the live domain XML until the
guest-agent channel reports `state="connected"`. That state flips when the **guest opens the
virtio-serial port** — which the kernel does early in boot — but the `qemu-guest-agent` daemon
is not yet answering commands. The first `guest-exec` therefore races into the window between
"channel connected" and "agent responsive" and gets code 86.

The *provisioning* path uses the identical `wait_for_agent`, but it does **not** exec
immediately: it returns the domain name, and the first real `guest-exec` happens in a later,
separate worker job (Install/Connect), by which time the agent daemon is long up. So the same
XML-only readiness check is sufficient there and insufficient in the build path. The XML channel
state is a necessary but not sufficient readiness signal.

The guest-agent virtio-serial channel is configured **identically** in the build domain
(`render_build_domain_xml`, `lifecycle/build_vm.py`) and the provisioning domain
(`render_domain_xml`, `lifecycle/xml.py`) — both attach `<channel type="unix">` with
`<target type="virtio" name="org.qemu.guest_agent.0"/>`. The build domain omits only the serial
console, `vmcoreinfo`, and the gdbstub `<qemu:commandline>` — none of which carry the agent
channel. So the residual difference between the two paths is **timing of the first command**, not
agent-channel configuration.

For an image whose agent *would* come up (the Fedora base), the fix is to wait until the agent
actually answers before execing. For an image whose agent never comes up (the stock
`ub24-big-build`, per the ADR-0159 campaign), the wait must terminate with an honest,
non-retryable failure rather than a misleading retryable one.

### Root-cause hypothesis and its limits

The hypothesis is that the build-domain agent comes up shortly after the channel reports
connected (a race the gate waits out). This is consistent with — but not proven by — the
evidence that the same image's agent answers in the provisioning domain. The unit tests below use
injected agent fakes and therefore **cannot** prove Goal #1 ("reaches a produced kernel
artifact"); that goal is validated only by an **operator live build run** on the remote-libvirt
deployment (recorded in the PR as an operator-TODO), not by CI.

Fallback guarantee: even if the hypothesis is wrong and the build-domain agent *never* answers
guest-ping, this change is still a strict improvement — the build fails fast and **non-retryable**
with an actionable message and the `agent_readiness` marker (Part A criterion #2) instead of the
current misleading `transport_failure`/`retryable:true`, and the diagnostic surfaces it as a FAIL
without burning a build. Goal #1 is the only criterion contingent on the hypothesis; criteria #2
and #3 hold regardless.

### Part B — a post-readiness code 86 is reported retryable

`GuestAgentExec._agent` (`guest/agent.py`) is the single choke point for guest-agent round-trips.
ADR-0159 subcategorizes the libvirt error by `get_error_code()`: a fixed deterministic set
(agent not configured / denied / unsupported) → `CONFIGURATION_ERROR` (`retryable=false`);
everything else, **including code 86**, → `TRANSPORT_FAILURE` (`retryable=true`). ADR-0159
*deliberately* left code 86 retryable because, without a readiness gate, it covers a
configured-but-mid-reconnect agent a bare retry can clear.

Once Part A guarantees the build transport runs only *after* the agent has answered a probe, a
subsequent code 86 on the build path is no longer that transient — it is a deterministic
dead-agent condition, and reporting it `retryable=true` invites wasted whole-build retries.

## Goals / non-goals

**Goals**
- An ephemeral-libvirt build reaches a produced kernel artifact on a healthy build image.
- A build whose agent never becomes ready within the readiness window fails **non-retryable**
  with an actionable message — not `transport_failure`/`retryable:true`.
- A regression test pins the classification of a post-readiness code-86 failure.

**Non-goals**
- Changing the global guest-agent classifier for the install/retrieve/debug planes. Those have
  no active readiness gate; ADR-0159's reasoning (code 86 there can be a transient a retry
  clears) still holds, so their default is unchanged.
- A new field, column, schema, or migration. This is a classifier/readiness change, like
  ADR-0159.
- Provision/admission-time preflight of a never-buildable host (#544/ADR-0167 is complementary;
  this design only makes its verdict track the new failure shape).

## Design

### Part A — an active guest-ping readiness gate

Add `wait_for_agent_responsive` to `lifecycle/readiness.py`: a poll loop that issues the
qemu-guest-agent `{"execute":"guest-ping"}` command through the injected `agent_command` callable
until the agent answers (the call returns without a `libvirtError`).

- A `libvirtError` whose code names a **deterministic-config** condition (agent not configured /
  denied / unsupported — the ADR-0159 base set, *without* code 86) is raised immediately as
  `CONFIGURATION_ERROR`: polling cannot make an absent or denied channel answer.
- A `libvirtError` that is transient — **including code 86** and a bare drop — means "not ready
  yet, keep polling". During the readiness window code 86 is exactly the mid-boot transient
  ADR-0159 describes, so the gate absorbs it.
- On the deadline, raise `CONFIGURATION_ERROR` (`retryable=false`) with an actionable message
  ("the build image's qemu-guest-agent did not become responsive") and a stable detail marker
  (see "Cross-module marker" below).

`EphemeralBuildVm.session` calls `wait_for_agent_responsive` immediately after `wait_for_agent`
and before binding/using the transport. `wait_for_agent` is kept: it is a cheap pre-check that
also detects "domain exited during boot", a distinct, faster failure. The two gates report
distinct, attributable failure points (channel-never-connected vs agent-never-answered).

The gate runs regardless of `wait_network`, so the `wait_network=False` diagnostic path
(ADR-0167) gains a true agent-responsiveness check, not just an XML-state check.

**Timeout budget.** The gate's overall timeout and poll interval are carried on `BuildVmTiming`
as new fields `agent_responsive_timeout_s` (default 120.0) and `agent_responsive_poll_s`
(default 2.0), distinct from the XML `agent_timeout_s`/`agent_poll_s` so the two gates are tuned
and injected independently. Each guest-ping carries a **positive bounded** per-call timeout (a
fixed `_PING_CALL_TIMEOUT_S = 5`, never libvirt's blocking `-2`), so a wedged channel surfaces as
a `libvirtError` the loop classifies rather than blocking the worker thread. Worst-case agent
wait on the build path is `agent_timeout_s` (XML, ≤180s) + `agent_responsive_timeout_s` (ping,
≤120s); this runs inside the BUILD worker job and, on the diagnostic path, inside the
`asyncio.to_thread` whose orphaned thread the ADR-0167 reaper backstops — the gate adds no new
unbounded wait, only a second bounded one.

**Cross-module marker.** The deadline error's detail marker is a single exported constant pair in
`lifecycle/readiness.py` — `AGENT_READINESS_DETAIL_KEY = "agent_readiness"` and
`AGENT_UNRESPONSIVE = "unresponsive"` — imported by both the gate (raise site) and
`diagnostics/buildhost_agent.py` (read site), so there is one source of truth and a literal drift
is impossible. A regression test drives the **actual gate** to its deadline and asserts the
diagnostic maps the resulting error to `AGENT_UNREACHABLE`, so any change that breaks the contract
breaks the test.

### Part B — the build transport treats a post-readiness code 86 as deterministic

The build transport runs **only after** the session's readiness gate confirmed the agent answers.
So for the build transport — and only it — a subsequent code 86 is deterministic.

`GuestAgentExec.__init__` gains a `deterministic_codes: frozenset[int]` parameter defaulting to
the existing base set, and `_classify_libvirt_error` is extracted to a module function
`classify_agent_libvirt_error(domain, exc, *, deterministic_codes)`. `GuestExecBuildTransport`
constructs its per-call `GuestAgentExec` with `BUILD_DETERMINISTIC_CONFIG_CODES =
base | {VIR_ERR_AGENT_UNRESPONSIVE}`. Every other consumer (install, retrieve, debug) keeps the
default base set, so the global contract ADR-0159 set is unchanged.

Result: a post-readiness code 86 on the build path → `CONFIGURATION_ERROR` (`retryable=false`)
with the libvirt error string + code in `details`; the same code 86 on any other plane stays
`TRANSPORT_FAILURE` (`retryable=true`).

### Diagnostic verdict mapping (ADR-0167)

The build-host agent diagnostic classifies a `CategorizedError` escaping the session:
`PROVISIONING_FAILURE` (channel never connected) → `AGENT_UNREACHABLE` (FAIL), everything else →
`HOST_UNREACHABLE` (ERROR). The new gate adds a second "agent never usable" shape — a
`CONFIGURATION_ERROR` carrying the shared `AGENT_READINESS_DETAIL_KEY` marker. `_blocking_probe` is
updated to map that marker to `AGENT_UNREACHABLE` as well, so an unresponsive agent is surfaced as
the operator-actionable FAIL the diagnostic exists to report (#544), while a pool/base-image
`CONFIGURATION_ERROR` (no marker) stays `HOST_UNREACHABLE`.

## Failure-mode matrix

| Condition | Where | Category | retryable | Diagnostic verdict |
|---|---|---|---|---|
| Healthy agent, mid-boot delay | ping gate absorbs it | (proceeds) | — | AGENT_READY |
| Agent never answers ping (broken image) | ping gate deadline | `configuration_error` | false | AGENT_UNREACHABLE (FAIL) |
| Domain exits during boot | `wait_for_agent` | `provisioning_failure` | true | AGENT_UNREACHABLE (FAIL) |
| Code 86 on a real build command (post-readiness) | build transport | `configuration_error` | false | n/a (build job) |
| Code 86 on install/retrieve/debug | shared classifier (default) | `transport_failure` | true | n/a |
| Absent pool / base image | session setup | `configuration_error` | false | HOST_UNREACHABLE (ERROR) |

## Test plan

- **readiness:** `wait_for_agent_responsive` returns when the first ping answers; polls past
  transient code-86 / bare drops then returns when a later ping answers; raises
  `CONFIGURATION_ERROR` immediately on a deterministic-config code; raises non-retryable
  `CONFIGURATION_ERROR` with the `agent_readiness` marker on the deadline.
- **agent classifier:** the default exec still maps code 86 → `TRANSPORT_FAILURE` (pins
  ADR-0159 is intact); an exec built with `BUILD_DETERMINISTIC_CONFIG_CODES` maps code 86 →
  `CONFIGURATION_ERROR`; the base deterministic codes still map to `CONFIGURATION_ERROR` under
  both sets.
- **build transport:** a code-86 round-trip raises `CONFIGURATION_ERROR` (`retryable` derives
  false); a base deterministic code still raises `CONFIGURATION_ERROR`; a transient non-86 error
  still raises `TRANSPORT_FAILURE`.
- **build_vm session:** the session yields only after the ping gate answers; a never-responsive
  agent raises non-retryable `CONFIGURATION_ERROR` and still tears the VM down; an agent that
  answers ping then serves the route probe yields as before.
- **diagnostic:** drive the **actual** `wait_for_agent_responsive` gate (via a session whose
  agent always raises code 86) to its deadline and assert `_blocking_probe` maps the resulting
  marked `CONFIGURATION_ERROR` to `AGENT_UNREACHABLE`; an unmarked `CONFIGURATION_ERROR` (absent
  base image) → `HOST_UNREACHABLE`.

## Rollback

Pure code change, no migration. Reverting the commits restores the prior (racy) behavior; no data
or schema cleanup is needed.
</content>
</invoke>
