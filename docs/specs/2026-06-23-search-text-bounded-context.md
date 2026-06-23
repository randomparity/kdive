# Spec — `artifacts.search_text` bounded-context schema + named rejection (#733)

Tracks issue #733 (epic #736, defect D3). Decision recorded in
[ADR-0225](../adr/0225-search-text-bounded-context-schema.md).

## Problem

`artifacts.search_text` caps its three context-line parameters in
`security/artifacts/artifact_search.py::_bounded_int`:

| parameter      | min | max | default |
|----------------|-----|-----|---------|
| `before_lines` | 0   | 10  | 2       |
| `after_lines`  | 0   | 20  | 4       |
| `max_matches`  | 1   | 50  | 20      |

Two ergonomics defects make those caps unusable by a black-box MCP agent:

1. The tool schema advertises no `minimum`/`maximum` for these fields, so a client
   that introspects the schema cannot learn the cap — it can only probe.
2. An over-cap request is rejected with `configuration_error` /
   `data.reason = "bad_search_input"` and **no** `detail`. The handler's
   `except ArtifactSearchInputError:` clause drops the exception, whose message
   (`"before_lines out of range"`) already names the offending field. The agent must
   binary-search the working value.

## Requirements

- **R1 — Cap visible in schema.** The JSON Schema for `artifacts.search_text`
  advertises `minimum`/`maximum` for `before_lines`, `after_lines`, `max_matches`,
  and each parameter's description states its range. The generated tool reference
  (`docs/guide/reference/artifacts.md`) reflects the range text.
- **R2 — Named rejection.** An over-cap request returns a `configuration_error`
  with `data.reason = "bad_search_input"` (unchanged token) and a `detail` naming the
  offending field and the bound it violated.
- **R3 — Reason token stable.** The `bad_search_input` reason token is unchanged so
  existing callers/tests that key on it keep working.
- **R4 — No leak.** The rejection `detail` is built only from the field name (one of
  three literals) and the integer bound; it never interpolates caller free text, a
  secret, a host name, or an object-store key (ADR-0123).
- **R5 — Direct callers still protected.** The `search_text` library function keeps
  rejecting out-of-range integers (its direct, non-MCP callers are not protected by
  the tool schema). The schema/model bound and the runtime bound are asserted equal.

## Design

The tool's parameters are declared in two places:

- the `@app.tool(name="artifacts.search_text")` **function signature** in
  `mcp/tools/catalog/artifacts/registrar.py` — this is the schema FastMCP exposes to
  clients; and
- the `ArtifactSearchRequest` Pydantic model in
  `mcp/tools/catalog/artifacts/reads.py` — the internal request object.

### Schema constraints (R1, R5)

Add `ge=`/`le=` and range-stating descriptions to the three context parameters on
**both** the registrar signature and `ArtifactSearchRequest`. FastMCP renders
`ge=`/`le=` as `minimum`/`maximum` in the exposed schema (verified empirically).

### Binding-time re-envelope (R2, R3, R4)

Because the schema now carries `ge=`/`le=`, FastMCP rejects an over-cap argument with
a Pydantic `ValidationError` at **arg-binding time** — before the tool function body
runs — so the handler's `except ArtifactSearchInputError:` never sees it. The
existing `BindingErrorMiddleware` (`mcp/middleware/binding_errors.py`) is the seam
that re-envelopes such typed binding errors. Register a conversion for
`artifacts.search_text`:

- **match:** every error entry has `loc[0]` in
  `{before_lines, after_lines, max_matches}` and a numeric-range `type`
  (`greater_than_equal` / `less_than_equal`).
- **build:** a `CONFIGURATION_ERROR` with `data.reason = "bad_search_input"` and a
  `detail` of the form `"<field> must be between <ge> and <le>"`, reconstructed from
  the first matching error's `loc` and the parameter's known bounds. The offending
  input value is **not** echoed into `detail` — the detail is a pure fixed template of
  field name plus the two integer bounds, so R4 holds with no caller-derived content at
  all. The object id is `artifact_id` (or the tool name when absent), matching the
  existing conversions.

The two bound sources are central constants so the registrar signature, the model,
the middleware detail, and the runtime `_bounded_int` all read the same numbers.

**Non-goal — type-coercion errors stay out of scope.** A non-integer context value
(e.g. `before_lines: "abc"` or `3.5`) produces a Pydantic `int_parsing` /
`int_from_float` error under the same `loc` but with a *different* error `type` and no
range `ctx`. The conversion deliberately matches **only** the numeric-range error
types (`greater_than_equal` / `less_than_equal`), so a type-coercion error does not
become a `bad_search_input` envelope — it keeps FastMCP's default binding behavior.
#733 is strictly about the undocumented *caps*; a wrong *type* was never the defect,
the MCP transport's JSON typing already constrains it, and re-enveloping it would be a
separate (and broader) decision. A test asserts the predicate ignores an
`int_parsing` error under a context field so this boundary cannot silently widen.

### Defense-in-depth (R5)

`_bounded_int` and `ArtifactSearchInputError` stay. The handler's existing
`except ArtifactSearchInputError: return _config_error(..., data={"reason":
"bad_search_input"})` remains the path for a direct caller or a value that bypasses
schema binding. A test asserts the schema/model bounds equal the `_bounded_int`
bounds.

## Test plan

- **Schema (R1):** build the app; assert the `artifacts.search_text` parameter schema
  carries the expected `minimum`/`maximum` for each context field and that the
  description states the range.
- **Named rejection (R2, R3):** drive an over-cap call through the binding seam (a
  `ValidationError` shaped like FastMCP's) and assert the envelope is
  `configuration_error`, `data.reason == "bad_search_input"`, and `detail` contains
  the offending field name and its bound. One case per field; both the low (`ge`) and
  high (`le`) edge for at least one field.
- **No leak (R4):** assert the `detail` is the fixed `"<field> must be between <ge>
  and <le>"` template (field name + two integer bounds only; no caller-supplied value
  echoed).
- **Type-coercion non-goal:** assert the conversion predicate does **not** match an
  `int_parsing` error under a context field (a non-integer `before_lines` is not
  re-enveloped as `bad_search_input`).
- **Direct caller (R5):** `search_text(...)` with an out-of-range value still raises
  `ArtifactSearchInputError` naming the field; assert the schema/model and
  `_bounded_int` bounds are equal.
- **Regression:** the generated `docs/guide/reference/artifacts.md` is regenerated and
  the existing `test_artifacts_search_text_*` suite stays green (boundary values
  `before_lines=10`, `max_matches=50` etc. accepted).
