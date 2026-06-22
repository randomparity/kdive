# ADR 0209 — Capability-aware MCP admission and profile-resolved tool defaults

- **Status:** Accepted
- **Date:** 2026-06-22
- **Deciders:** kdive maintainers
- **Issue:** M2.8 A2
- **Builds on:** [ADR-0208](0208-provider-capability-descriptor.md) (the descriptor this consults),
  [ADR-0174](0174-config-error-actionable-detail.md) (the actionable `data.reason` /
  `accepted_values` `CONFIGURATION_ERROR` detail pattern this reuses),
  [ADR-0097](0097-not-found-conflict-error-categories.md) (map to existing categories, invent none).
- **Spec:** [`../superpowers/specs/2026-06-22-local-libvirt-service-parity-honesty.md`](../superpowers/specs/2026-06-22-local-libvirt-service-parity-honesty.md)

## Context

ADR-0208 gives every `ProviderRuntime` a declarative capability descriptor. But a descriptor no
one enforces only makes the surface *visible*-honest (via `resources.describe`); it does not stop
an agent from invoking a plane the bound provider cannot serve. Today, when an agent calls a tool
backed by a stubbed local-libvirt plane, the call **passes every admission check**, enqueues a
durable job (or runs a synchronous op), and only **then** fails — asynchronously, with a
`MISSING_DEPENDENCY` whose message names "the live_vm gate," a test-harness concept meaningless
to the caller. Two tools are actively worse than silent:

- `vmcore.fetch` **defaults its method to `HOST_DUMP`** (`mcp/tools/lifecycle/vmcore.py`), and
  local-libvirt advertises `HOST_DUMP` in `supported_capture_methods` while its
  `_real_host_dump_capture` seam is a stub. So the *obvious* call — `vmcore.fetch` with no method
  — passes the supported-methods guard, returns `{job_id, running}`, then fails the job. The
  surface's own default steers the agent into the gap.
- `debug.start_session`, `introspect.from_vmcore`, and `introspect.run` carry no capability
  signal at all; the agent learns the plane is unavailable only by invoking and reading the deep
  error.

The descriptor must be *enforced* at the tool boundary, and the steering default must go.

## Decision

Make the MCP admission layer **capability-aware**: consult the bound runtime's ADR-0208
descriptor and reject an unsupported plane/method **before** enqueueing a job or running a
synchronous op, with an actionable `configuration_error`; and make `vmcore.fetch`'s default
**profile-resolved** (through the existing `capture_method(profile)` seam) so the surface never
steers a caller into an unsupported method.

### 1. Fail fast, before enqueue, with actionable detail

`debug.start_session`, `introspect.from_vmcore`, `introspect.run`, and `vmcore.fetch` resolve the
bound `ProviderRuntime` and check the requested plane/method against its descriptor at the start
of the handler — on the same pre-grant/pre-enqueue path that already resolves the binding. On a
miss, raise `CONFIGURATION_ERROR` with ADR-0166-style detail:

```
data: {
  "reason": "capability_unsupported",
  "capability": "debug_transport:gdbstub" | "introspection:live" | "capture_method:host_dump",
  "provider": "local-libvirt",
  "supported": [...the bound provider's supported set for that plane...]
}
```

The message names the plane, the provider, and the supported alternative (e.g. "local-libvirt
supports capture method KDUMP; HOST_DUMP is not available on this provider"). No job row is
created, no synchronous seam is touched — the failure is immediate and self-correcting, the
opposite of today's deferred `MISSING_DEPENDENCY`.

### 2. `vmcore.fetch` loses its static `HOST_DUMP` default — resolved via the existing profile seam

The `method: CaptureMethod = CaptureMethod.HOST_DUMP` default is removed. The method becomes
**profile-resolved through the seam that already owns this decision**: the providers already
implement `ProfilePolicy.capture_method(profile)` (`providers/core/runtime.py`), which returns the
crash-capture method the System's profile enables (local: `KDUMP` for a crashkernel profile,
`HOST_DUMP` for `preserve_on_crash`, `GDBSTUB`/`CONSOLE` otherwise). A new flat provider-level
"default method" field is **deliberately not introduced** — it would duplicate and could
contradict `capture_method(profile)` (a flat `KDUMP` is wrong for a local System provisioned
`preserve_on_crash`).

