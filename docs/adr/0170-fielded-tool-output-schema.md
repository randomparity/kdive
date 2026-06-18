# ADR 0170 — Advertise a fielded, non-recursive `ToolResponse` output schema

- **Status:** Accepted <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-06-18
- **Deciders:** KDIVE maintainers
- **Builds on (revisits):** [ADR-0113](0113-flat-tool-output-schema.md) (the flat
  `{"type": "object"}` output schema — superseded *only* at the advertised-schema level; its
  central `build_app` sweep, zero-count guard, and payload/`structured_content` invariants are
  retained). Unchanged: [ADR-0019](0019-tool-response-envelope.md) (the `ToolResponse` model)
  and [ADR-0151](0151-mcp-doc-resources.md) (the doc-resource allowlist).
- **Issue:** [#565](https://github.com/randomparity/kdive/issues/565).
- **Spec:** [`../specs/2026-06-18-fielded-tool-output-schema.md`](../specs/2026-06-18-fielded-tool-output-schema.md).

## Context

ADR-0113 replaced FastMCP's auto-derived `ToolResponse` schema with the flat constant
`{"type": "object"}`, advertised on every tool. The auto-derived schema was self-referential
(`items: list[ToolResponse]` and `data: dict[str, JsonValue]` produce `$ref`s in `$defs`),
and the FastMCP 3.4.0 client cannot build a `TypeAdapter` for a recursive `$ref` — it logs a
per-call parse error and nulls `CallToolResult.data`. The flat constant removed the recursion
and restored `.data`.

The cost ADR-0113 explicitly accepted: the advertised schema "is now uninformative about
envelope fields (it says only 'an object')." A black-box agent reading `tools/list` therefore
cannot discover that every result carries `object_id`, `status`, `suggested_next_actions`,
`refs`, `error_category`, `retryable`, `detail`, `data`, and `items` — the single contract
shared by all 109 tools (`TOOL_ASSESSMENT.md` finding F1). ADR-0113 considered, and rejected,
hand-writing a full non-recursive schema for two reasons:

1. It might over-constrain and make the client's `validate_python` reject a real payload.
2. It would drift silently whenever `ToolResponse` gained a field.

Both objections are answerable without changing the runtime model, so we revisit the decision.

## Decision

Replace the flat constant with a fielded, **non-recursive** `ENVELOPE_OUTPUT_SCHEMA` that
documents every top-level envelope field, swept onto every tool through the same `build_app`
chokepoint and zero-count guard ADR-0113 established. The two recursive fields collapse to
generic shapes that carry no `$ref`:

- `data` → `{"type": "object"}`.
- `items` → `{"type": "array", "items": {"type": "object"}}` (each element is itself a
  `ToolResponse`, documented in the doc resource, not advertised as a self-`$ref`).

All other fields are advertised with their concrete JSON types; nullable fields use
`{"type": ["<type>", "null"]}`. The top level is `{"type": "object", "properties": {...},
"description": ...}` with **no `$defs`, no `$ref`, no `required`, and no
`additionalProperties: false`**. The runtime `ToolResponse` model, the `structured_content`
wire payload, and the `validate_json_value` runtime JSON-safety check are unchanged — only
the advertised `outputSchema` changes. The sweep helper is renamed
`_advertise_flat_output_schema` → `_advertise_envelope_output_schema`.

We answer ADR-0113's two objections with tests rather than prose:

1. **Permissiveness.** No `required`, no `additionalProperties: false`, nullable types, and
   generic recursive fields keep the schema strictly more permissive than the real payloads.
   A round-trip test drives a real success, collection, and failure envelope through a FastMCP
   `Client` and asserts `.data` is populated with no parse-error log.
2. **Silent drift.** A drift-guard test asserts the schema's `properties` keys equal
   `ToolResponse.model_fields`. A new envelope field fails the test loudly and must be added to
   the schema in the same change.

AC#4: `docs/guide/response-envelope.md` is corrected (it typed `data` as `dict[str, str]` and
omitted `items`/`detail`/`retryable`) and registered as an MCP doc resource via the ADR-0151
allowlist at `resource://kdive/docs/guide/response-envelope.md`; the schema's top-level
`description` points to that URI.

## Consequences

- A black-box agent learns the envelope shape from `tools/list` alone: which fields exist,
  which are nullable, and that `data`/`items` are intentionally open.
- The FastMCP 3.4.0 client builds a validator with no recursion and no parse error.
  `structured_content` is byte-identical to ADR-0113 — that is the compatibility guarantee,
  so `LiveStackClient` (which reads `structured_content`) and the `structured_content`-shape
  pin test are unaffected. The Python *type* of `CallToolResult.data` does change: because the
  schema now carries `properties`, the client deserializes `.data` into a generated pydantic
  model (attribute access, `result.data.object_id`) instead of leaving it the plain dict the
  bare `{"type": "object"}` schema produced. A consumer that subscripted `.data` directly must
  switch to attribute access or read `structured_content`; no in-repo consumer does (the
  live-stack client already reads `structured_content`).
- The single `build_app` sweep still covers every current and future tool; the zero-count
  guard still fails loud if the FastMCP registry accessor changes under us.
- The advertised `data`/`items` shapes are generic. An agent that needs the per-payload keys
  reads the per-plane tool docs or the registered envelope doc resource; the advertised schema
  documents the *envelope*, not each tool's payload, by design.
- The hand-written schema and the model can still diverge in principle, but the drift-guard
  test converts that from a silent interop regression (ADR-0113's objection) into a failing
  test on the next field addition.

## Alternatives considered

- **Keep the flat `{"type": "object"}` (ADR-0113 status quo).** Rejected: it satisfies the
  client but hides the envelope, which is the whole of #565.
- **Advertise `items` as a self-`$ref` to the full envelope.** Most precise, but reintroduces
  exactly the recursive `$ref` that breaks the FastMCP 3.4.0 client (ADR-0113). Rejected; the
  generic-object array is the recursion-free substitute, with the nesting explained in prose.
- **Derive the schema programmatically from `ToolResponse.model_json_schema()` and strip the
  recursion.** Stays in sync automatically, but requires fragile surgery against pydantic's
  internal `$defs`/`$ref` output on every pydantic upgrade, and the result is harder to read
  than a literal. A small literal constant plus the drift-guard test is simpler and gives the
  same protection. Rejected.
- **Override `ToolResponse.__get_pydantic_json_schema__`.** Rejected for the same reason as in
  ADR-0113: it changes `model_json_schema()` for every consumer, not just the MCP boundary.
- **`output_schema=None`.** Rejected for the same reason as in ADR-0113: it advertises no
  schema at all, strictly less informative than a fielded object.
