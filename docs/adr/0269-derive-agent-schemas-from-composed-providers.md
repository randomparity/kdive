# ADR-0269: derive agent-facing provider schemas from the composed deployment (#879)

- Status: Accepted
- Date: 2026-06-28

## Context

The agent-facing MCP surface advertises every `ResourceKind` regardless of which providers the
running deployment composed. A local-libvirt-only server still shows `remote-libvirt` and
`fault-inject` in `allocations.request`'s `kind` selector, the `systems.define`/`provision`/`reprovision`
provider-section union, the `resources.list` kind filter, and `systems.profile_examples`. The
runtime already gates correctly — `build_provider_resolver()` composes only enabled providers and
`resolve()` fails closed for an unregistered kind (`configuration_error`, ADR-0131) — but the
**presentation layer ignores it**: the static Pydantic models generate their JSON schemas at
import time from the full enum, blind to per-deployment composition. The agent is sold a provider
the server cannot serve (`BLACK_BOX_REVIEW.md` P2: an agent tried to allocate the unschedulable
`fault-inject` mock).

`fault-inject` (ADR-0072, default-off test fixture) is the loudest instance; `remote-libvirt`
exhibits the same defect on a local-only server. Issue #879 is a special case of a general gap.

The resolution layer is already *set-based* — the resolver holds a `dict[ResourceKind,
ProviderRuntime]` and `registered_kinds()` returns a `frozenset` — and the enablement predicates
are independent, so multiple providers can be composed at once. The presentation layer regressed
to an enumerated triple (`ProviderSection`'s three hardcoded fields). The next milestone adds
cloud and bare-metal providers with several enabled simultaneously, so the fix must be N-provider
native and make adding a provider a registration concern, not a model edit.

## Decision

Make the agent-facing provider enumeration a per-deployment **subset projection** of the composed
set, derived from one registry and computed at list/call time.

1. **One registry.** A first-class `PROVIDER_SECTIONS: Mapping[ResourceKind,
   ProviderSectionSpec]` (section model + alias + label) replaces the knowledge split across
   `provisioning.py`'s hardcoded fields and `_KIND_BY_BLOCK`. The section models are unchanged
   building blocks; the registry is the single source the resolver, schema projection, and
   discovery iterate. A guard test pins its key set == `ResourceKind` members.

2. **Registry-driven projection.** Two pure helpers keyed to the single `registered_kinds()` set
   (the anti-drift property): `project_tool_schema(parameters, kinds)` structurally narrows a
   tool's FastMCP-generated schema — filtering the `ResourceKind` enum `$def` and the
   `ProviderSection` object's properties to the live set (section sub-models unchanged and already
   present, so it only drops members) — and `assert_kind_composed(kind, kinds)` raises
   `configuration_error` for a non-composed kind. FastMCP generates the published schema from the
   handler signature and the domain model stays static (decision 4), so this is a structural
   projection plus a membership guard rather than a single dynamically-built Pydantic model; both
   iterate the registry, so a new provider is covered without editing either helper. Boundary-only:
   they feed the agent-facing schema and validation, never storage or the digest. Memoized on the
   frozenset key.

3. **Computed at list/call time, never frozen at registration.** Schema is projected in
   `ToolExposureMiddleware.on_list_tools` (and `tools.search`) from the live
   `registered_kinds()`; validation runs against the same projection at call time. This is option
   (a): restart-to-add now, but a future runtime hot-add (recomposable resolver +
   `tools/list_changed`) is additive — the schema architecture already tracks the live set. The
   projection fails open to the full schema on error, and that branch emits a structured warning +
   counter so a silent revert is observable.

4. **Two membership views.** The agent boundary projects over `registered_kinds()`; the
   domain/storage layer (`ProvisioningProfile.parse`, digest, render, teardown) keeps the existing
   hand-written static `ProviderSection` over the full `ResourceKind` set — **unchanged**.
   Disabling a provider must not orphan existing Systems of that kind — a stored `remote-libvirt`
   profile must still parse and tear down after remote is disabled, so narrowing lives strictly at
   the agent surface. Leaving the domain model untouched also keeps `profile_digest` (ADR-0038),
   the reprovision dedup key, byte-identical; routing the domain model through the factory was
   rejected for that digest-stability risk. The `resources.list` `kind` filter is a query over
   existing data (which may include non-composed kinds), not a provisioning choice, so it too
   stays permissive — only the three provisioning-choice surfaces narrow.

