# Tighten tool input schemas and per-tool scope boundaries

- **Issue:** #507
- **ADR:** [0147](../adr/0147-tool-schema-and-scope-boundary-tightening.md)
- **Status:** Accepted
- **Date:** 2026-06-17

## Problem

Two narrow gaps in the per-tool metadata the model reads in `list_tools` make it guess
more than it should:

1. `systems.list`'s `state` filter is advertised as a bare string, though its valid set
   is the `SystemState` enum. An invalid value is rejected only *after* binding, so the
   model never sees the legal values up front.
2. The confusable `systems.define` / `systems.provision` / `systems.provision_defined`
   lifecycle trio carries terse per-tool docstrings with no when-not-to-use / use-X
   guidance, even though the module header has exactly that.

This is closing two known gaps, not a rewrite. The surface already does tight schemas
(strict Pydantic models, discriminated unions) and scoped module docstrings well
elsewhere.

## Scope

In scope:

- `systems.list` `state` filter → `SystemState` enum on the `@app.tool` wrapper.
- Scope-boundary per-tool docstrings for the `systems.*` lifecycle mutation family
  (`define`, `provision`, `provision_defined`, `reprovision`).
- Extend the `test_tool_docs` guard to pin both.

Explicitly out of scope (with rationale, settled in ADR-0147):

- `shape` stays `str | None` — runtime-mutable catalog (`shapes.set`), no code enum.
- `pcie` stays `str | None` — structured open `<vendor>:<device>` format.
- The other ~90 tools' docstrings — broader coverage is follow-up; this change is
  scoped to the family the issue exemplifies and already touches.

## Design

### Task 1 — `state` enum at the schema layer

FastMCP preserves enum constraints in the advertised input schema when the wrapper
parameter is annotated with the enum (verified empirically: a `SystemState | None`
wrapper param renders `{"anyOf": [{"enum": [...7 states...], "type": "string"}, {"type":
"null"}]}`, and the enum class docstring rides along as the schema `description`). So:

- `systems_list`'s `state` parameter annotation changes from `str | None` to
  `SystemState | None`. The `Field(description=...)` is preserved.
- The wrapper passes the value straight into `SystemsListRequest(state=...)`. Because
  `SystemState` is a `StrEnum`, the value is a `str` subtype and flows unchanged through
  the existing `str | None` payload field and the `SystemState(state)` call in
  `_build_filters` (which returns the member as-is for an enum input).
- The post-binding `SystemState(state)` guard and its `configuration_error` branch stay
  — they cover direct handler callers (the unit tests drive `list_systems` with a raw
  string and must still get the envelope).

### Task 2 — scope-boundary docstrings

Fold the `provision.py` module-header guidance into the four per-tool docstrings, one to
two sentences each, each naming the alternative tool:

- `systems.define` — name `systems.provision` as the no-upload-window alternative.
- `systems.provision` — name `systems.define` + `systems.provision_defined` for the
  upload-window path.
- `systems.provision_defined` — name `systems.define` as its prerequisite.
- `systems.reprovision` — distinguish from `systems.provision` (in-place vs new System).

### Guard tests (extend `tests/mcp/core/test_tool_docs.py`)

- `test_systems_list_state_filter_is_enum_constrained`: the `state` param schema
  exposes exactly the `SystemState` values; `shape` and `pcie` expose no `enum`
  (plain string), pinning the open-vs-closed decision.
- `test_confusable_systems_tools_name_their_alternative`: each of the four
  descriptions contains the alternative tool name (substring assertion, mirroring
  `test_run_cmdline_docs_describe_debug_args_only`).

## Acceptance criteria

- `systems.list`'s `state` filter rejects an invalid value at the schema layer (enum in
  the advertised input schema); `shape` / `pcie` remain bare strings.
- The four confusable `systems.*` tool descriptions state when not to use them and name
  the alternative.
- `test_tool_docs` guards both and the full `just ci` suite is green.

## Verification

- Unit: the two new guard tests + the existing `systems.list` handler tests
  (`test_systems_list.py`) stay green (the post-binding guard is unchanged, so
  `test_unknown_state_is_config_error` / `test_state_filter_rejects_invalid_values`
  still pass when driving the handler directly).
- Generated tool reference regenerates and is committed.
