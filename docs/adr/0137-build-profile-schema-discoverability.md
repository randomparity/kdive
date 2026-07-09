# ADR 0137 — Build-profile schema discoverability at the MCP boundary

> **Superseded by [ADR-0316](0316-remove-server-build-lane.md)** (2026-07-08) — the server-build
> lane and its discoverable build-profile schema were removed; kdive builds only from uploaded
> artifacts. The decision below is retained as history.

- **Status:** Superseded by [ADR-0316](0316-remove-server-build-lane.md)
- **Date:** 2026-06-16
- **Deciders:** kdive maintainers

## Context

Black-box MCP evaluation (defect D2, #482) found `build_profile` — the `runs.create`
input — undiscoverable. The validated models are already strict and complete
(`ServerBuildProfile` / `ExternalBuildProfile`, `extra="forbid"`,
`src/kdive/profiles/build.py:86-111`), but the tool parameter is typed
`BuildProfileInput = Mapping[str, object]` (`src/kdive/profiles/types.py:11`), so FastMCP
advertises a freeform `additionalProperties: true` blob with no field schema. A caller learns
the required fields (`schema_version`, `kernel_source_ref`, …) only by submitting `{}` and
reading the validation error. There is also no documented path from the profile to a
kdump + debuginfo kernel: `config` is a `ComponentRef` and an omitted `config` resolves to the
seeded `kdump` catalog fragment (ADR-0096), but nothing on `build_profile`'s surface says so or
points at the `buildconfig.*` tools that expose those fragments.

This is the exact gap ADR-0124 closed for the provisioning `profile` parameter. That ADR
established the pattern and validated its two risks in-tree: (1) a typed Pydantic param is
validated by FastMCP **at argument binding**, before the tool body and before any in-body
`try/except` that builds the `configuration_error` envelope, so the binding `ValidationError`
must be intercepted and re-enveloped (`BindingErrorMiddleware`, `_BINDING_CONVERSIONS`,
`src/kdive/mcp/middleware.py:254-307`); and (2) the FastMCP 3.4.0 client must render the typed
input schema usably. ADR-0113 flattened *output* schemas because the client's per-call
`TypeAdapter` choked on the recursive `ToolResponse` `$ref`; that sweep touches only
`output_schema`, never `inputSchema`, so a structural input schema is unaffected by it but its
client rendering is what ADR-0124 spike (2) verified.

Two `build_profile`-specific facts shape the decision:

- The discriminator field `source` is **not uniformly required**: `ServerBuildProfile.source`
  defaults to `"server"` (existing server documents omit it), while `ExternalBuildProfile.source`
  is a required `Literal["external"]`. A Pydantic *discriminated* union (`Field(discriminator=…)`)
  requires the discriminator present in every member, so it cannot model this pair. A plain
  (smart) union `ServerBuildProfile | ExternalBuildProfile` does, and matches the existing
  dispatch in `BuildProfile.parse` (default `source="server"`).
- The handler `create_run` already parses through `BuildProfile.parse(...)`, which owns the
  redaction guarantee (submitted values scrubbed from error details, ADR-0029). That boundary
  must stay the single parse/redaction point.

## Decision

Type the `build_profile` parameter as the union `ServerBuildProfile | ExternalBuildProfile` so
FastMCP advertises the `anyOf` JSON schema (both lanes, every field, discoverable from the tool
surface), mirroring ADR-0124. Specifically:

1. **Type the param** in the `runs.create` registrar
   (`src/kdive/mcp/tools/lifecycle/runs/registrar.py`) as
   `ServerBuildProfile | ExternalBuildProfile` (a `ParsedBuildProfile`), and re-serialize the
   bound model to a dict via the existing `dump_build_profile(...)` before constructing
   `RunCreateRequest` — exactly how `systems.provision` calls `dump_profile(profile)`. The
   `create_run` handler, `RunCreateRequest.build_profile: BuildProfileInput`, and the
   `BuildProfile.parse` redaction boundary are **unchanged**: the handler still parses the dict,
   so the single parse/redaction point and the existing in-body `configuration_error` path are
   preserved for any caller still sending a mapping internally.

2. **Re-envelope the binding error** by adding one `_BINDING_CONVERSIONS` entry for
   `runs.create`:
   `_BindingConversion("system_id", _loc_under("build_profile"), _build_profile_envelope)`.
   `system_id` is the call's object id (matching the body path's `request.system_id` failure
   envelope); `_loc_under("build_profile")` recognises a binding failure (the plain union's error
   `loc` is `("build_profile", "ServerBuildProfile"/"ExternalBuildProfile", …)`, all under
   `build_profile`); `_build_profile_envelope` is a small build-lane sibling of ADR-0124's
   `_profile_envelope` — same `configuration_error` + bounded `errors` surfacing, but its message
   is `"invalid build profile"` to match the in-body `BuildProfile.parse` failure. A field-level
   error elsewhere on the call propagates unchanged.

