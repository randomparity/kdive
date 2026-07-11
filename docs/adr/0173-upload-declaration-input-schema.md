# ADR 0173 — Discoverable upload-declaration input schema

- **Status:** Accepted <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-06-18
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0048](0048-external-build-artifact-ingestion.md)
  (the external-upload build lane and its declaration shape),
  [ADR-0104](0104-chunked-external-upload-reassembly.md) (the client-split chunked-upload
  declaration with per-chunk `sha256`/`size_bytes`),
  [ADR-0166](0166-upload-artifact-error-detail.md)
  (the self-correcting `bad_artifact_declaration` rejection + `artifacts.expected_uploads`
  discovery tool — this ADR completes the input-schema item ADR-0166 deferred),
  [ADR-0170](0170-fielded-tool-output-schema.md) (the fielded — not flat — schema posture;
  this ADR applies the same fielded principle to the upload-declaration *input* item),
  [ADR-0047](0047-agent-facing-tool-guide-generation.md) (the generated tool reference the
  new examples render into).
- **Spec:** [`../specs/2026-06-18-upload-declaration-input-schema.md`](../design/2026-06-18-upload-declaration-input-schema.md)
- **Issue:** [#567](https://github.com/randomparity/kdive/issues/567)

## Context

`artifacts.create_run_upload` and `artifacts.create_system_upload` accept a list of
artifact upload declarations. Each declaration is required to carry `name`, `sha256`, and
`size_bytes`, and may carry a `chunks` list (each chunk a `{sha256, size_bytes}` mapping)
for the chunked-upload path (ADR-0104). The runtime validators enforce all of this
(`uploads.py` `_validate_one_declaration` / `_validate_chunks`).

But the *advertised* MCP input type for a declaration item is `Mapping[str, object]`
(`uploads.py:96-97` `type ArtifactDeclaration = Mapping[str, object]`). FastMCP renders
that as a bare `{"type": "object"}` item with no `properties`, so a black-box client
inspecting the tool schema cannot discover the declaration shape — it learns the field
names only by reading the prose description or by trial-and-error against the runtime
rejection. ADR-0166 made the *rejection* self-correcting and added a name-vocabulary
discovery tool, but explicitly deferred the input-schema work ("A schema enum on the
`artifacts` field … is a larger FastMCP change than #551 needs"). #567 is that follow-up.

The constraint that shapes the decision: ADR-0166's structured rejection details
(`data.reason`, `data.field`, `data.accepted_names`) must remain reachable. If the input
schema were enforced as a strict pydantic model at the FastMCP transport boundary, a
declaration missing a required field would be rejected with a generic pydantic
`ValidationError` *before* reaching the handler — collapsing the self-correcting
`bad_artifact_declaration` envelope into an opaque protocol-level error and regressing
ADR-0166. The schema must therefore be **advertised for discovery but not boundary-enforced**.

## Decision

### 1. Advertise the item schema without boundary enforcement

Keep the handler/registrar parameter runtime type as `Sequence[Mapping[str, object]]`
(unchanged — no boundary coercion, so every declaration still flows through the existing
runtime validators that produce ADR-0166's structured rejections). Attach the
discovery schema to the `artifacts` `Field` via pydantic's `json_schema_extra`, which
merges into the advertised array schema and replaces the empty default item with a fielded
object schema:

```json
{ "type": "object",
  "required": ["name", "sha256", "size_bytes"],
  "properties": {
    "name":       {"type": "string",  "description": "…accepted names…"},
    "sha256":     {"type": "string",  "description": "Base64-encoded SHA-256 of the whole object."},
    "size_bytes": {"type": "integer", "description": "Total object size in bytes."},
    "chunks": {"type": "array", "description": "…optional chunked-upload parts…",
               "items": {"type": "object", "required": ["sha256", "size_bytes"],
                         "properties": {"sha256": {"type": "string", …},
                                        "size_bytes": {"type": "integer", …}}}}}}
```

The schema is built once as a module constant in `uploads.py` (`UPLOAD_DECLARATION_ITEM_SCHEMA`)
and reused by both registrar tools so run and system uploads advertise the identical shape.
A drift-guard test asserts the advertised `required` keys equal `_REQUIRED_DECLARATION_FIELDS`,
so the schema can never silently diverge from what the validator enforces.

### 2. Render single-PUT and chunked examples into the generated reference

The tool reference generator (ADR-0047) renders only the top-level parameter table; it does
not descend into `items`. Extend it so a parameter whose schema carries `examples` emits an
"Examples" block (a fenced ```json``` listing) beneath the parameter table. Attach two
examples to the `artifacts` field via `json_schema_extra` — one single-PUT declaration and
one chunked declaration — so `just docs` regenerates `artifacts.md` with both, satisfying the
acceptance criterion without hand-editing the generated file.

## Alternatives considered

- **A strict pydantic model / TypedDict for the item (boundary-enforced).** Rejected: it
  validates at the FastMCP transport boundary and rejects a malformed declaration with a
  generic pydantic `ValidationError`, bypassing ADR-0166's `bad_artifact_declaration`
  envelope (`data.reason`/`field`/`accepted_names`). That regresses the explicit #567
  acceptance criterion that those details remain available. `json_schema_extra` advertises
  the same `properties`/`required` for discovery while leaving runtime validation in the
  handler where the structured rejections live.
- **Put examples only in the parameter `description`.** Rejected: the generator forbids `|`
  and newline characters in a description (they break the Markdown table), so a multi-line
  JSON example cannot live there. A structured `examples` block in the schema, rendered by
  the generator, is both machine-discoverable and human-readable.
- **A recursive/nested output schema.** Not applicable — this is an *input* schema. The
  output schema stays the central `build_app` envelope sweep (ADR-0170); the input change
  is independent of it.

## Consequences

- A black-box MCP client can discover the upload-declaration shape (required `name`,
  `sha256`, `size_bytes`; optional `chunks` with per-chunk `sha256`/`size_bytes`) directly
  from the tool input schema, and the generated reference shows a single-PUT and a chunked
  example.
- ADR-0166's self-correcting rejections are unchanged: declarations still reach the runtime
  validators, which still emit `data.reason`/`field`/`accepted_names`.
- No DB change, no migration. The change is the registrar `Field` metadata, one module-level
  schema constant, and a generator enhancement. The advertised input schema is additive
  detail over the prior bare object; existing callers passing valid mappings are unaffected.
- The generated tool reference is regenerated; the doc-style/doc-check CI gates cover it.
