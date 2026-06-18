# Spec — Advertise a fielded, non-recursive `ToolResponse` output schema

- **Issue:** [#565](https://github.com/randomparity/kdive/issues/565)
- **ADR:** [ADR-0170](../adr/0170-fielded-tool-output-schema.md)
- **Date:** 2026-06-18
- **Status:** Proposed

## Problem

Every MCP tool advertises the flat constant `{"type": "object"}` as its `outputSchema`
([ADR-0113](../adr/0113-flat-tool-output-schema.md), `src/kdive/mcp/app.py`
`ENVELOPE_OUTPUT_SCHEMA`). That constant was chosen to remove a recursive `$ref` schema
that broke the FastMCP 3.4.0 client's per-call `TypeAdapter`. It works, but it tells a
black-box agent nothing: from `tools/list` alone the agent cannot learn that a result
carries `object_id`, `status`, `suggested_next_actions`, `refs`, `error_category`,
`retryable`, `detail`, `data`, and `items`. The envelope is the one contract that is
identical across all 109 tools, yet it is the one thing the advertised schema hides
(`TOOL_ASSESSMENT.md` finding F1).

The recursion ADR-0113 worked around comes from exactly two `ToolResponse` fields
(`src/kdive/mcp/responses.py`):

- `items: list[ToolResponse]` — a direct self-reference.
- `data: dict[str, JsonValue]` — `JsonValue` is a recursive union.

FastMCP auto-derives the full model schema, which emits self-referential `$ref`s in
`$defs`; the client cannot build a validator for that and nulls `CallToolResult.data`.

## Goal

Replace the flat `{"type": "object"}` constant with a single **non-recursive** schema that
documents the top-level envelope fields, swept onto every tool through the same `build_app`
chokepoint and zero-count guard ADR-0113 established. A black-box agent reading `tools/list`
learns the envelope shape; the FastMCP 3.4.0 client still builds a validator and populates
`.data`.

## Non-goals

- **No change to the runtime `ToolResponse` model or the `structured_content` wire payload.**
  Only the *advertised* `outputSchema` changes. ADR-0113's payload-level invariants
  (`validate_json_value`, the `structured_content` shape pinned by `LiveStackClient`) are
  untouched.
- **No per-tool output schema.** The per-tool shape of `data`/`items` stays intentionally
  open (a generic object / array of objects). Documenting the *envelope* is the goal, not
  enumerating each tool's payload keys.
- **No new migration, no new state, no auth change.**

## Why this revisits ADR-0113

ADR-0113 listed "Hand-write a full non-recursive `ToolResponse` schema" under
*Alternatives considered* and rejected it for two reasons. This spec adopts that
alternative; ADR-0170 supersedes the rejection by answering both objections head-on.

1. **"It must stay permissive enough that the client's `validate_python` never rejects a
   real payload."** The schema is deliberately permissive: no top-level
   `additionalProperties: false`, no `required` list, nullable fields are typed
   `["<type>", "null"]`, and the two recursive fields collapse to generic shapes
   (`data` → `{"type": "object"}`, `items` → an array of `{"type": "object"}`). A
   round-trip test drives a real success, collection, and failure envelope through the
   FastMCP client and asserts `.data` is populated with no parse-error log — proving the
   client accepts every real envelope.
2. **"It drifts silently whenever `ToolResponse` gains a field."** A drift-guard test
   asserts the advertised schema's `properties` keys exactly equal `ToolResponse.model_fields`.
   Adding an envelope field (as ADR-0123 added `detail`) fails that test loudly and forces
   the schema update in the same change. Silent drift becomes a red test.

ADR-0113's other rejected alternatives stay rejected and this spec does not reopen them:
`output_schema=None` (less informative), per-`@app.tool` overrides (scattered across 96
registrations), and overriding `ToolResponse.__get_pydantic_json_schema__` (changes the
shared model for every consumer, not just the MCP boundary).

## Behavior

### The schema (`ENVELOPE_OUTPUT_SCHEMA`, `src/kdive/mcp/app.py`)

A flat object schema with a `properties` entry per envelope field and **no `$defs` / no
`$ref`**:

| Property | Advertised JSON type | Source field |
|---|---|---|
| `object_id` | `{"type": "string"}` | `object_id: str` |
| `status` | `{"type": "string"}` | `status: str` |
| `suggested_next_actions` | `{"type": "array", "items": {"type": "string"}}` | `list[str]` |
| `refs` | `{"type": "object", "additionalProperties": {"type": "string"}}` | `dict[str, str]` |
| `error_category` | `{"type": ["string", "null"]}` | `str \| None` |
| `retryable` | `{"type": ["boolean", "null"]}` | `bool \| None` |
| `detail` | `{"type": ["string", "null"]}` | `str \| None` |
| `data` | `{"type": "object"}` | `dict[str, JsonValue]` (recursion broken) |
| `items` | `{"type": "array", "items": {"type": "object"}}` | `list[ToolResponse]` (recursion broken) |

The top level is `{"type": "object", "properties": {...}, "description": "..."}`. The
`description` names the envelope and points to the doc resource (below). No field is
`required` and `additionalProperties` is left at its permissive default, so a future field
that exists on a payload but not yet on the schema cannot make the client reject it — only
the drift-guard test fails, deliberately.

`items` advertises an array of generic objects rather than a self-`$ref`: this is what
breaks the recursion. The doc resource explains that each item is itself a `ToolResponse`.

### The sweep (`_advertise_envelope_output_schema`, `src/kdive/mcp/app.py`)

Unchanged in structure from ADR-0113: iterate the live `app.local_provider._components`,
set `output_schema = dict(ENVELOPE_OUTPUT_SCHEMA)` on each `Tool`, raise if zero tools were
swept. Only the constant it writes changes. The helper is renamed from
`_advertise_flat_output_schema` to `_advertise_envelope_output_schema` to match the new
behavior; its call site in `build_app` and the two tests that import it are updated.

Each tool gets a fresh `dict(ENVELOPE_OUTPUT_SCHEMA)` shallow copy as before; because the
nested values (`properties`, the array `items` dicts) are shared sub-dicts, the swept tools
must treat the schema as read-only — they never mutate it, matching current behavior.

### AC#4 — the doc resource

`docs/guide/response-envelope.md` already documents the envelope but is stale: it types
`data` as `dict[str, str]` and omits `items`, `detail`, and `retryable`. Update it to match
the model and add a section, **"Reading an open payload,"** explaining that `data` and
`items` are advertised as generic shapes on purpose — `data` carries plane-specific scalars,
each `items` entry is a nested `ToolResponse`, and `refs` are object-store keys resolved via
`artifacts.get`, never inline bytes.

Register that doc as an MCP resource through the existing ADR-0151 allowlist
(`src/kdive/mcp/resources/registrar.py` `DOC_RESOURCES`) at
`resource://kdive/docs/guide/response-envelope.md`, and regenerate the packaged snapshot
(`just resources-docs`). `tools/list`'s envelope-schema `description` references this URI, so
an agent that wants the open-payload semantics has a discoverable, stable pointer.

## Success criteria (falsifiable)

Tests live in `tests/mcp/core/test_output_schema.py` (the ADR-0113 suite) unless noted.

1. **AC#1 — fielded schema advertised.** After the sweep, every probe tool's `outputSchema`
   has `properties` containing all nine envelope field names, and the top level is
   `{"type": "object"}` (not the bare flat constant). Replaces the old
   `test_detail_field_is_not_an_advertised_output_property` assertion that `"properties"` is
   absent.
2. **AC#1 — real `build_app` surface.** `test_real_build_app_tools_advertise_*` in
   `tests/mcp/core/test_tool_wrapper_boundary.py` asserts every real `build_app` tool
   advertises the fielded schema (replacing the `== {"type": "object"}` assertion).
3. **AC#2 — no recursion, client parses.** A success, a collection (non-empty `items`), and
   a failure envelope each round-trip through a FastMCP `Client.call_tool`: `.data` is a
   populated dict and no "structured content" parse-error is logged. Proves the new schema
   does not reintroduce the ADR-0113 break and that `validate_python` accepts real payloads.
4. **AC#2 — schema is `$ref`-free.** The advertised schema serialized to JSON contains no
   `"$ref"` and no `"$defs"` key (a structural pin on "non-recursive").
5. **AC#3 — drift guard.** `set(schema["properties"]) == set(ToolResponse.model_fields)`.
   Adding or removing an envelope field without updating `ENVELOPE_OUTPUT_SCHEMA` fails this
   test.
6. **Zero-count guard retained.** The empty-surface `RuntimeError` test still passes against
   the renamed helper.
7. **AC#4 — doc resource reachable.** A `build_app`-backed test (or the existing resource
   listing test) shows `resource://kdive/docs/guide/response-envelope.md` is advertised, and
   `just resources-docs-check` passes (snapshot regenerated).

## Guardrails

`just ci` (lint, type, lint-shell, lint-workflows, check-mermaid, docs-*, resources-docs-check,
test) green before every commit. The schema constant and helper stay ≤100 lines/function,
cyclomatic ≤8, absolute imports, Google-style docstring on the helper, 100-char lines, plain
factual prose per the repo doc-style convention.
