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

**`ProfilePolicy` declares the `ResourceKind` whose profile section it owns.** Add a read-only
`kind` property to the protocol in `src/kdive/profiles/provider_policy.py` and a `ClassVar` to each
of the three adapters. A policy is already a per-provider singleton constructed by that provider's
composition, so the value is a class-level constant, not state — and a read-only protocol member is
what keeps it one: a mutable `kind: ResourceKind` on the protocol would make the discriminant the
guard trusts assignable, and would reject both idiomatic constant spellings (`ClassVar` and
`@property`) in an implementer.

**`validate_profile_for_provider` cross-checks the profile against that kind, first.** Before
`_reject_unknown_destructive_ops` and before any provider dereference,
`src/kdive/services/systems/validation.py` compares `profile.provider.kind` against
`profile_policy.kind` and raises `CategorizedError(CONFIGURATION_ERROR)` naming **both** the
profile's provider section and the Resource kind, with
`details={"profile_provider_section": …, "resource_kind": …}`. That details payload is new — the
remote provisioner's existing missing-section errors name one side and carry no details — and it is
what lets an agent tell which of the two to change without parsing prose.

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
- The fault-inject lane stops minting a System that survives provisioning and then dies at first
  use. The fault-inject provisioner discards the profile entirely
  (`providers/fault_inject/lifecycle/provisioning.py`), so a mismatched fault-inject System did
  reach `ready`; what failed later was `destructive_opt_in` / `capture_method` on the control,
  install, boot-evidence and vmcore lanes.
- **This is a write-path fix only, and it repairs nothing already stored.** The check runs at
  create and reprovision, never on a stored-profile read — which is why no previously-working read
  path can start raising (`control._op_opt_in` parses a stored profile unguarded and still does).
  A mismatched System persisted before this change stays `ready` and stays broken on those lanes.
  Detecting that residue is separate work, tracked in issue #1907; nothing here sweeps for it.
- `ProfilePolicy` gains a member, so a fourth provider must declare its kind. The protocol is
  structural and every implementer lives in `src/kdive/providers/<provider>/profile_policy.py`,
  so a missing declaration is a type error, not a runtime surprise. The guarantee is weaker inside
  the test suite, where several `ProfilePolicy` doubles are `cast()` past the checker.
- `assert_kind_composed` stays exactly as it is. It answers "is this kind deployed here?" before
  any database round-trip; the new check answers "does this profile match this Resource?" and needs
  the resolved runtime. Two questions, two gates, in that order — and the first **pre-empts** the
  second: in a deployment that does not compose the profile's kind at all, the caller gets
  `assert_kind_composed`'s "not configured in this deployment" error, which names the profile's
  kind and the composed set but not the Resource's kind. The new message is what a caller sees when
  the mismatched kind *is* composed, which is the case worth disambiguating.
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
