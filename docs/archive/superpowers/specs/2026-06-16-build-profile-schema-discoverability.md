# Build-profile schema discoverability at the MCP boundary (#482, D2)

- **ADR:** [0137](../../adr/0137-build-profile-schema-discoverability.md)
- **Issue:** [#482](https://github.com/randomparity/kdive/issues/482)
- **Status:** Draft

## Problem

`runs.create`'s `build_profile` parameter is typed `BuildProfileInput = Mapping[str, object]`
(`src/kdive/profiles/types.py:11`), so FastMCP advertises a freeform `additionalProperties: true`
input schema with no field information. A caller discovers the required fields
(`schema_version`, `kernel_source_ref`, …) only by submitting `{}` and reading the validation
error. The validated models (`ServerBuildProfile` / `ExternalBuildProfile`,
`src/kdive/profiles/build.py:86-111`) are strict and complete but never reach the published
schema. There is also no documented path from a profile to a kdump + debuginfo kernel: `config`
is a `ComponentRef` resolving to the seeded `kdump` fragment when omitted (ADR-0096), but nothing
on the tool surface says so or points at `buildconfig.*`.

## Goal / acceptance

A caller can construct a valid `build_profile` — including selecting a kdump + debuginfo
`config` — from the tool schema/docs alone, without submitting `{}` to learn the fields.

## Approach

Mirror ADR-0124 (`systems.provision` typed `profile`) for the build lane. Three changes, all in
the issue's file scope (`registrar.py`, `middleware.py`, the param docstring, tests, and the
regenerated reference):

### 1. Type the parameter (registrar)

In `src/kdive/mcp/tools/lifecycle/runs/registrar.py`, change the `runs.create` `build_profile`
parameter annotation from `BuildProfileInput` to the union `ServerBuildProfile |
ExternalBuildProfile` (the existing `ParsedBuildProfile` alias). The wrapper re-serializes the
bound model to a dict via the existing `dump_build_profile(profile)` before constructing
`RunCreateRequest`, exactly how `systems.provision` calls `dump_profile(profile)`.

The `create_run` handler, `RunCreateRequest.build_profile: BuildProfileInput`, and
`BuildProfile.parse` are **unchanged** — the handler still parses the dict, so the single
parse + redaction boundary (ADR-0029) is preserved.

**Why a plain union, not discriminated:** `ServerBuildProfile.source` defaults to `"server"`;
`ExternalBuildProfile.source` is a required `Literal["external"]`. A Pydantic discriminated union
requires the discriminator on every member, so it is impossible here. A plain (smart) union
dispatches correctly — verified: server-default, explicit-server, external, and git-source inputs
each bind to the right model — and matches `BuildProfile.parse`'s default-`"server"` dispatch.

### 2. Re-envelope the binding error (middleware)

FastMCP validates a typed param at argument binding, before the tool body. Add one entry to
`_BINDING_CONVERSIONS` in `src/kdive/mcp/middleware.py`:

```python
"runs.create": _BindingConversion(
    "system_id", _loc_under("build_profile"), _build_profile_envelope
),
```