5. **Call-time rejection.** A request naming a non-composed kind is rejected with
   `configuration_error` (parity with `resolve()`), enumerating the composed kinds. The guard runs
   on the shared service/handler path that both a direct call and the ADR-0268 `tools.invoke`
   dispatcher traverse — not as a schema-only constraint — so the gateway (raw `arguments`, never
   reading the projected list schema) cannot bypass it. Presentation (§3) and enforcement read the
   same live set.

6. **Empty composed set fail-closed.** A zero-provider deployment projects `enum: []` (matches
   nothing) and no `ProviderSection` properties; `assert_kind_composed` checks the empty set first
   and rejects any kind with `configuration_error` "no providers configured", before the static
   model's "exactly one section" validator could raise a generic, misattributed failure.

7. **Fault-inject is a derived consequence.** It is absent from every agent-facing surface iff it
   is not composed — the stock case under its default-off opt-in. No per-provider special-casing,
   `test_only` marker, or prose band-aid; #879's acceptance criteria fall out of the general rule.

## Consequences

- A cold agent on any deployment is offered exactly the providers it can provision; a
  non-composed provider neither appears nor is accepted.
- Adding a provider (cloud, bare-metal) still authors the provider's own definitions — a
  `ResourceKind` member, its section model, a typed field on the static domain `ProviderSection`
  (kept static for digest stability), a `PROVIDER_SECTIONS` entry, and a composition opt-in — but
  the three narrowed agent-facing surfaces and `profile_examples` then update from the registry with
  **no per-surface edits**. The registry removes the surface re-listing, not the provider
  definition.
- Schema is now a function of runtime composition, not an import-time constant —
  `tools/list` and `tools.search` output varies per deployment. The projection fails open to the
  full schema on error (availability over tightness; the call-time gate is the real boundary).
- The domain model stays permissive, so existing Systems of a disabled kind keep parsing and
  tearing down; runtime ops on them still fail closed via `resolve()` (unchanged).
- No DB migration, no RBAC change, no storage/digest/render change.
- Supersedes the parked single-issue approach (closed PR #883): fault-inject-hiding is no longer
  special-cased, the `test_only` marker and field-description band-aids are unnecessary and not
  introduced.

## Considered & rejected

- **Prose / `test_only` marker on a still-advertised fault-inject (parked PR #883).** Marks the
  symptom in one provider's text; leaves `remote-libvirt` mis-advertised and the schema still
  enumerating non-composed kinds. Treats #879 as a special case rather than the general defect.
- **Hand-maintained kind list + an independent validator.** The variant this ADR rejects is one
  where the schema transform and the validator each carry their *own* enumeration of kinds (two
  sources that can drift) and the kind set is hand-coded per surface. The chosen mechanism
  (decision 2) is distinct: the schema projection and `assert_kind_composed` both read the *single*
  `registered_kinds()` set and both iterate `PROVIDER_SECTIONS`, so there is one membership source
  and no per-surface hand-coding. Structural schema narrowing is the right tool *because* the
  domain model stays static (decision 4) and FastMCP generates the schema from the signature;
  building a second dynamic Pydantic model to "own" the boundary schema would duplicate the section
  models for no gain.
- **Freeze the projection at tool registration.** Simpler, but a non-list-time snapshot would have
  to be reworked when runtime hot-add lands; computing at list/call time costs a memoized build
  and makes hot-add additive.
- **Full runtime hot-add now (recomposable resolver + `tools/list_changed` + concurrency).**
  Larger than #879 and arguably its own epic; deferred. This ADR builds only the seam so it stays
  additive (option (a)).
- **Removing `fault-inject` / non-composed kinds from the `ResourceKind` enum.** The enum is the
  universe of kinds and the providers are real when composed; removal would break the providers and
  their tests. Narrowing is a per-deployment subset, never a deletion.
- **Narrowing the domain model too.** Would orphan stored Systems of a later-disabled provider
  (unparseable profiles); the permissive domain model is required for teardown.
