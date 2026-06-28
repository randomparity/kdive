# Derive agent-facing provider schemas from the composed deployment (#879)

- Date: 2026-06-28
- ADR: [ADR-0269](../adr/0269-derive-agent-schemas-from-composed-providers.md)
- Supersedes: the parked single-issue approach (closed PR #883 / draft ADR-0269 "hide
  fault-inject from default discovery"). Fault-inject-hiding becomes a *derived consequence*
  of the general gating, not a special case.

## Problem

The agent-facing MCP surface advertises **every** `ResourceKind` regardless of which
providers the running deployment actually composed. A local-libvirt-only server still shows
`remote-libvirt` and `fault-inject` in:

- `allocations.request` — the `kind` selector (`ResourceByKind.kind`, an open `ResourceKind`
  enum; `tool_payloads.py:51-53`).
- `systems.define` / `systems.provision` — the provider section union, which carries a
  hardcoded section for all three providers (`profiles/provisioning.py:157-189`).
- `systems.profile_examples` — the discovery tool that emits example profiles per provider.

(The `resources.list` `kind` filter shows the same static enum, but it is a *query predicate over
existing data*, not a provisioning choice, and is deliberately left permissive — see §4.)

The runtime already gates correctly: `ProviderComposition.build_provider_resolver()`
(`composition.py:329-351`) composes only the *enabled* providers, and
`ProviderResolver.resolve()` (`resolver.py:88-100`) fails closed for any unregistered kind
with `configuration_error`. The defect is purely that the **presentation layer ignores this**:
the static Pydantic models generate their JSON schemas at import time from the full enum, blind
to per-deployment composition. The agent is *sold* a provider the server cannot serve, then the
request dead-ends — the concrete report (`BLACK_BOX_REVIEW.md` P2) is an agent reading the
`fault-inject` example in depth and trying to allocate an unschedulable mock.

`fault-inject` (ADR-0072, a test/mock crash fixture, default-off via `KDIVE_FAULT_INJECT`) is
the loudest instance, but `remote-libvirt` exhibits the identical defect on a local-only server.
Issue #879 is therefore a special case of a general architectural gap.

## Goals

1. Every agent-facing *provisioning-choice* enumeration — the allocation `kind` selector, the
   `systems` provider-section union, and the `profile_examples` discovery output — presents
   exactly the providers in the deployment's composed set (`ProviderResolver.registered_kinds()`).
   Read/query surfaces (the `resources.list` `kind` filter) stay permissive (§4).
2. A request naming a non-composed kind is rejected at the agent boundary with
   `configuration_error`, not accepted-then-failed-late.
3. The seam is **forward-looking for the cloud-provider milestone**: the agent-facing
   discovery/schema/validation surfaces derive from the registry, so adding a provider needs **no
   edits to them**. The provider's own definitions are still authored — a `ResourceKind` member,
   its section model class, a typed field on the static domain `ProviderSection` (kept static for
   digest stability, §3), a `PROVIDER_SECTIONS` registry entry, and a composition opt-in — but the
   three narrowed surfaces (§4) and `profile_examples` then update automatically from the registry
   with no per-surface edits. The registry removes the surface *re-listing*, not the provider
   *definition*.
4. Schema and validation are computed from the *current* `registered_kinds()` at **list-time /
   call-time**, so a future runtime hot-add (recomposable resolver + `tools/list_changed`) is an
   *additive* change, not a schema-layer rewrite.

## Non-goals

- **Runtime hot-add itself.** The composed set is still resolved once at startup; adding a
  provider requires a config change + restart. This spec only ensures the schema seam will not
  have to be rewritten when hot-add lands (option (a), not (b), of the design discussion).
- **Changing runtime resolution, storage, digests, rendering, or teardown.** Those stay on the
  permissive domain model (see "Two membership views").
- **Removing any `ResourceKind` enum member.** The enum remains the universe of kinds; narrowing
  is a per-deployment *subset projection*, never a deletion.
- **The `fixtures` namespace** (`tool_index.py` TOC). It is the rootfs baseline catalog
  (ADR-0089), not the fault-inject provider — the issue conflates them; left unchanged.
- **`resources.register_fault_inject`** and the other `resources.register_*` tools — already
  `platform_admin`-gated operator surface, not general agent discovery; unchanged.

## Design

### 1. One registry, the single source of truth

Promote the implicit provider→section knowledge (today split across `provisioning.py`'s three
hardcoded fields and `resources/_common.py:_KIND_BY_BLOCK`) into one first-class registry:

```
PROVIDER_SECTIONS: Mapping[ResourceKind, ProviderSectionSpec]
```

where `ProviderSectionSpec` carries the existing Pydantic section model (`LibvirtProfile`,
`FaultInjectProfile`, `RemoteLibvirtProfile`), the `systems.toml` block / alias name, and the
schema label. The section *models* are unchanged building blocks; the registry is the data the
resolver, the schema projection, and discovery all iterate. The registry removes the
*per-agent-surface* edits: discovery, schema projection, and call-time validation all derive from
it. (The provider's own definitions — enum member, section model class, the typed field on the
static domain `ProviderSection`, and the composition opt-in — are still authored; the registry is
not a substitute for defining the provider, only for re-listing it on every agent surface.) A guard
test asserts the registry's key set equals the `ResourceKind` members so a new kind cannot be added
without a section spec, nor a section spec without an enum member.

### 2. Deployment-scoped projection, consumed at list/call time

A pure, memoized factory:

```
deployment_provider_models(kinds: frozenset[ResourceKind]) -> DeploymentProviderModels
```

builds, from the registry filtered to `kinds`:

- the narrowed `kind` constraint (a `Literal` over `kinds`' values) for the allocation selector
  (the `resources.list` filter is a read surface and stays permissive — §4), and
- a `ProviderSection`-equivalent model whose fields are exactly those kinds' sections, with a
  generated "exactly one section" validator (the per-System single-provider invariant, which is
  orthogonal to deployment membership and is preserved).

One model object yields **both** the JSON schema (`model_json_schema()`, projected at list-time
per §5) and the validation (`model_validate()`, invoked on the handler path per §6) — a single
source, so schema and accept/reject cannot disagree about membership. The factory is **memoized on the frozenset key**: built once per distinct composed
set, recomputed automatically if the set ever changes (the hot-add seam). It is called at
list-time (to project schemas) and call-time (to validate) from the *live* resolver, never
frozen at registration.

### 3. Two membership views — boundary narrowed, domain permissive

This is the load-bearing correctness decision.

- **Agent boundary** (MCP tool schemas + create-time validation): factory called with
  `resolver.registered_kinds()`. Only composed providers appear and are accepted.
- **Domain / storage** (`ProvisioningProfile.parse`, `profile_digest`, libvirt render, teardown):
  keeps the existing hand-written static `ProviderSection` (`provisioning.py:157-189`) over the
  full `ResourceKind` set — **unchanged**. The factory/projection is **boundary-only**; it never
  feeds storage, the domain model, or the digest.

Rationale: **disabling a provider must not orphan existing Systems of that kind.** A
remote-libvirt System created while remote was enabled must still parse, digest, and tear down
after remote is disabled; if the *domain* model narrowed, those stored profiles would become
unparseable. Narrowing therefore lives strictly at the agent surface.

Leaving the domain model untouched also protects `profile_digest` (`provisioning.py:394-408`),
the reprovision dedup key (ADR-0038): it hashes `model_dump(by_alias=True, exclude_none=True)`, so
*any* change to the stored/serialized `ProviderSection` would silently re-key every stored
profile's digest and break reprovision dedup. Because the boundary projection is a separate model
that never touches storage, digests are provably unchanged — pinned by a regression test (see
Testing). Routing the domain model through the factory was considered and rejected for exactly
this risk: the DRY gain does not justify putting digest stability in play. Runtime resolution for a
disabled-kind System already fails closed via `resolve()` (ADR-0131) — unchanged and out of scope.

### 4. The three narrowed surfaces (and the permissive read surface)

| Surface | Mechanism |
|---|---|
| `allocations.request` `kind` (`ResourceByKind`) | narrowed `Literal` from the projection |
| `systems.define` / `provision` provider union | projected `ProviderSection` model |
| `systems.profile_examples` | iterate `registered_kinds()` (replaces #883's special-case) |

These are *provisioning-choice / discovery* surfaces — what the agent is told it can provision.
`profile_examples` is already a tool *call*, so it reads the live set naturally; the first two are
static schemas today, their published `inputSchema` projected at list-time (§5) and their input
validated at call-time (§6).

**Read/query surfaces stay permissive.** The `resources.list` `kind` filter is a query predicate
over *existing data*, not a provisioning choice. Because the domain stays permissive (§3), the
catalog can legitimately hold rows of a now-disabled kind (a `remote-libvirt` resource registered
before remote was disabled); narrowing the filter would make those rows unfilterable while the
unfiltered listing still returns them — an observable inconsistency. So the `resources.list` filter
is deliberately **excluded** from narrowing and keeps the full `ResourceKind` enum.

### 5. List-time schema projection seam

`ToolExposureMiddleware.on_list_tools` (`middleware/exposure.py:29-47`) is the existing list-time
seam (it already RBAC-filters and core-set-filters the returned `Tool` sequence). It gains the
resolver (constructor injection) and, for the affected tools, rewrites the returned `Tool`'s
`inputSchema` for the current `registered_kinds()`. FastMCP generates each tool's schema from the
handler signature, so the projection does not regenerate the whole tool schema — it substitutes the
factory model's narrowed reusable `$defs` (the `ResourceKind` enum and the `ProviderSection`
object) into the generated `parameters` (returning a `model_copy` of the `Tool`, leaving the
registry object — and the deployment-agnostic generated tool reference — untouched). The section
sub-models (`LibvirtProfile` etc.) are unchanged and already present in `$defs`, so the
substitution drops members, never invents new definitions. The same projection helper
is applied by `tools.search` (ADR-0268), which returns full input schemas for gateway dispatch —
otherwise a searched schema would re-expose a non-composed kind the listed schema hid. The
projection failing must **fail open** to the unprojected (full) schema, mirroring the middleware's
existing fail-open on filter error (availability over tightness; the call-time gate in §6 is the
real boundary). The fail-open branch **must emit a structured warning and increment a counter** so
a silent revert to the full schema is detectable in production — without it, Goal 1 would degrade
unobserved and the happy-path exclusion tests (§Testing) would not catch the regression.

### 6. Call-time validation

A request naming a non-composed kind is rejected at the boundary with `configuration_error`
(category parity with `resolve()`). Call-time validation is the **same factory-built deployment
model as §2** — the handler validates raw `arguments` with
`deployment_provider_models(registered_kinds()).model_validate(...)` on the shared service path,
not FastMCP's statically-bound (permissive) tool param — so §2's "one model yields schema and
validation" and this check are the *same* mechanism reading the *same* live set, not two guards
that could drift. **It runs on the shared service/handler path that both a direct tool call and
the ADR-0268 `tools.invoke` dispatcher traverse — never as an advertised-schema-only constraint.** The gateway
dispatcher re-enters `app.call_tool(run_middleware=True)` with raw `arguments` and never reads the
projected list schema (§5); a schema-only narrowing would let it drive a non-composed kind straight
to the `resolve()`-fails-late dead-end this issue is about. Putting the guard on the handler path
closes that. This is enforcement; §5 is presentation. Both read the same live set, so a kind hidden
from the schema is also rejected by validation.

### 7. Empty-resolver fail-closed

A zero-provider deployment (ADR-0131) is valid but degenerate. The projection over an empty set
yields a `kind` constraint that admits nothing: the published JSON schema shows `enum: []`
(honest — matches nothing) and any value fails validation with a `configuration_error` naming
"no providers configured". For the systems provider-union path, the generated "exactly one section"
validator over *zero* sections would otherwise raise a generic, misattributed "exactly one provider
section" error; the factory therefore **short-circuits on an empty registry to the same
`configuration_error` ("no providers configured") before that validator runs**, so both boundary
shapes give the same clear message. The resolver already warns at construction; no new crash path.

### 8. Fault-inject as a derived consequence

With the above, `fault-inject` is absent from every agent-facing surface **iff** it is not in
`registered_kinds()` — which, by its default-off `KDIVE_FAULT_INJECT` opt-in, is the stock case.
No per-provider special-casing, no `test_only` marker, no prose description band-aid: the issue's
acceptance criteria fall out of the general rule. When an operator *does* enable fault-inject
(deliberate test/dev environment), it appears — correctly, because it is then composed and
schedulable.

## Forward-looking: the cloud-provider milestone

The next milestone adds cloud (and later bare-metal) providers, with multiple providers enabled
at once (`remote-libvirt + cloud + bare-metal`). The set-based projection handles N providers
with no change: a new provider's agent-facing presence (the three narrowed surfaces +
`profile_examples`) appears automatically from its `PROVIDER_SECTIONS` entry with no per-surface
edits, once its provider definitions (enum member, section model, the static-`ProviderSection`
field, composition opt-in) are authored. Because schema/validation are computed at list/call time
from the live set, the residual work for runtime hot-add is only the recomposable
resolver, the `tools/list_changed` notification, and mid-request concurrency safety — none of
which touch this schema architecture.

## Error handling

- Non-composed kind at the boundary → `configuration_error` (parity with `resolve()`), details
  enumerating the composed kinds so a black-box caller can self-correct (no-leak, ADR-0123).
- Empty composed set → `configuration_error` "no providers configured" on any kind.
- Projection failure at list-time → fail open to the full schema (logged); call-time gate holds.
- Domain parse of a stored non-composed kind → succeeds (permissive); never raised by this change.

## Testing

- **Factory unit:** 0 / 1 / 2 / N kinds → correct `Literal` members + union sections + generated
  "exactly one" validator; empty registry → both boundary shapes (the `kind` constraint and the
  provider union) fail closed with the same `configuration_error` ("no providers configured").
- **Boundary projection:** with only local-libvirt composed, the projected `inputSchema` for each
  narrowed surface excludes `remote-libvirt` and `fault-inject`; `tools.search` returns the same
  narrowed schema as `tools/list`.
- **Read surface stays permissive:** the `resources.list` `kind` filter still accepts every
  `ResourceKind`, and a listing returns a stored non-composed-kind row.
- **Call-time rejection (incl. gateway):** an `allocations.request` / `systems.define` naming a
  non-composed kind returns `configuration_error` enumerating the composed kinds — asserted both on
  a direct call **and** through the ADR-0268 `tools.invoke` dispatcher (proves the guard is not
  schema-only).
- **Digest stability:** a stored `remote-libvirt` profile's `profile_digest` is byte-identical
  before and after this change, and `parse`/`dump_profile` still round-trip it when remote-libvirt
  is *not* composed (proves the boundary projection never touches storage/digest, ADR-0038).
- **Fault-inject derived behavior:** default deployment (no `KDIVE_FAULT_INJECT`) shows no
  `fault-inject` on any narrowed surface; with it set, `fault-inject` appears on all three.
- **Forward-looking property:** a factory-level test over an *injected* registry carrying an extra
  spec yields that kind across all three narrowed projections, proving the agent surfaces derive
  from the registry with no per-surface edits; the documented "add a provider" cost (enum member +
  section model + static-`ProviderSection` field + registry entry + composition opt-in) is asserted
  in prose, not claimed as zero.
- **Registry completeness guard:** `PROVIDER_SECTIONS` key set == `ResourceKind` members.

## Acceptance criteria (issue #879) → coverage

- *"fault-inject not presented unless registered, or behind a dev/test flag"* → §8 (absent unless
  composed; `KDIVE_FAULT_INJECT` is that flag, and it is the composition signal — not a second
  source of truth).
- *"if it remains in any agent-facing enum/schema, marked test-only"* → stronger: it is **removed**
  from agent-facing schemas when not composed, so no marker is needed; when composed it is a real
  schedulable provider, not a fixture-to-be-marked.
- *"a guard/test prevents reappearance by default"* → the fault-inject derived-behavior test and
  the registry completeness guard.

## Scope / migration

No DB migration. No change to runtime resolution, storage, digest, render, or teardown. New
ADR-0269 supersedes the parked draft; README index updated.
