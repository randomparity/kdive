# Profile-examples onboarding: disk-image kernel source + discovery chain

- **Issues:** [#472](https://github.com/randomparity/kdive/issues/472) (bug) ·
  [#474](https://github.com/randomparity/kdive/issues/474) (feature)
- **ADR:** [`0133`](../adr/0133-profile-examples-onboarding-chain.md)
- **Follow-up to:** [#449](https://github.com/randomparity/kdive/issues/449) onboarding epic
- **Status:** Draft

## Problem

Validating the live VM lifecycle on `sha-6898353` found two onboarding defects in
`systems.profile_examples` (the ADR-0124 entry-point discovery tool).

1. **#472 — disk-image example demands a kernel source the VM-only flow has no use for.**
   `ProvisioningProfile.kernel_source_ref` is a schema-`required` `NonEmptyStr`, and the examples
   tool emits a `kernel_source_ref` REPLACE_ME in its shared `_CORE` for the `disk-image`
   remote-libvirt example. A disk-image provision boots the operator-staged base image's own kernel
   (ADR-0078/0080) — it builds no kernel — so a black-box agent provisioning a VM is told to invent
   a "real" kernel source it does not have, with no signal that the field is unused on this lane.

2. **#474 — the breadcrumb chain skips discovery.** `systems.profile_examples` chains only
   `["systems.define", "allocations.request"]` in `suggested_next_actions`, omitting the discovery
   tools an agent needs to assemble a *valid* request: `resources.list` (kind/id), `shapes.list`
   (sizing), `accounting.estimate` (cost). Following breadcrumbs alone does not reach a granted
   allocation.

## Ground truth (verified in tree)

- `ProvisioningProfile.kernel_source_ref` is read **nowhere** on any provisioning path. The only
  `.kernel_source_ref` readers are `profiles/build.py:183` and
  `providers/shared/build_host/dispatch.py:125`, both over `ServerBuildProfile` — a *different*
  field on the build profile, not the provisioning profile.
- All eight chain identifiers are registered tools (`systems.provision`, `systems.get`,
  `systems.teardown` via `mcp/tools/lifecycle/systems/registrar.py`; `resources.list`,
  `shapes.list`, `accounting.estimate`, `allocations.request`, `allocations.release` via their
  registrars / the `mcp/app.py` description map).
- The pairing rule `_pair_boot_method_with_provider` already ties `disk-image` ⇔ remote-libvirt
  and `direct-kernel` ⇔ local/fault, so `boot_method` is a reliable discriminator for "is this the
  build-iterating lane."

## Design

### #472 — optional `kernel_source_ref`, required on `direct-kernel`

In `src/kdive/profiles/provisioning.py`:

- Relax `kernel_source_ref: NonEmptyStr` → `kernel_source_ref: NonEmptyStr | None = None`.
- Add an `@model_validator(mode="after")` `_require_kernel_source_for_direct_kernel` that raises
  `ValueError` (mapped to `configuration_error` by `parse()`) when `boot_method=direct-kernel` and
  `kernel_source_ref is None`. `disk-image` accepts a present *or* absent value (a present value is
  ignored downstream, as it always was — backward compatible).

We require it on `direct-kernel` rather than dropping the requirement entirely: "required iff
direct-kernel" is the narrowest change that fixes the reported defect (disk-image) without churning
the direct-kernel callers/tests (`tests/mcp/systems_support.py`, `tests/profiles/test_provisioning.py`'s
`_valid()`) that already supply it. The field is dead on *every* provisioning render path today;
removing it from the provisioning profile entirely is a separate, broader decision recorded as
out-of-scope below, so this change does not pre-empt it.

In `src/kdive/mcp/tools/lifecycle/systems/profile_examples.py`:

- Move `kernel_source_ref` out of the shared `_CORE` dict. Add it only to the two `direct-kernel`
  example builders (`_local_profile`, `_fault_profile`); the `disk-image` `_remote_profile` emits
  no `kernel_source_ref`.
- Update `_REPLACE_NOTE` so it no longer names `kernel_source_ref` as a universal REPLACE_ME, and
  the disk-image example carries no kernel-source instruction. The note's kernel-source mention
  applies only to the direct-kernel examples that still carry the placeholder.

### #474 — full discovery→provision chain

In `profile_examples.py`, `_NEXT_ACTIONS` becomes:

```python
_NEXT_ACTIONS = [
    "resources.list", "shapes.list", "accounting.estimate", "allocations.request",
    "systems.provision", "systems.get", "systems.teardown", "allocations.release",
]
```

This **removes `systems.define` from the entry breadcrumb** in favor of the one-shot
`systems.provision` path the lifecycle uses. `systems.define` is not orphaned — it remains a
registered, directly-callable tool with its own documented define→`provision_defined` lifecycle; the
entry breadcrumb just leads a cold agent down the discovery + one-shot `systems.provision` path
first, rather than the two-step define-then-provision path. The existing `test_collection_chains_into_define` behavior test (which asserts
`systems.define` is in the entry chain) is rewritten to assert the new chain: `resources.list`
leads, and `allocations.request` precedes `systems.provision`.

The granted-allocation envelope (`allocations.py`, the other place #474 suggests chaining) is owned
by a concurrent change and is out of scope here; the `profile_examples` chain alone satisfies the
entry-point acceptance.

## Acceptance

- A `disk-image` remote-libvirt profile that omits `kernel_source_ref` parses and validates; a
  `direct-kernel` profile that omits it is rejected `configuration_error`.
- `systems.profile_examples` emits no `kernel_source_ref` REPLACE_ME for the disk-image example.
- `systems.profile_examples` `suggested_next_actions` contains
  `resources.list, shapes.list, accounting.estimate` in order before `allocations.request`.

## Out of scope

- The granted-allocation envelope chain (allocations plane, concurrent owner).
- Removing `kernel_source_ref` from the provisioning profile entirely (it remains the
  direct-kernel learnable shape; a broader cleanup is a separate decision).
- Any DB migration (none needed).
