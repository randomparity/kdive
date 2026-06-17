# ADR 0157 — A deterministic guest-agent failure is a configuration error, not a retryable transport failure

- **Status:** Accepted <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-06-17
- **Deciders:** KDIVE maintainers
- **Builds on (does not supersede):** [ADR-0078](0078-object-store-in-target-install-seam.md)
  (the in-target `guest-exec`/`guest-exec-status` seam this refines) and
  [ADR-0118](0118-wait-on-resource-mechanisms.md) (the category→`retryable` table whose
  contract this change relies on).

## Context

`retryable` is a pure function of `error_category`, derived once in the response model
(`src/kdive/mcp/responses.py`, `_RETRYABLE_BY_CATEGORY`) and never caller-set — that is
ADR-0118's decision. `TRANSPORT_FAILURE` maps to `retryable=true`; `CONFIGURATION_ERROR`
maps to `retryable=false`. The flag's whole purpose (ADR-0118 / #430) is to stop an agent
hammering a permanent failure, so the category a raise site picks **is** the retry decision.

`GuestAgentExec._agent` (`src/kdive/providers/remote_libvirt/guest/agent.py`) is the single
choke point for every qemu-guest-agent round-trip. It catches `libvirt.libvirtError` and
raises one category — `TRANSPORT_FAILURE` — for **every** libvirt error, regardless of
whether the agent is transiently mid-reconnect or **permanently** absent/misconfigured:

```python
except libvirt.libvirtError as exc:
    raise CategorizedError(
        "qemu-guest-agent command failed (agent unreachable or not connected)",
        category=ErrorCategory.TRANSPORT_FAILURE,
        details={"domain": _domain_name(domain)},
    ) from exc
```

