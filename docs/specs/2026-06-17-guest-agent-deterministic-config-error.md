# Deterministic guest-agent failure classified as a configuration error (#531)

- **Status:** Draft
- **Date:** 2026-06-17
- **ADR:** [0157](../adr/0157-guest-agent-deterministic-failure-classification.md)
- **Issue:** [#531](https://github.com/randomparity/kdive/issues/531)

## Problem

On an ephemeral remote build host, `runs.build` fails with
`transport_failure: qemu-guest-agent command failed (agent unreachable or not connected)`
and the envelope reports `retryable: true`. The failure is **deterministic** (the build
image's qemu-guest-agent is permanently unreachable; reproduced 2/2 against a freshly
provisioned builder), so the `retryable` flag is wrong and invites an agent to burn retry
cycles on a failure that can never clear.

`retryable` is a pure function of `error_category` (ADR-0118): `TRANSPORT_FAILURE → true`,
`CONFIGURATION_ERROR → false`, derived once in `ToolResponse` and never caller-set
(`src/kdive/mcp/responses.py`, `_RETRYABLE_BY_CATEGORY`). The category a raise site picks
**is** the retry decision.

The single choke point for every qemu-guest-agent round-trip — `GuestAgentExec._agent`
(`src/kdive/providers/remote_libvirt/guest/agent.py`, the `except libvirt.libvirtError`
branch) — raises one category, `TRANSPORT_FAILURE`, for **every** libvirt error. A
permanently-broken agent (channel absent from the domain XML, image missing `qemu-ga`,
permission denied) is therefore indistinguishable from a transient channel drop.

## Acceptance criteria (from the issue)

- A deterministic guest-agent failure (agent not installed / channel not present /
  permission denied) returns `configuration_error` (`retryable=false`), **not**
  `transport_failure` (`retryable=true`).
- A genuinely transient channel drop still returns `transport_failure` (`retryable=true`).
- The underlying libvirt error string is included in `details` so the distinction is
  auditable.

## Design

See ADR-0157 for the decision and rejected alternatives. In summary:

1. **Classify by `get_error_code()` at the `_agent` raise site.** The existing
   `except libvirt.libvirtError as exc` branch reads `exc.get_error_code()` and routes:
   - **Deterministic →** `CONFIGURATION_ERROR`. Code set (module-level frozenset constant):
     `VIR_ERR_ARGUMENT_UNSUPPORTED` ("QEMU guest agent is not configured"),
     `VIR_ERR_ACCESS_DENIED`, `VIR_ERR_OPERATION_DENIED`, `VIR_ERR_NO_SUPPORT`,
     `VIR_ERR_OPERATION_UNSUPPORTED`, `VIR_ERR_CONFIG_UNSUPPORTED`.
   - **Everything else →** `TRANSPORT_FAILURE` (unchanged behavior), including
     `VIR_ERR_AGENT_UNRESPONSIVE`, `VIR_ERR_AGENT_COMMAND_TIMEOUT`, `VIR_ERR_AGENT_UNSYNCED`,
     and a libvirtError with no live code (`get_error_code() == VIR_ERR_OK`, which is the
     shape a bare `libvirt.libvirtError("msg")` carries).

2. **The message reflects the category.** The deterministic raise carries a message naming a
   build-host/agent configuration problem (e.g. "qemu-guest-agent is not usable on this build
   host (configuration)"); the transient raise keeps the existing "agent unreachable or not
   connected" wording. Neither category is in `_SUPPRESSED_DETAIL`, so the message reaches the
   envelope `detail`.

3. **`details` gains two auditable keys on both branches:** `libvirt_error` = the libvirt
   error string (`str(exc)`), `libvirt_error_code` = `exc.get_error_code()`. `domain` stays.
   These are hypervisor-side diagnostics, not kdive secret material; they pass through the
   response boundary's existing `safe_error_details` redaction unchanged (ADR-0078: the TLS
   client cert is consumed by the transport layer and never reaches this seam).

4. **No new error category, field, column, or migration.** `CONFIGURATION_ERROR` and
   `TRANSPORT_FAILURE` both already exist and already carry the correct `retryable` in
   `_RETRYABLE_BY_CATEGORY`. The change is one classifier branch in a pure helper.

5. **Scope held to the runtime classification contract.** The provisioning-time preflight
   angle (failing a never-buildable builder closed at admission, issue mentions #533) is
   complementary and out of scope here; a preflight cannot cover an agent that passes the
   readiness gate and then fails on first exec, so the runtime category must be honest
   regardless.

## Test plan (TDD)

Unit tests in `tests/providers/remote_libvirt/guest/test_guest_agent.py`, using the existing
`libvirt_error(code)` helper (`tests/providers/remote_libvirt/conftest.py`) to construct a
`libvirt.libvirtError` whose `get_error_code()` returns a chosen code:

- **Deterministic → `CONFIGURATION_ERROR`:** parametrized over each code in the deterministic
  set; assert `category is CONFIGURATION_ERROR` and `retryable=false` is what the category
  yields. Include `VIR_ERR_ARGUMENT_UNSUPPORTED` (the issue's "not configured" case) and
  `VIR_ERR_ACCESS_DENIED` (permission denied) explicitly.
- **Transient → `TRANSPORT_FAILURE`:** `VIR_ERR_AGENT_UNRESPONSIVE` and a no-code
  `libvirt.libvirtError("guest agent is not connected")` both stay `TRANSPORT_FAILURE`. The
  existing `test_agent_unreachable_maps_to_transport_failure` is preserved (no-code path).
- **`details` payload:** both branches carry `libvirt_error` (the error string) and
  `libvirt_error_code` (the numeric code) alongside `domain`.
- **Timeout path unchanged:** `_await_exit`'s in-guest timeout stays `TRANSPORT_FAILURE`
  (the command ran; the agent answered) — existing
  `test_run_times_out_when_the_command_never_exits` must still pass.

Edge cases: a code outside both sets falls through to `TRANSPORT_FAILURE` (conservative
default); an exception whose `get_error_code()` raises or returns `VIR_ERR_OK` maps to
`TRANSPORT_FAILURE` (no false terminal).

## Out of scope

- Provisioning/admission preflight of the build image's guest agent (#533).
- Any change to the readiness gate (`wait_for_agent`) or to `retryable`'s derivation.
- Broadening or centralizing libvirt-code→category mapping across the provider (ADR-0076
  keeps per-site classification).
