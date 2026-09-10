# 0637 — The worker-host appliance multiplier is provider-shared, not local-libvirt's

## Status

Accepted (2026-09-09)

- **Issue:** #2397

## Context

[ADR-0636](0636-host-keyed-appliance-budgets.md) established that a host-side libguestfs
appliance budget scales off the **worker host's** KVM, put `host_appliance_multiplier()` in
`providers/local_libvirt/lifecycle/deadlines.py`, and applied it to one budget:
`_VIRT_CUSTOMIZE_TIMEOUT_S`. It left `SLOW_BUILD_TOOL_TIMEOUT_S` unscaled as a recorded
follow-up (#2397), so in-guest `build-fs` on an emulated host still fails at
`virt-tar-out exceeded its timeout {'timeout_s': 1800}` — deviation 1 of #2383's proof record.

That constant lives in `providers/shared/build_timeouts.py` and is consumed by **both** libvirt
providers: `local_libvirt/rootfs_build.py`, `local_libvirt/lifecycle/rootfs/customization_boot.py`,
and `remote_libvirt/rootfs_build.py`. Scaling it therefore asks a question ADR-0636 did not
answer: may the provider-agnostic `shared/` layer reach into a concrete provider package for the
multiplier, or does the multiplier belong somewhere both providers may see?

The appliance is a worker-host fact, not a local-libvirt fact. ADR-0636 said so in its own
reasoning; it placed the helper in `local_libvirt/` only because local-libvirt was then its sole
caller. #2397 is the change that stops being true.

## Decision

We will move `host_appliance_multiplier()` to a new provider-agnostic module,
`providers/shared/appliance_budget.py`, keeping it importable from
`providers/local_libvirt/lifecycle/deadlines.py` as a re-export so existing local-libvirt callers
are unchanged. `providers/shared/build_timeouts.py` will expose
`slow_build_tool_timeout_s() -> int` — the 1800 s budget scaled by that multiplier and truncated
to an int — and every rootfs-build call site in both providers will call it at invocation time
rather than binding a module-level constant at import time.

## Consequences

Both providers scale the same budget from one definition, and the `providers/shared` →
`providers/local_libvirt` import the alternative needed never appears, so the boundary
`tests/providers/test_provider_boundaries.py` enforces stays intact. The new module reads
`LIBVIRT_TCG_DEADLINE_MULTIPLIER` from `local_libvirt/settings.py`, which is that test's one
declared exception (ADR-0087 declarations contain no provider implementation), so operators keep
ADR-0636's single knob.

Returning an `int` keeps `run_guestfs_tool(..., timeout_s: int)` and the
`details={"timeout_s": ...}` error payload unchanged; a KVM host gets exactly 1800, byte-identical
to today. Resolving the budget per call replaces five module-level aliases
(`_ACQUIRE_TIMEOUT_S`, `_REPACK_TIMEOUT_S`, `_INJECT_TIMEOUT_S`, `_SEAL_TIMEOUT_S`,
`_VIRT_BUILDER_TIMEOUT_S`, each a bare copy of the constant) with one call, and moves the
`/dev/kvm` probe off import time, where a cached verdict would outlive the fact it measures.

The cost is a re-export in `deadlines.py` that ordinarily would be replaced outright. #2397's
approved scope excludes touching `overlay_customize.py`, the re-export's only consumer, so
collapsing it is a follow-up rather than part of this change. An emulated host also now takes
5 hours to surface a genuinely hung build tool instead of 30 minutes, bounded by the job deadline
above it — the same trade ADR-0636 accepted at its own scale.

## Considered & rejected

- **Import `host_appliance_multiplier` into `providers/shared/build_timeouts.py` from
  `local_libvirt/lifecycle/deadlines.py`.** verified: adding that one import and running
  `uv run python -m pytest tests/providers/test_provider_boundaries.py` at 007b070ea fails
  `test_only_composition_imports_local_libvirt_provider_implementation` with
  `src/kdive/providers/shared/build_timeouts.py:7: from kdive.providers.local_libvirt.lifecycle.deadlines import ...`.
  It would also put local-libvirt lifecycle code in remote-libvirt's import graph, which ADR-0076
  forbids.
- **Define the KVM branch a second time in `shared/` and leave `deadlines.py` byte-identical.**
  judgment: two definitions of one policy for the sake of avoiding a two-line re-export, with a
  divergence nobody would notice until an operator's budgets disagreed.
- **Move `tcg_deadline_multiplier` to `shared/` as well, so both keys stay together.** verified:
  #2397's operator-approved scope excludes modifying `tcg_deadline_multiplier`; it is ADR-0636's
  and PR #2395's, merged.
- **Raise `SLOW_BUILD_TOOL_TIMEOUT_S` to a larger fixed number.** verified: ADR-0636 rejected
  exactly this for the sibling budget — it would have to clear the slowest emulated host, so
  every KVM host inherits a timeout that bounds nothing.
- **Do nothing; keep building guest images on an x86_64 host.** verified: #2383's proof record
  records that workaround at deviation 1 and states an operator who cannot reach an x86_64 host
  has no path to a guest image on an emulated host.