- `system_id` is the call's object id — matching the body path's
  `ToolResponse.failure_from_error(request.system_id, …)` on a profile parse failure
  (`create.py:106`). As for every typed-param tool, binding validation runs *before* the body, so
  a call with both a malformed `system_id` and a malformed `build_profile` reports the profile
  error at the boundary (object id = the raw caller-supplied `system_id` string), whereas the body
  would have returned the `system_id` UUID error first (`create.py:100-102`). This is consistent
  with the existing `systems.*` typed-profile tools and is not a regression; no leak (the object id
  is the caller's own string either way).
- `_loc_under("build_profile")` recognises the binding failure: the plain union's error `loc` is
  `("build_profile", "ServerBuildProfile" | "ExternalBuildProfile", …)`, all under
  `build_profile`, so `loc[0] == "build_profile"` holds for every entry.
- `_build_profile_envelope` is a small build-lane sibling of ADR-0124's `_profile_envelope`: same
  `configuration_error` + bounded `errors` list (no input/ctx echoed), but the message is
  `"invalid build profile"` so the binding-path `detail` is byte-identical to the in-body
  `BuildProfile.parse` failure (`profiles/build.py:151,159`). (`_profile_envelope` could not be
  reused verbatim — its `"invalid provisioning profile"` message **is** surfaced as `detail` for a
  `configuration_error`, since `suppressed_detail` only suppresses `authorization_denied`/
  `not_found`, so it would mislabel a build-profile failure.)

A field-level `ValidationError` the predicate rejects, or any non-`ValidationError`, propagates
unchanged.

### 3. Document the config-fragment path (param docstring + reference)

Expand the `build_profile` `Field(description=…)` to state that `config` is a `ComponentRef`
(concretely a `catalog` ref, e.g. `{"kind": "catalog", "provider": "system", "name": "kdump"}`),
that **omitting** `config` resolves to the seeded `kdump` catalog fragment
(`DEFAULT_CONFIG_REF`, ADR-0096) — which already carries the kdump + debuginfo options
(KEXEC / CRASH_DUMP / DEBUG_INFO_DWARF5 / GDB_SCRIPTS) — and that `buildconfig.get` retrieves a
named fragment's bytes + sha256 + merge recipe so a caller can inspect what they are selecting.
Cross-reference `docs/operating/build-source-staging.md` (#481/ADR-0136, the kernel *source* axis)
rather than duplicating it; this change owns the *config* axis. Regenerate the tool reference
(`just docs`).

> No `buildconfig.list` tool exists — only `buildconfig.get` (read, by name) and
> `buildconfig.set` (operator write). The docs must name only the real tools; for the default
> happy path a caller omits `config` entirely and gets the kdump fragment.

## Edge cases / failure modes

- **Empty `{}` profile:** binding `ValidationError` (missing `schema_version`/`kernel_source_ref`)
  → `configuration_error` envelope with field-path `errors`, object id = the call's `system_id`.
- **Unknown extra key:** `extra="forbid"` rejects at binding → same envelope. (Previously tolerated
  under `additionalProperties: true`, then caught by `parse()`; now caught one layer earlier — an
  intended tightening.)
- **`source="external"` with server fields:** binding error (external model forbids them) →
  envelope.
- **Git-source object:** `{"git": {"remote": …, "ref": …}}` binds to `ServerBuildProfile` with a
  `GitKernelSource` — schema discoverable from the published `anyOf`.
- **Non-mapping `build_profile`** (e.g. a string): FastMCP binding rejects it under
  `build_profile`; `_loc_under` matches → envelope.
- **Client schema rendering:** the FastMCP 3.4.0 client must render the `anyOf` input schema —
  verified in-tree with an in-memory `Client` (the ADR-0124 proof, now for a plain union).

## Out of scope

- A `runs`-side profile-examples discovery tool (mirroring `systems.profile_examples`) — the
  acceptance criterion is met by the published schema + docs.
- Any create-time rejection of URI-looking bare `kernel_source_ref` — owned and rejected by
  ADR-0136.
- Build-config *contents* correctness (`CONFIG_CRASH_DUMP` etc.) — preflighted by the builder
  against the tree (ADR-0029 §3), not at this boundary.

## Test plan

- **Binding-middleware unit test** (`tests/mcp/core/test_binding_error_middleware.py`): a
  malformed-`build_profile` binding `ValidationError` on `runs.create` becomes a
  `configuration_error` envelope with `object_id` = the call's `system_id` and a clean `errors`
  list; a non-`build_profile` `ValidationError` on `runs.create` is re-raised.
- **End-to-end client test**: drive the real `runs.create` tool through an in-memory FastMCP
  `Client` — a valid typed `build_profile` is accepted (the published input schema carries the
  union), a malformed one returns the envelope (not a `ToolError`).
- **Registrar/handler behaviour**: existing `runs.create` tests still pass (the handler contract is
  unchanged); a typed valid profile produces a `created` Run whose stored `build_profile` matches.
- **Doc guard**: `just docs-check` green after regenerating `runs.md`; `test_tool_docs` parameter
  + coverage guards pass.
- **Guardrails**: `just lint type test` and the full `just ci` set green.