3. **Document the config-fragment path** in the `build_profile` parameter description and the
   generated tool reference: that `config` is a `ComponentRef` (a `catalog` ref keyed by name),
   that **omitting** it resolves to the seeded `kdump` catalog fragment (`DEFAULT_CONFIG_REF`,
   which carries KEXEC / CRASH_DUMP / DEBUG_INFO_DWARF5 / GDB_SCRIPTS, ADR-0096) — the default
   happy path for a kdump + debuginfo kernel — and that `buildconfig.get` retrieves a named
   fragment's bytes + sha256 + merge recipe so a caller can inspect what `config` selects. There
   is **no** `buildconfig.list` tool (only `buildconfig.get` read and `buildconfig.set` operator
   write); the docs name only those. Cross-reference the #481 operator doc
   (the build-source-staging guide, ADR-0136; both removed by ADR-0316) for staging the kernel *source* rather
   than duplicating it — this ADR covers the *config* selection axis, that doc covers the
   *source* axis.

A dedicated `_build_profile_envelope` helper is added rather than reusing `_profile_envelope`:
`failure_from_error` surfaces the `CategorizedError` message as `detail`, and `configuration_error`
`detail` is **not** suppressed (`suppressed_detail` only suppresses `authorization_denied`/
`not_found`), so reusing `_profile_envelope`'s `"invalid provisioning profile"` message would
mislabel a build-profile failure. The new helper carries `"invalid build profile"`, byte-matching
the in-body parse failure.

## Consequences

- A caller can construct a valid `build_profile` — including selecting a kdump + debuginfo
  `config` — from the published `anyOf` schema and the parameter docs alone, without submitting
  `{}` to learn the fields (the acceptance criterion).
- Typing the param tightens the contract: extra keys previously tolerated under
  `additionalProperties: true` are now rejected at binding (`extra="forbid"`) — already true at
  the `parse()` boundary, now enforced one layer earlier, flagged so it is not a surprise.
- Malformed input never regresses to a raw FastMCP `ToolError`: the binding error is converted to
  the standard `configuration_error` envelope inside the telemetry span, identical to the
  `systems.*` typed-profile tools.
- The advertised flat *output* schema (ADR-0113) is untouched; only the `inputSchema` gains the
  union shape, which the FastMCP 3.4.0 client renders usably (verified in-tree with an in-memory
  `Client`, the same proof ADR-0124 required, now for a plain union).
- New obligations: a `runs.create` end-to-end test that a malformed `build_profile` returns the
  envelope (not a `ToolError`) and a valid one is accepted; a regenerated tool reference
  (`docs/guide/reference/runs.md`) carrying the union schema and config-fragment guidance; the
  `_BINDING_CONVERSIONS` and tool-doc guard updates.
- This refines ADR-0029 (the build-profile schema is now advertised, not just enforced) and
  reuses ADR-0123/0124 (the `detail` + binding-error-conversion seam). No schema field, parse
  validator, DB column, migration, or auth change.

## Considered & rejected

- **Discriminated union via `Field(discriminator="source")`**: rejected — `source` is not present
  on every member (`ServerBuildProfile.source` defaults, `ExternalBuildProfile.source` required),
  which a Pydantic discriminated union forbids. The plain union dispatches correctly (verified:
  server-default, explicit-server, external, and git-source all bind to the right model) and
  matches `BuildProfile.parse`'s existing default-`"server"` dispatch.
- **Expose the JSON schema via a docs or examples affordance only** (keep the param a `Mapping`):
  the issue offered this as the fallback if typing the param were infeasible under the
  flat-schema constraint. It is not infeasible — the in-tree client spike binds the union and
  re-envelopes the error end-to-end — so the typed param ships, which machine-advertises the
  schema rather than relying on prose. (A `runs`-side profile-examples discovery tool mirroring
  `systems.profile_examples` is a possible future affordance but is out of scope for #482, whose
  acceptance is met by the published schema + docs.)
- **Parse in the registrar and pass the model through** (drop the `dump_build_profile` round-trip
  and the handler's `parse`): rejected — it would split the redaction boundary across two layers
  and change `RunCreateRequest`/`create_run` signatures for no behavioural gain. Re-serializing to
  a dict keeps `BuildProfile.parse` the single parse/redaction point (matching
  `systems.provision`'s `dump_profile` round-trip).
- **A create-time rejection of URI-looking bare `kernel_source_ref`**: out of scope and already
  rejected by ADR-0136 — a URI-looking bare string is the established warm-tree-label convention.
  *(Later reversed: [ADR-0242](0242-self-service-build-from-url.md) supersedes ADR-0136's
  no-guard decision and rejects clone-URL-scheme bare strings at the parse boundary.)*