When the caller omits `method`, `vmcore.fetch` resolves it as `capture_method(profile)` **clamped
to the core-producing methods** (`{KDUMP, HOST_DUMP}` — the existing `_VMCORE_METHODS`, since
`CONSOLE`/`GDBSTUB` produce no core). If the profile's method is core-producing and in the
descriptor's `supported_capture_methods`, that is the default; if it is non-core (e.g. a
console-only System), there is no valid implicit core method, so the tool requires an explicit
core-producing `method` and otherwise returns the rule-1 `configuration_error`. An explicitly
supplied method is validated against the descriptor (rule 1). The surface never carries a
hard-coded method that a given provider cannot honor, and the per-System resolution stays in the
one seam that already encodes it.

### 3. Capability rejection is `CONFIGURATION_ERROR`, not a new category

A request for a plane the bound provider does not support is a caller/configuration mismatch, not
an infrastructure fault — it maps to the existing `CONFIGURATION_ERROR`, enriched with the detail
above (ADR-0174), not a new `ErrorCategory`. `MISSING_DEPENDENCY` is **retired from this path**:
after M2.8 it means a genuinely absent host dependency at runtime (an unimported `drgn`/`guestfs`
on a provider that *does* support the plane), never "this provider never wired this seam."

### 4. The check is provider-neutral

The admission code reads `runtime.supported_debug_transports` / `runtime.supported_introspection`
/ `runtime.supported_capture_methods` and never branches on `ResourceKind`. The same handler
admits a remote-libvirt System (which supports the plane) and rejects a local-libvirt System
(which does not, until its Epic B plane lands) by reading data, not by knowing the provider.

## Consequences

- An agent that calls an unsupported plane gets an immediate, actionable `configuration_error`
  naming the supported alternative — it can self-correct in one turn instead of polling a job to
  a cryptic terminal failure.
- The `vmcore.fetch` "default steers into the gap" bug is gone: omitting the method does the right
  thing for the bound provider; supplying an unsupported one is rejected up front.
- As each Epic B plane lands and flips its descriptor field, the *same* admission code begins
  admitting that plane on local with no tool change — enforcement and capability stay in lockstep
  through ADR-0208's descriptor.
- The change is confined to the four tools' handlers plus the `vmcore.fetch` signature; no port,
  schema, or new error category. The generated tool reference and `test_tool_docs` regenerate for
  the signature/maturity change.

## Considered & rejected

- **Let the job fail with `MISSING_DEPENDENCY` as today, just improve the message.** Rejected: a
  deferred async failure forces a poll cycle for a condition knowable synchronously at call time,
  and `MISSING_DEPENDENCY` mis-categorizes a static capability mismatch as a runtime dependency
  fault. Fail fast, correct category.
- **Keep `HOST_DUMP` as the `vmcore.fetch` default and rely on the new admission to reject it on
  local.** Rejected: a default that is *defined to be rejected on the default provider* is a
  surface that documents the wrong thing. A provider-resolved default is correct for every
  provider with no special-casing.
- **A new `CAPABILITY_UNSUPPORTED` error category.** Rejected: it is a configuration mismatch;
  ADR-0097 discipline is to map to the most specific existing category and carry the specificity
  in `data`, not to mint strings. `CONFIGURATION_ERROR` + `reason: capability_unsupported` does
  exactly that.
- **A flat `default_capture_method` field on the descriptor (ADR-0208).** Rejected: it duplicates
  the existing `ProfilePolicy.capture_method(profile)`, which already resolves the per-System
  capture method from the profile, and a provider-flat value would contradict it (a flat `KDUMP`
  is wrong for a local System provisioned `preserve_on_crash` → `HOST_DUMP`). Resolve the omitted
  `vmcore.fetch` method through `capture_method(profile)` clamped to the core-producing set
  instead of adding a redundant field.
- **Enforce in a shared middleware wrapping every tool.** Rejected as premature: only four tools
  are provider-plane-gated, and each needs a plane-specific capability key; an explicit check in
  each handler is clearer than a generic interceptor inferring the plane from the tool name. Can
  be lifted into a helper if the set grows.
