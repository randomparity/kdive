# 0867 — Resolve System operation handles in the provider runtime

## Status

Accepted (2026-11-24)

Builds on [ADR-0063](0063-typed-provider-runtime.md),
[ADR-0071](0071-per-kind-provider-runtime-registry.md) and
[ADR-0187](0187-remote-libvirt-per-op-resource-selection.md).
Preserves the refusing [BYO skeleton](0813-byo-host-skeleton-inventory-and-hmc-identity.md).

## Context

Runtime selection is provider-aware but generic operation callers still create
libvirt names or bare UUID handles. At `277fdb48f`, superseded provision cleanup
uses `kdive-<id>` without a recorded name, while fault-inject provisioning returns
`fault-inject-<id>`. The synthetic domain survives that cleanup. Other callers
honor the recorded name, leaving one System with inconsistent operation targets.

## Decision

Add a required pure `system_handle(UUID, recorded_name)` callback to the existing
`ProviderRuntime`, returning the existing SystemHandle type. Each current
composition supplies its policy. Preserve nonempty recorded names; libvirt and
fault-inject own their respective legacy fallback. BYO still refuses before I/O.

Generic worker/MCP callers use the bound runtime's policy and ports. Keep resource
selection, fences, public wire contracts and UUID storage/lifecycle identities.
Remove old caller-side naming paths without a compatibility alias or new registry.

## Consequences

The internal Runtime constructor gains one required collaborator; in-tree
constructors/fakes migrate together. No public schema or persisted data changes.
A future runtime must state its target policy. This enables no new provider operation.

## Considered & rejected

- **Keep a generic default or fallback helper.** verified: at `277fdb48f`,
  `src/kdive/providers/fault_inject/lifecycle/provisioning.py::domain_name` and
  `src/kdive/jobs/handlers/systems.py::provision_handler` select different prefixes;
  one generic libvirt default cannot represent both policies.
- **Rewrite every operation protocol to accept a System record.** judgment: UUID
  lifecycle/storage contracts and opaque operation handles have different jobs;
  replacing both expands this repair beyond target selection.
- **Make the callback optional and retain old caller paths.** judgment: silent
  fallback preserves the defect and leaves ownership split across consumers.
- **Add another binding registry or target model.** judgment: the existing runtime
  already selects provider ports, and two existing identity facts suffice.
