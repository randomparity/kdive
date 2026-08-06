# 0549 — Profile policies declare their ResourceKind, and admission cross-checks it

## Status

Accepted (2026-08-05)

## Context

A `ProvisioningProfile` carries exactly one provider section — `_require_exactly_one_provider`
enforces that, and `ProviderSection.kind` derives the section's `ResourceKind` from whichever one
is present. Nothing compared that derived kind against the kind of the Resource the System is
actually provisioned onto.

The two kind checks that exist check something else:

- `assert_kind_composed` at `systems.provision` / `systems.reprovision`
  (`src/kdive/mcp/tools/lifecycle/systems/registrar.py`) asserts only that the profile's kind is
  *composed into this deployment* (ADR-0269), not that it matches this System's allocation.
- `resource_kind_for_system` on the Run lanes (`src/kdive/services/runs/bind.py`,
  `src/kdive/services/runs/admission.py`) derives and compares `target_kind` from the **Resource**,
  never from the profile — a different contract, and the reason a kind-mismatched System is not a
  correctness hole.

The provider runtime — hence the `ProfilePolicy` — is resolved from the *Resource*:
`ProviderResolver.runtime_for_allocation` for create, `runtime_for_system` for reprovision. So the
policy that admission applies to a profile can belong to a different provider than the profile's
section, and every downstream dereference reads a section that is not there. The three policies
degrade three different ways:

- **`fault_inject`** — `rootfs_source` returns `None` and `validate_profile` is a no-op, so a
  fault-inject Allocation carrying a local- or remote-libvirt profile passes admission
  **completely** and the profile is persisted. The `AttributeError` is deferred to
  `destructive_opt_in` / `capture_method`, far from the mistake.
- **`remote_libvirt`** — `validate_profile` is `return None`; the failure is a bare
  `AttributeError` out of `rootfs_source` during admission.
- **`local_libvirt`** — fails only incidentally, because `validate_profile` happens to dereference
  `profile.provider.local_libvirt`; also a bare `AttributeError`.

`ProviderSection.local_libvirt` / `.remote_libvirt` / `.fault_inject` raise a bare `AttributeError`
when their section is absent, and `SystemAdmission._admit_within_bound` catches only
`CategorizedError`. So the mismatch surfaces to an agent as a generic FastMCP tool error with a
traceback rather than a `CONFIGURATION_ERROR` envelope naming the mistake.

The deep remote provisioner already models this correctly — both
`src/kdive/providers/remote_libvirt/lifecycle/provisioning.py` and `lifecycle/xml.py` raise
`CONFIGURATION_ERROR` on a missing section. Admission dies before either is reached.

No accepted decision governs the cross-check today: ADR-0269 covers composition narrowing only,
and ADR-0071 says nothing about profiles.

## Decision

**`ProfilePolicy` declares the `ResourceKind` whose profile section it owns.** Add a `kind:
ResourceKind` member to the protocol in `src/kdive/profiles/provider_policy.py` and to each of the
three adapters. A policy is already a per-provider singleton constructed by that provider's
composition, so the value is a class-level constant, not state.

**`validate_profile_for_provider` cross-checks the profile against that kind, first.** Before
`_reject_unknown_destructive_ops` and before any provider dereference,
`src/kdive/services/systems/validation.py` compares `profile.provider.kind` against
`profile_policy.kind` and raises `CategorizedError(CONFIGURATION_ERROR)` naming **both** the
profile's provider section and the Resource kind, with
`details={"profile_provider_section": …, "resource_kind": …}` — the same message and details shape
the remote provisioner already uses for a missing section.

That single call site covers both agent-facing lanes, because both already call it inside an
`except CategorizedError` that produces a typed envelope:

- `systems.provision` — `SystemAdmission._admit_within_bound`, pre-mutation, before the allocation
  lock, so a rejection writes no System and no job.
- `systems.reprovision` — `SystemAdminHandlers.reprovision_system`, before the System lock, so a
  `ready` System is never transitioned on a mismatch.

Ordering it first is load-bearing: it makes the `AttributeError` paths in the three policies
unreachable for a kind mismatch rather than merely better-reported.

This is a **new agent-facing rejection**. A `systems.provision` call that today is accepted (the
fault-inject case) or fails with an untyped error (the two libvirt cases) now returns
`configuration_error` at create. That is the intended contract change: accepted-then-failed becomes
rejected-at-create.

## Consequences

- A kind-mismatched profile is rejected at `systems.provision` with an actionable message at the
  point of the mistake, instead of a traceback at boot or a persisted System that can never reach
  `ready`.
- The fault-inject lane stops persisting a System it can never provision. Any such System created
  before this change is unaffected — the check runs on the create and reprovision write paths only,
  never on a stored-profile read, so `control._op_opt_in`'s unguarded read path cannot start
  raising on stored data.
- `ProfilePolicy` gains a member, so a fourth provider must declare its kind. The protocol is
  structural and every implementer lives in `src/kdive/providers/<provider>/profile_policy.py`,
  so a missing declaration is a type error, not a runtime surprise.
- `assert_kind_composed` stays exactly as it is. It answers "is this kind deployed here?" before
  any database round-trip; the new check answers "does this profile match this Resource?" and needs
  the resolved runtime. Two questions, two gates, in that order.
- The check reads `profile.provider.kind`, which is total: `_require_exactly_one_provider` already
  guarantees exactly one section, so the `AttributeError` branch of that property stays unreachable
  from this path.

## Considered & rejected

- **Implement the check inside each provider's `validate_profile`.** The issue named this as the
  natural seam, and it needs no protocol change. Rejected: it writes the same guard three times, it
  is opt-in per provider (exactly how the gap arose — two of three implementations are `return
  None`), and it is unreachable for the case that matters most, since `fault_inject`'s
  `validate_profile` would still have to be written from scratch. A cross-provider invariant
  enforced provider-by-provider is a gap generator.
- **Thread the Allocation's `ResourceKind` through admission into
  `validate_profile_for_provider`.** Rejected: the kind is not in scope at either call site. In
  `_admit_within_bound` the profile is validated *before* the allocation lock is taken, so reaching
  the Allocation's Resource kind means either a second pre-lock query or moving validation inside
  the lock — and moving it inside would delay a pure-input rejection past a lock acquisition, for
  data the resolved policy already implies. `reprovision_system` would need the same plumbing
  independently. The policy is *already* resolved from that Resource; asking it its own kind is one
  field, not a second source of truth.
- **Add `kind` to `ProviderRuntime` rather than to `ProfilePolicy`.** The runtime is the object
  the resolver hands out, so it looks like the natural owner. Rejected:
  `validate_profile_for_provider` and both handler dataclasses take the `ProfilePolicy`, not the
  runtime, so this widens every signature on the path to carry a value only the profile check
  reads. The member belongs on the object that owns profile-section semantics.
- **Reject the mismatch in `ProvisioningProfile` parsing.** Rejected as impossible in the right
  direction: the profile is parsed from agent input with no knowledge of any Allocation. The
  mismatch is relational, not structural, and the profile model is deliberately the structural
  layer (`parse` stays total so the unguarded read-path parse cannot raise).
- **Widen `_admit_within_bound` to catch `AttributeError` and convert it.** The smallest possible
  diff, and it does produce a `CONFIGURATION_ERROR` envelope. Rejected: it converts a symptom into
  a message without naming what mismatched, it would swallow unrelated `AttributeError`s from
  anywhere in admission, and it leaves the fault-inject case — the one that is silently accepted
  and persisted — completely unfixed, because that lane raises nothing at admission at all.
