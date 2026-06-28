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
- `resources.list` — the kind filter.
- `systems.profile_examples` — the discovery tool that emits example profiles per provider.

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

1. Every agent-facing provider enumeration — schema enum, profile union, discovery output,
   and the kind filter — presents exactly the providers in the deployment's composed set
   (`ProviderResolver.registered_kinds()`).
2. A request naming a non-composed kind is rejected at the agent boundary with
   `configuration_error`, not accepted-then-failed-late.
3. The seam is **forward-looking for the cloud-provider milestone**: adding a provider touches
   only the composition opt-in table and a provider-section registry; every agent-facing
   surface updates with no further edits.
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
resolver, the schema projection, and discovery all iterate. Adding a provider = one registry
entry (plus its composition opt-in). A guard test asserts the registry's key set equals the
`ResourceKind` members so a new kind cannot be added without a section spec.

### 2. Deployment-scoped projection, consumed at list/call time

A pure, memoized factory:

```
deployment_provider_models(kinds: frozenset[ResourceKind]) -> DeploymentProviderModels
```

builds, from the registry filtered to `kinds`:

- the narrowed `kind` constraint (a `Literal` over `kinds`' values) for the allocation selector
  and the `resources.list` filter, and
- a `ProviderSection`-equivalent model whose fields are exactly those kinds' sections, with a
  generated "exactly one section" validator (the per-System single-provider invariant, which is
  orthogonal to deployment membership and is preserved).

One model object yields **both** the JSON schema (`model_json_schema()`) and the validation
(`model_validate()`) — a single source, so schema and accept/reject cannot disagree about
membership. The factory is **memoized on the frozenset key**: built once per distinct composed
set, recomputed automatically if the set ever changes (the hot-add seam). It is called at
list-time (to project schemas) and call-time (to validate) from the *live* resolver, never
frozen at registration.

### 3. Two membership views — boundary narrowed, domain permissive

This is the load-bearing correctness decision.

- **Agent boundary** (MCP tool schemas + create-time validation): factory called with
  `resolver.registered_kinds()`. Only composed providers appear and are accepted.
- **Domain / storage** (`ProvisioningProfile.parse`, `profile_digest`, libvirt render, teardown):
  factory called with the **full** `ResourceKind` set — stays permissive.

Rationale: **disabling a provider must not orphan existing Systems of that kind.** A
remote-libvirt System created while remote was enabled must still parse, digest, and tear down
after remote is disabled; if the *domain* model narrowed, those stored profiles would become
unparseable. Narrowing therefore lives strictly at the agent surface. Runtime resolution for such
a System already fails closed via `resolve()` (ADR-0131) — unchanged and out of scope.

### 4. The four surfaces

| Surface | Mechanism |
|---|---|
| `allocations.request` `kind` (`ResourceByKind`) | narrowed `Literal` from the projection |
| `resources.list` kind filter | narrowed `Literal` from the projection |
| `systems.define` / `provision` provider union | projected `ProviderSection` model |
| `systems.profile_examples` | iterate `registered_kinds()` (replaces #883's special-case) |

`profile_examples` is already a tool *call*, so it reads the live set naturally. The first three
are static schemas today; their published `inputSchema` is projected at list-time (§5) and their
input validated at call-time (§6).

### 5. List-time schema projection seam

`ToolExposureMiddleware.on_list_tools` (`middleware/exposure.py:29-47`) is the existing list-time
seam (it already RBAC-filters and core-set-filters the returned `Tool` sequence). It gains the
resolver (constructor injection) and, for the affected tools, rewrites the returned `Tool`'s
`inputSchema` to the projection for the current `registered_kinds()`. The same projection helper
is applied by `tools.search` (ADR-0268), which returns full input schemas for gateway dispatch —
otherwise a searched schema would re-expose a non-composed kind the listed schema hid. The
projection failing must **fail open** to the unprojected (full) schema, mirroring the middleware's
existing fail-open on filter error (availability over tightness; the call-time gate in §6 is the
real boundary).

### 6. Call-time validation

A request naming a non-composed kind is rejected at the boundary with `configuration_error`
(category parity with `resolve()`), reusing `ProvisioningProfile.parse`'s existing
`ValidationError → CONFIGURATION_ERROR` mapping for the systems path and an explicit guard fed by
`registered_kinds()` for the allocation path. This is enforcement; §5 is presentation. Both read
the same live set, so a kind hidden from the schema is also rejected by validation.

### 7. Empty-resolver fail-closed

A zero-provider deployment (ADR-0131) is valid but degenerate. The projection over an empty set
yields a `kind` constraint that admits nothing: the published JSON schema shows `enum: []`
(honest — matches nothing) and any value fails validation with a `configuration_error` naming
"no providers configured". The resolver already warns at construction; no new crash path.

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
with no change: a new provider is one `PROVIDER_SECTIONS` entry + one composition opt-in, and it
appears across all four surfaces automatically. Because schema/validation are computed at
list/call time from the live set, the residual work for runtime hot-add is only the recomposable
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
  "exactly one" validator; empty → fail-closed `configuration_error`.
- **Boundary projection:** with only local-libvirt composed, the projected `inputSchema` for each
  of the four surfaces excludes `remote-libvirt` and `fault-inject`; `tools.search` returns the
  same narrowed schema as `tools/list`.
- **Call-time rejection:** an `allocations.request` / `systems.define` naming a non-composed kind
  returns `configuration_error` enumerating the composed kinds.
- **Domain-permissive regression:** a stored `remote-libvirt` profile still `parse`s, digests, and
  serializes when remote-libvirt is *not* composed.
- **Fault-inject derived behavior:** default deployment (no `KDIVE_FAULT_INJECT`) shows no
  `fault-inject` on any surface; with it set, `fault-inject` appears on all four.
- **Forward-looking property:** registering a synthetic kind in `PROVIDER_SECTIONS` + composition
  makes it appear across all four surfaces with no other edits (proves goal 3).
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
