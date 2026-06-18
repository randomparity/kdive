# Spec — Discoverable upload-declaration input schema (#567)

- **Issue:** [#567](https://github.com/randomparity/kdive/issues/567)
- **ADR:** [ADR-0173](../adr/0173-upload-declaration-input-schema.md)
- **Date:** 2026-06-18

## Problem

`artifacts.create_run_upload` and `artifacts.create_system_upload` advertise their
`artifacts` items as a bare `Mapping[str, object]`
(`src/kdive/mcp/tools/catalog/artifacts/uploads.py:96-97`). FastMCP renders that as
`{"type": "object"}` with no `properties`, so a black-box MCP client cannot discover the
declaration shape from the tool schema. The required fields (`name`, `sha256`,
`size_bytes`) and the optional `chunks` sub-structure (`{sha256, size_bytes}` per chunk)
are enforced only at runtime and described only in prose
(`registrar.py:101-148`, `uploads.py:70,145-159,162-212`).

## Goals

1. The MCP input schema for an upload-declaration item exposes the required `name`,
   `sha256`, `size_bytes` fields.
2. The optional `chunks` schema is exposed, including each chunk's `sha256` and
   `size_bytes`.
3. The generated tool reference includes a single-PUT example and a chunked-upload
   example.
4. ADR-0166's structured rejection details (`data.reason`, `data.field`,
   `data.accepted_names`) remain available for malformed declarations.

## Non-goals

- No change to the runtime validators or the accepted artifact-name vocabulary.
- No change to the output (response) schema — that stays the central `build_app` envelope
  sweep (ADR-0170).
- No DB change, no migration.
- No digest-encoding (base64 vs hex) validation — out of scope, as in ADR-0166.

## Design

### Advertise, do not enforce, at the boundary

The handler and registrar parameter stay typed `Sequence[Mapping[str, object]]`. A strict
pydantic model or TypedDict would coerce/validate at the FastMCP transport boundary and
reject a malformed declaration with a generic `ValidationError` before the handler runs,
collapsing ADR-0166's self-correcting `bad_artifact_declaration` envelope (goal 4). Instead
the discovery schema rides on the `artifacts` `Field` via `json_schema_extra`, which merges
into the advertised array schema and replaces the empty default item with a fielded object
schema. Pydantic does not enforce `json_schema_extra`, so declarations still reach the
runtime validators unchanged.

### One shared schema constant

`uploads.py` gains a module-level `UPLOAD_DECLARATION_ITEM_SCHEMA` (the object schema above)
and an `UPLOAD_DECLARATION_FIELD_EXAMPLES` list (one single-PUT, one chunked). Both registrar
tools attach the same constants so run and system uploads advertise an identical shape and
examples. The schema's `required` list is built from `_REQUIRED_DECLARATION_FIELDS` so it
cannot drift from the validator.

### Generated-reference examples

`scripts/gen_tool_reference.py` renders only the top-level parameter table today. Extend the
`ParamDoc` model with an `examples` field and `render_namespace` to emit an "Examples" block
(a fenced ```json``` listing) under a parameter's row when the parameter schema carries
`examples`. `just docs` regenerates `artifacts.md` with the two examples.

## Acceptance / verification

- Unit test (`tests/mcp/lifecycle/test_create_upload_tool.py` or a registrar-schema test):
  build the app, read `artifacts.create_run_upload`/`create_system_upload` input schema, assert
  the `artifacts.items` object exposes `properties` for `name`/`sha256`/`size_bytes` with
  `required` covering all three, and a `chunks` array whose item exposes `sha256`/`size_bytes`.
- Drift-guard test: advertised `items.required` == sorted `_REQUIRED_DECLARATION_FIELDS`.
- Behavior test (existing): a declaration missing a required field still returns a
  `configuration_error` with `data.reason == "bad_artifact_declaration"`, `data.field`, and
  `data.accepted_names` — i.e. boundary validation did not pre-empt the handler.
- Generator test: a `ParamDoc` carrying `examples` renders an Examples block;
  `just docs-check` passes after `just docs`.
- `just ci` green.

## Rollback

Revert the registrar `Field` metadata, the `uploads.py` constants, and the generator change;
regenerate `artifacts.md`. No data or schema state to unwind.
