# 0637 — The shared build budget resolves its own worker-host multiplier

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
and `remote_libvirt/rootfs_build.py`. Scaling it asks a question ADR-0636 did not answer: may the
provider-agnostic `shared/` layer reach into a concrete provider package for the multiplier, or
must it be supplied some other way?

It may not. `tests/providers/test_provider_boundaries.py` forbids any import of
`kdive.providers.local_libvirt.*` outside `providers/local_libvirt/`,
`providers/assembly/composition.py`, and two `system_authority` modules — with one declared
exception, `kdive.providers.local_libvirt.settings`, because ADR-0087 declarations contain no
provider implementation. `host_appliance_multiplier` is therefore out of reach from `shared/`,
and so is `tcg_deadline_multiplier`, which it delegates to and which #2397's approved scope
excludes moving.

## Decision

We will give `providers/shared/build_timeouts.py` a
`slow_build_tool_timeout_s(*, kvm_present: Callable[[], bool] | None = None) -> int` that resolves
the worker host's KVM itself — the ADR-0352 probe from
`kdive.diagnostics.contributions.guest_arch_accel`, which is provider-agnostic — and scales
`SLOW_BUILD_TOOL_TIMEOUT_S` by `LIBVIRT_TCG_DEADLINE_MULTIPLIER` read through the declared
settings exception. Every rootfs-build call site in both providers will call it at invocation
time in place of the five module-level aliases of the constant, which are deleted. Nothing moves:
`deadlines.py`, `host_appliance_multiplier`, and `overlay_customize.py` are untouched.

## Consequences

The `providers/shared` → `providers/local_libvirt` implementation import the alternatives needed
never appears, so the boundary the test enforces holds and remote-libvirt's import graph stays
free of local-libvirt lifecycle code (ADR-0076).

The cost is that the "KVM is unscaled, anything else scales by the setting" branch is now written
twice — once in `host_appliance_multiplier`, once here. What is duplicated is the branch, not the
factor: both read `LIBVIRT_TCG_DEADLINE_MULTIPLIER`, so ADR-0636's one-knob guarantee holds and
the two budgets cannot drift apart in an operator's hands. Collapsing the two into one definition
needs `tcg_deadline_multiplier` in a provider-agnostic module, which #2397 excludes.

The injected `kvm_present` seam mirrors `host_appliance_multiplier`'s, and for the same stated
reason: without it the resolved budget is a property of whichever machine runs the suite, so
neither branch could be proven. Returning an `int` keeps `run_guestfs_tool(..., timeout_s: int)`
and the `details={"timeout_s": ...}` error payload unchanged; a KVM host gets exactly 1800.
Resolving per call rather than at import keeps the probe answering for the host as it is when the
tool runs.

An emulated host now takes 5 hours to surface a genuinely hung build tool instead of 30 minutes,
and nothing above the tool run terminates it: the job queue's lease is "a per-heartbeat limit, not
a total job runtime limit" (`jobs/queue.py`), and `jobs/worker.py` records that a long-running job
never starves the liveness ticker. That exposure is the price of a budget an emulated host can
meet at all, and it is the same trade ADR-0636 accepted at its own scale.

## Considered & rejected

- **Import `host_appliance_multiplier` into `providers/shared/build_timeouts.py` from
  `local_libvirt/lifecycle/deadlines.py`.** verified: adding that one import and running
  `uv run python -m pytest tests/providers/test_provider_boundaries.py` at 007b070ea fails
  `test_only_composition_imports_local_libvirt_provider_implementation` with
  `src/kdive/providers/shared/build_timeouts.py:7: from kdive.providers.local_libvirt.lifecycle.deadlines import ...`.
- **Move `host_appliance_multiplier` to a new provider-shared module and re-export it from
  `deadlines.py`.** verified: it cannot carry its body — `deadlines.py` ends it with
  `return tcg_deadline_multiplier("kvm" if probe() else None)`, and `tcg_deadline_multiplier`
  stays put (next bullet), so the moved copy would need the import the bullet above fails on.
  The move therefore buys no single definition while adding a module, a re-export kept alive only
  because `overlay_customize.py` is out of scope, and a follow-up to remove it.
- **Move `tcg_deadline_multiplier` to a provider-agnostic module as well, so both keys stay
  together.** verified: #2397's operator-approved scope excludes modifying it; it is ADR-0636's
  and PR #2395's, merged.
- **Raise `SLOW_BUILD_TOOL_TIMEOUT_S` to a larger fixed number.** judgment: it would have to clear
  the slowest emulated host, so every KVM host inherits a timeout that bounds nothing — ADR-0636
  reached the same judgment for the sibling budget.
- **Do nothing; keep building guest images on an x86_64 host as #2383 did.** verified: issue
  #2397's first acceptance criterion is an emulated host completing in-guest `build-fs`, which the
  workaround does not deliver. The proof record's deviation 1 notes the qcow2 is digest-identical
  either way (`sha256:4dcc3e74…`), so the workaround is sound where a second host exists; the
  criterion is what rules it out as the answer.
