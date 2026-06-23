# ADR 0225 — `artifacts.search_text` bounded-context schema and named rejection

- **Status:** Accepted <!-- Proposed | Accepted | Rejected | Superseded by NNNN -->
- **Date:** 2026-06-23
- **Deciders:** kdive maintainers

## Context

`artifacts.search_text` (ADR-0064) enforces caps on its context-line parameters —
`before_lines` 0–10, `after_lines` 0–20, `max_matches` 1–50 — inside
`security/artifacts/artifact_search.py::_bounded_int`, which raises
`ArtifactSearchInputError(f"{label} out of range")`. Two ergonomics defects
(`BLACK_BOX_REVIEW.md` D3, sub-issue #733 of epic #736) make those caps unusable
by a black-box MCP agent:

1. **The caps are invisible.** The tool schema's `Field(...)` annotations
   (`mcp/tools/catalog/artifacts/registrar.py`) carry no `ge=`/`le=` and the
   descriptions state no bound, so the JSON Schema a client introspects advertises
   only the default. An agent cannot know the maximum without probing.
2. **The rejection is opaque.** The handler's
   `except ArtifactSearchInputError: return _config_error(..., data={"reason":
   "bad_search_input"})` discards the descriptive message — which already names the
   offending field — so the caller gets `configuration_error / bad_search_input`
   with no field and no bound and must binary-search the working value.

This is the same root cause as the closed error-ergonomics epic #449 ("the server
holds what the caller needs and discards it before the wire"). The existing
primitives — schema constraints surfaced by FastMCP, the `config_error(..., detail=)`
seam, and the `BindingErrorMiddleware` conversion table — already cover it; they
were simply not applied here.

A subtlety forces the design. If `ge=`/`le=` are added to the tool-schema parameters,
FastMCP rejects an over-cap argument with a Pydantic `ValidationError` at
**arg-binding time**, before the handler body runs. That raw error is not the
`bad_search_input` envelope acceptance requires, and (unlike the runtime
`_bounded_int` check) the handler's `except ArtifactSearchInputError` never sees it.
The two acceptance criteria — "cap visible in schema" and "over-cap returns a
named `bad_search_input` detail" — therefore cannot both be met by the handler
alone; the binding-time error must be re-enveloped at the same seam the other typed
tools use.

## Decision

We will surface the context caps in the tool schema and re-envelope an over-cap
binding error with a field-naming detail:

1. Add `ge=`/`le=` constraints and range-stating descriptions to the
   `before_lines` / `after_lines` / `max_matches` parameters of the
   `artifacts.search_text` tool signature in `registrar.py` (the schema FastMCP
   exposes) and mirror them on the internal `ArtifactSearchRequest` model.
2. Register a `BindingErrorMiddleware` conversion for `artifacts.search_text` that
   maps a numeric range `ValidationError` under one of those three fields into a
   `CONFIGURATION_ERROR` carrying `data.reason = "bad_search_input"` and a `detail`
   naming the offending field and the bound it violated (reconstructed from the
   error's `loc` and `ctx`).
3. Keep the runtime `_bounded_int` check and its `ArtifactSearchInputError` as
   defense-in-depth for direct (non-MCP) callers; the handler continues to map a
   raised `ArtifactSearchInputError` to the same `bad_search_input` envelope.

The `detail` is built only from the field name (a fixed enum of three literals) and
the integer bound from the validator context — never from caller-supplied free text,
a secret, a host name, or an object-store key (ADR-0123).

## Consequences

- The cap is discoverable: the JSON Schema for `artifacts.search_text` now advertises
  `minimum`/`maximum` per context field, and the generated tool reference
  (`docs/guide/reference/artifacts.md`) states the range, so an agent reads the bound
  instead of probing for it.
- An over-cap request returns a self-correcting envelope: `configuration_error`,
  `data.reason = "bad_search_input"`, and a `detail` naming the field and its bound,
  keeping the established `bad_search_input` reason token stable.
- Two layers now own the same bound (schema/`ArtifactSearchRequest` and the runtime
  `_bounded_int`). This is intentional defense-in-depth, not duplication to remove:
  the schema bound serves MCP clients and is the discoverable contract; the runtime
  bound protects direct library callers of `search_text`. The numbers are asserted
  equal by test so they cannot drift silently.
- `artifacts.search_text` joins the `BindingErrorMiddleware` conversion table, which
  is the established place such typed-binding errors are re-enveloped.

## Alternatives considered

- **Validate only in the handler, leave the schema unconstrained.** Rejected: it
  fails the first acceptance criterion — the cap stays invisible to schema
  introspection, which was the actual blocker for the black-box agent.
- **Constrain the schema only, let FastMCP's raw `ValidationError` propagate.**
  Rejected: the raw error is not the `bad_search_input` envelope, breaks the uniform
  failure contract, and leaks Pydantic-internal error shapes onto the wire.
- **Clamp out-of-range values to the cap instead of rejecting.** Rejected: silently
  returning fewer/more context lines than asked is a silent-success trap; the caller
  cannot tell its request was altered. An explicit named rejection is self-correcting.
- **Drop the runtime `_bounded_int` once the schema enforces the bound.** Rejected:
  `search_text` is a library function with direct (non-MCP) callers and tests; the
  schema constraint does not protect them. Keep both, asserted equal.
