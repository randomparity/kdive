# 0757 — System capture methods use the guest profile architecture

## Status

Accepted (2026-10-12)

## Context

Issue #3153 found explicit `vmcore.fetch(method="fadump")` accepts an x86_64
System because `_resolve_capture_method` checks the provider's static set and
returns before reading the profile. `ArchTraits.fadump_capture` is false for
x86_64 and true for ppc64le. Profile validation already restricts the fadump opt-in,
but an explicit capture request bypasses that path.

ADR-0349 decision 3 deliberately defines the provider descriptor as a superset.
`resources.describe.supported_capture_methods` projects it without a System.
An x86_64 host may support ppc64le guests; using the server or host architecture
would incorrectly remove their method. The existing `pseries_fadump` discovery
signal and provisioning gate answer host support, not a particular guest's arch.

`systems.get` already resolves the bound runtime without provider I/O and exposes
optional snapshot/traffic capabilities. It is the existing System-specific read
surface. No System-specific capture-method field exists today.

## Decision

Add optional `systems.get.data.supported_capture_methods`: a sorted list of the bound provider's
capture-method tokens restricted by the stored System profile architecture.
For an architecture whose `fadump_capture` is false, remove only `fadump`.
All other methods remain the provider's existing set; `vmcore.fetch` still accepts
only core-producing methods. This is compatibility information, not proof that a
kernel, guest image, host firmware or current System state is capture-ready.

Use one small pure domain selector for this architecture rule and capture admission.
Never derive guest architecture from `platform.machine`, Resource host CPU, current
server, or accelerator. Preserve the ppc64le method under both KVM and TCG profile
contexts; this does not claim successful native or emulated fadump capture.

Explicit fadump admission on a parsed x86_64 System returns
`configuration_error` with `data.reason=capability_unsupported` and the architecture-compatible supported set, before
kernel/rootfs checks, activation lookup, keyed mutation or enqueue. An invalid
profile on this path returns the existing typed configuration error.
Implicit method resolution uses the same architecture-compatible set after its
existing profile parse. Explicit non-fadump paths keep their existing behavior;
unknown strings and non-core methods retain their existing validation precedence.
No new opt-in, crashkernel or host-readiness gate is added by this change.

Keep `resources.describe` and provider descriptors unchanged as the provider-wide
superset under ADR-0349. Document that distinction in the agent-visible resource
wrapper, and direct callers to `systems.get` for the System-specific list. Update
both the `systems.get` wrapper and `vmcore.fetch` wrapper/parameter description.
This extends discovery without changing the accepted resource-level decision.

The new System field is absent when the provider is unavailable or its stored
profile cannot be parsed, meaning unknown, not supported or unsupported. Retain
existing System metadata and independently known snapshot/traffic fields in that
case. A known empty provider set is emitted as an empty list. Do not add the field
to `systems.list`, which does not resolve runtimes per row.

## Consequences

This is an additive response field on an existing MCP tool; the operator approved
the interface on 2026-10-12 before implementation. No new tool, parameter, persisted
column, migration or remote provider policy. Existing resource clients retain the
same superset. The narrower System list and architecture refusal share the rule,
so they cannot drift through independently coded architecture branches.

The list does not guarantee a capture succeeds: current-state, core-producing,
kernel/image, activation and existing host-provisioning gates still apply.
Native POWER qualification remains outstanding (#2818). The capture carriers and
their eight x86_64 fadump contract cells are #2814/#3114's; this issue must not
fabricate their pending evidence bindings.

## Considered & rejected

- **Admission only.** Judgment: closes the bug but leaves no machine-readable
  System-specific advertisement for the issue's required agreement check.
- **Filter using host architecture.** Verified: ADR-0349 and discovered guest_arches
  allow ppc64le guests on another host architecture; host and guest are distinct.
- **Remove fadump from the provider descriptor.** Verified: positive ppc64le
  admission tests rely on the static supported set; ADR-0349 deliberately includes it.
- **Change resources.describe into a System query or add an architecture parameter.**
  Judgment: the existing systems.get runtime/profile context is smaller and avoids
  accepting caller-supplied architecture as though it were the System's identity.
- **Declare fadump supported on x86_64.** Verified: ArchTraits and profile validation
  expressly disagree; no x86 mechanism evidence supports changing that contract.
