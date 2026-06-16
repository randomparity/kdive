# ADR 0133 — Profile-examples onboarding: optional disk-image kernel source + discovery chain

- **Status:** Proposed
- **Date:** 2026-06-16
- **Deciders:** kdive maintainers

## Context

`systems.profile_examples` (ADR-0124) is the onboarding entry point: it projects the
`systems.toml` inventory into one ready-to-edit example provisioning profile per configured
provider so a cold agent can learn a valid profile shape from the MCP surface alone. Validating
the live VM lifecycle on `sha-6898353` surfaced two onboarding defects (#472, #474).

**#472 — `kernel_source_ref` misleads VM-only provisioning.** `ProvisioningProfile.kernel_source_ref`
(`src/kdive/profiles/provisioning.py:254`) is a schema-`required` `NonEmptyStr`, and the examples
tool emits `kernel_source_ref: "git:REPLACE_ME-kernel-source"` in its shared `_CORE` for **every**
example — including the `disk-image` remote-libvirt example. A `disk-image` provision boots the
operator-staged base image's own kernel (ADR-0078/0080); it does not build a kernel. So an agent
asked simply to *provision/boot a VM* is told to invent a "real" kernel source it does not have.

Tracing every reader of the field confirms it is functionally dead on the provisioning path:
`ProvisioningProfile.kernel_source_ref` is read **nowhere** in the codebase. The build lane reads
a *separate* `kernel_source_ref` that lives on `ServerBuildProfile` (`src/kdive/profiles/build.py:100`,
consumed by `providers/shared/build_host/dispatch.py:125`), not on the provisioning profile. The
provisioning renderers (local-libvirt direct-kernel, remote-libvirt disk-image) never dereference
it. It is a required-but-unread field on the provisioning profile.

**#474 — discovery breadcrumbs are too short.** The 104-tool surface has no onboarding index; the
intended substitute is `suggested_next_actions` breadcrumbs. But `systems.profile_examples` chains
only `["systems.define", "allocations.request"]`, skipping the discovery tools an agent needs to
build a *valid* allocation request: `resources.list` (the resource kind/id), `shapes.list`
(sizing), `accounting.estimate` (cost). Following the breadcrumbs alone does not walk an agent from
"I want to provision a VM" to a granted allocation and a booted System.

## Decision

**#472 — make `kernel_source_ref` optional, required only for `direct-kernel`.** Relax the field to
`NonEmptyStr | None = None` and add a model_validator that requires it when
`boot_method=direct-kernel` and forbids no value otherwise. The pairing rule (ADR-0080) already
ties `disk-image` to the remote-libvirt section, so "disk-image" is the lane that legitimately
omits it. We keep the field accepted (not removed) because the local/fault `direct-kernel` lanes
and existing callers/tests still supply it; we only stop *requiring* it on the lane that never reads
it. The examples tool stops emitting a `kernel_source_ref` REPLACE_ME for the `disk-image` example
(it moves out of the shared `_CORE` and is added only to the `direct-kernel` example profiles), and
the replace-note no longer instructs the agent to invent a kernel source for a VM-only provision.

Rationale for requiring it on `direct-kernel` rather than dropping the requirement entirely: the
field is dead on *every* provisioning render path today, but `direct-kernel` is the build-iterating
lane where a downstream `runs.build` is the expected next step, and keeping the example carry a
source there preserves the existing learnable shape for the build lifecycle. The narrowest change
that fixes the reported defect without inventing new scope is "required iff direct-kernel."

**#474 — chain the full discovery→provision lifecycle.** `systems.profile_examples`'s
`suggested_next_actions` becomes the literal, valid tool-identifier sequence:

```
resources.list → shapes.list → accounting.estimate → allocations.request
→ systems.provision → systems.get → systems.teardown → allocations.release
```

Every identifier is a registered tool. `systems.define` is dropped from the chain in favor of
`systems.provision` (the one-shot define+provision path the lifecycle uses); an agent that follows
the chain reaches `allocations.request` having discovered the resource kind (`resources.list`) and
a shape (`shapes.list`) and an estimate (`accounting.estimate`), so the request is granted on the
first valid attempt (the #474 acceptance).

The granted-allocation envelope (the other place #474 suggests chaining) lives in
`services/allocation`/`mcp/tools/lifecycle/allocations.py`, owned by a concurrent change; this ADR
scopes only the `profile_examples` chain, which alone satisfies the entry-point acceptance.

## Consequences

- No DB migration: the schema change is a Pydantic field relaxation, not a column change.
- A stored `disk-image` profile may now omit `kernel_source_ref`; no renderer reads it, so the
  stored profile is unaffected. A `direct-kernel` profile that omits it is now rejected at
  `parse()` with `configuration_error` (previously rejected for the same reason via `required`).
- The examples tool's generated tool-reference doc (`docs/guide/reference/`) regenerates; the
  committed snapshot and the `test_systems_profile_examples`/`test_systems_profile_schema` tests
  update with the new chain and the disk-image example's absent `kernel_source_ref`.
- Backward compatibility: a `disk-image` profile that *does* supply `kernel_source_ref` is still
  accepted (the value is ignored, as it always was); only the *requirement* is lifted.
