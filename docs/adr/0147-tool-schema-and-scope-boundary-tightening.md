# ADR 0147 — Tighten tool input schemas and per-tool scope boundaries

- **Status:** Accepted
- **Date:** 2026-06-17
- **Deciders:** kdive maintainers

## Context

A review of the MCP surface against tool-design dimensions *tight input schemas* and
*descriptions with scope boundaries* (#507) found two narrow gaps in the per-tool
metadata the model reads in `list_tools`:

1. **A closed-value-set filter typed as a bare string.** `systems.list`'s `state`
   filter is `str | None` on the `@app.tool` wrapper
   (`lifecycle/systems/registrar.py`). The valid set is the `SystemState` StrEnum,
   but the wrapper advertises only `{"type": "string"}`, so an invalid value is
   rejected *after* binding — `_build_filters` does `SystemState(state)` and returns a
   `configuration_error` envelope. The model never sees the legal values up front and
   must guess. FastMCP already preserves enum constraints (and the enum class
   docstring) in the advertised input schema when the wrapper annotation is the enum,
   so the fix is to lift the type to the wrapper, not to add machinery.

2. **Per-tool docstrings without scope boundaries for confusable tools.** The
   `systems.*` *module* docstring family states when-to / when-not and names
   alternatives, but `list_tools` shows the **per-tool** docstrings, which are terse
   one-liners. The `systems.define` / `systems.provision` / `systems.provision_defined`
   trio is the clearest mis-sequencing hazard: `define` opens a pre-provision rootfs
   upload window, `provision` mints-and-enqueues directly, and `provision_defined`
   admits a `defined` System only after its upload window closes. A one-liner does not
   tell the model which to pick or in what order.

Two sibling filters look superficially similar to `state` but are **not** closed
compile-time value sets:

- **`shape`** is a runtime-mutable catalog. Shapes are DB rows seeded by migration
  0013 and mutated at runtime through the `shapes.set` / `shapes.delete` tools; there
  is no shape enum in code. A `Literal` would hardcode the current seed and reject a
  shape an operator added through `shapes.set` at the schema layer — a correctness
  regression and a code↔data drift source.
- **`pcie`** is a structured open format (`<vendor>:<device>` hex), parsed by
  `parse_match_spec`. Its value set is open, like the `object_id: str` UUID case #507
  explicitly carves out as acceptable.

## Decision

Two scoped, additive changes; no schema-version, migration, DB, auth, or entrypoint
change.

1. **Lift `systems.list`'s `state` filter to the `SystemState` enum on the wrapper.**
   The `@app.tool` wrapper parameter becomes `SystemState | None`, so the advertised
   input schema carries the enum and an invalid value is a schema-layer validation
   error the model sees before invocation. The post-binding `SystemState(state)` guard
   in `_build_filters` stays as defense-in-depth (a caller driving the handler
   directly, e.g. a unit test, still gets the `configuration_error` envelope).
   `shape` and `pcie` stay `str | None` for the reasons above. `SystemsListRequest`
   stays `str | None`-typed: it is the handler-boundary payload, decoupled from the
   wire enum, and `SystemState` is a `StrEnum` so an enum value flows through it and
   into `SystemState(state)` unchanged.

2. **Fold scope boundaries into the confusable per-tool docstrings.** The
   `systems.define` / `systems.provision` / `systems.provision_defined` /
   `systems.reprovision` docstrings each state when *not* to use the tool and name the
   alternative, drawn from the existing `provision.py` module header. Wording stays
   one or two sentences so the `list_tools` projection stays terse.

The doc guard (`tests/mcp/core/test_tool_docs.py`) gains:
- a check that `systems.list`'s `state` parameter advertises the full `SystemState`
  enum and that `shape` / `pcie` remain plain strings (pinning the open-vs-closed
  decision so a future change cannot silently enum-ify a runtime-mutable catalog), and
- a check that each confusable `systems.*` tool description names its alternative
  tool (mirroring the existing `test_run_cmdline_docs_describe_debug_args_only`
  substring style).

## Consequences

- The model sees the legal `state` values (and the lifecycle prose the enum docstring
  carries) before calling `systems.list`; an invalid `state` fails at the schema layer
  instead of after a round trip.
- `shape` adding/removal through `shapes.set` stays filterable on `systems.list` with
  no code change, because its filter is not pinned to a compile-time set.
- The four confusable `systems.*` docstrings carry a maintenance obligation: a new
  sibling tool, or a renamed alternative, must keep the cross-reference accurate, which
  the new guard test enforces.
- No behavior change to the handler, the SQL, or the failure envelope a direct caller
  receives. The generated tool reference regenerates (the `systems.list` `state`
  schema and four docstrings change).

## Considered & rejected

- **Type `shape` as a `Literal` / enum of the seeded shape names.** Shapes are
  runtime-mutable via `shapes.set` and live only as DB rows; a compile-time set would
  reject operator-added shapes at the schema layer and drift from the migration seed.
  Rejected — `shape` is an open value set, documented as such.
- **Type `pcie` as an enum.** It is a structured `<vendor>:<device>` format, not an
  enumerable set; #507's own scope note treats such open formats (like UUIDs) as
  acceptable bare strings. Rejected.
- **Drop the post-binding `SystemState(state)` guard now that the schema rejects bad
  values.** The guard also covers direct handler callers (unit tests, future internal
  callers) that bypass the wire schema; keeping it costs nothing and preserves the
  malformed-input envelope contract. Rejected.
- **Rewrite every terse per-tool docstring across all ~90 tools.** #507 asks to
  prioritize the most confusable / mis-sequenced tools, not a blanket rewrite. Scoping
  to the `systems.*` lifecycle family (the issue's own example, and the surface this
  change already touches) keeps the change bounded and reviewable. Rejected as scope
  creep; broader coverage is follow-up work.