A live black-box build campaign on `ub24-big-build` (`kind = ephemeral_libvirt`) reproduced
a **deterministic** failure (2/2 against a freshly-provisioned builder): the build image's
qemu-guest-agent was permanently unreachable, yet `runs.build` returned `transport_failure`
with `retryable: true` (issue [#531](https://github.com/randomparity/kdive/issues/531)). The
misleading flag invites an agent to burn retry cycles on a failure that can never clear.

libvirt already distinguishes these conditions at the source. The qemu driver reports
`VIR_ERR_ARGUMENT_UNSUPPORTED` with "QEMU guest agent is not configured" when the agent
channel is absent from the domain (a permanent, domain-XML/image condition), and
`VIR_ERR_AGENT_UNRESPONSIVE` ("not running or not usable" / "not available due to an error")
when the agent is configured but not answering right now (a transient condition: mid-boot,
died, sync-timeout). Permission and capability errors (`VIR_ERR_ACCESS_DENIED`,
`VIR_ERR_OPERATION_DENIED`, `VIR_ERR_NO_SUPPORT`, `VIR_ERR_OPERATION_UNSUPPORTED`,
`VIR_ERR_CONFIG_UNSUPPORTED`) are likewise deterministic — re-invocation cannot clear them.

The readiness gate (`wait_for_agent`, `lifecycle/readiness.py`) already waits for the channel
to reach `state="connected"` before yielding the build transport, but it cannot prevent a
later first-exec failure or cover an image that simply lacks a working `qemu-ga`; the
misclassification at the exec seam is the in-scope defect.

## Decision

We will **subcategorize the libvirt error at the `_agent` raise site by its
`get_error_code()`**: a code that names a **deterministic** condition (agent not configured,
permission denied, operation/config unsupported) raises `CONFIGURATION_ERROR`
(`retryable=false`); every other libvirt error — including a bare error with no live error
code (`get_error_code()` returns `None` when the libvirtError has no `.err` tuple) — keeps
`TRANSPORT_FAILURE` (`retryable=true`), preserving the existing behavior for a genuinely
transient channel drop. The classifier is a single membership test against the deterministic
code set, so `None` and any unlisted code fall through to `TRANSPORT_FAILURE` with no special
case.

The deterministic set is:

| `VIR_ERR_*` code | meaning | category |
|---|---|---|
| `ARGUMENT_UNSUPPORTED` | "QEMU guest agent is not configured" (channel absent) | `CONFIGURATION_ERROR` |
| `ACCESS_DENIED`, `OPERATION_DENIED` | permission denied | `CONFIGURATION_ERROR` |
| `NO_SUPPORT`, `OPERATION_UNSUPPORTED`, `CONFIG_UNSUPPORTED` | host/op cannot run the agent command | `CONFIGURATION_ERROR` |
| anything else (incl. `AGENT_UNRESPONSIVE`, `AGENT_COMMAND_TIMEOUT`, `AGENT_UNSYNCED`, or `None`/no `.err`) | transient/unknown | `TRANSPORT_FAILURE` |

`AGENT_UNRESPONSIVE` stays `TRANSPORT_FAILURE` **on purpose**: it covers "configured but
mid-reconnect/died", which a bare retry can clear; ADR-0118's documented bias is terminal
only when transience is ambiguous, and here libvirt's own taxonomy resolves the ambiguity in
favor of transient.

The underlying libvirt error string is included in the raised error's `details` under
`libvirt_error`, and the numeric code under `libvirt_error_code`, so the
configuration-vs-transport distinction is auditable from the failure surface. This seam runs
only on the build/install **worker** path, so those `details` reach a client through the
worker's redaction seam (`worker._failure_context` runs every scalar detail value through the
`Redactor`, then `jobs.get`/`ToolResponse.from_job` surfaces the redacted
`job.failure_context`), not the synchronous `safe_error_details` path. The libvirt error
string is a hypervisor-side diagnostic carrying no kdive secret material at this seam (the TLS
client cert is consumed by the transport layer and never reaches the exec seam, per ADR-0078),
and the worker `Redactor` scrubs it on the way to persistence regardless.

## Consequences

- **No new field, column, schema, or migration.** The change is one classifier branch in a
  pure helper plus two `details` keys; every caller already propagates `CategorizedError`
  from `run()`, and `retryable` derives downstream from the category with no caller change.
- **Failure-contract change (the intended one).** A deterministic guest-agent failure now
  returns `configuration_error` / `retryable=false` instead of `transport_failure` /
  `retryable=true`. An agent that honored the old flag and retried a permanently-broken
  builder now correctly stops. A genuinely transient drop is unaffected.
- **`details` now carries the libvirt error string.** This is new surfaced text. It is a
  hypervisor diagnostic, not request/secret data; on the build/install worker path it is keyed
  into `job.failure_context` as `failure_detail_libvirt_error` only after the worker `Redactor`
  scrubs it, so it makes the classification auditable (which the issue asks for) without a new
  leak channel.
- **Conservative default preserves safety.** Any libvirt error this ADR does not name as
  deterministic — including a future code, a bare drop, or `AGENT_UNRESPONSIVE` — stays
  `TRANSPORT_FAILURE`. The change can only *narrow* retry on the specific deterministic codes,
  never widen a terminal misclassification onto a transient drop.
- **Provisioning-time preflight is out of scope.** Failing a never-buildable builder closed at
  provision/admission (issue mentions #533) is a separate, additive surface; this ADR fixes
  only the runtime classification contract so the existing `runs.build` failure is honest.

## Alternatives considered

- **Keep one category, add `retryable` as a raise-site argument.** Rejected: it reopens
  ADR-0118, which deliberately makes `retryable` a pure function of the category at ~15 sites
  to stop the flag drifting. The honest fix is to pick the *correct category*, which already
  carries the right `retryable`.
- **Treat `AGENT_UNRESPONSIVE` as deterministic too.** Rejected: libvirt raises it for a
  configured-but-not-currently-answering agent (mid-boot, transient death, sync timeout) —
  exactly the transient case a bare retry can clear. Mapping it terminal would re-break the
  retryable case the seam is meant to serve and would make a flaky-under-load channel (the
  ADR-0153 campaign's observed condition) read as permanent.
- **Parse the libvirt error *message* string instead of the code.** Rejected: the message is
  human-readable, localizable, and version-dependent; `get_error_code()` is the stable,
  machine-comparable signal the rest of the provider already keys on (`storage.py`,
  `control.py`, `provisioning.py`, `gdb.py` all branch on `get_error_code()`).
- **Centralize a libvirt-code→category mapper across the provider.** Rejected here as scope
  creep: ADR-0076 chose per-site classification with no shared layer, and the existing sites
  each map a small, site-specific code set. A guest-agent-local classifier matches that
  convention; a cross-provider mapper is a separate refactor an ADR can justify later.
- **Fix it only at provisioning preflight, leaving the runtime category as-is.** Rejected as
  insufficient: a preflight cannot cover an agent that passes the readiness gate and then
  fails on first exec, and the runtime envelope would still lie. Preflight is complementary
  (#533), not a substitute for an honest runtime category.
