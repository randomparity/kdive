# Spec — Opt-in compact response envelope (#1035)

- **Status:** Draft
- **Date:** 2026-07-08
- **Issue:** #1035 (`BLACK_BOX_REVIEW.md` pain point P4, cf. epic #998)
- **ADR:** [0314-compact-response-envelope](../adr/0314-compact-response-envelope.md)
- **Branch:** `feat/compact-response-envelope-1035` (base `main`)

## Problem

Every `ToolResponse` (`src/kdive/mcp/responses.py`) carries nine fields, six of
which default null/empty on a success row: `suggested_next_actions=[]`,
`refs={}`, `error_category=None`, `retryable=None`, `detail=None`, `items=[]`
(and `data={}` when there is no payload). FastMCP serializes the model with a
hard-coded full dump — `pydantic_core.to_jsonable_python(raw_value)` at
`fastmcp/tools/base.py:324` — so nulls are kept, and the *same* JSON is emitted
**twice**: once as `structured_content` and once as the `content` TextContent
text block.

`items` is typed `list[ToolResponse]` (`responses.py:90`), so each row of a
collection response (`images.list`, `audit.query`, `runs.list`, …) is a **full**
envelope repeating all six defaulted fields. A 2-item `images.list` measured at
503 bytes per wire field; the fixed overhead scales linearly with page size. For
an agent paying per token across a long session this is pure waste.

The current shape is deliberate (ADR-0019: "an agent learns one envelope"), and
the reporter asked for **opt-in** compaction rather than unconditional
null-stripping, to stay faithful to that intent.

## Goals

1. Provide an **opt-in** way to omit the null/empty *defaulted* envelope fields
   from every tool response, including recursively within `items`.
2. Preserve the response contract exactly when compaction is off (the default):
   byte-identical output, no schema change, no test churn.
3. Preserve the `error_category`-iff-failure invariant and the derived
   `retryable` in compact mode — a failure envelope must still carry
   `error_category`, `retryable`, and `detail`.
4. Zero blast radius across the 28 `collection()` / N list-tool call sites — the
   mechanism must be cross-cutting, not per-tool.

## Non-goals

- No per-call `verbosity` parameter on tool signatures (rejected — see ADR-0314).
- No per-session, agent-set verbosity state (rejected — see ADR-0314).
- No change to the *contents* of `data` (payload is not defaulted state; a tool
  that puts `next_cursor=None` inside `data` keeps it — that is payload, not
  envelope boilerplate).
- No change to which fields *can* appear; compaction only omits fields already
  at their default. The advertised `ENVELOPE_OUTPUT_SCHEMA` already declares all
  six as optional/nullable, so compact output stays schema-valid.

## Decision (summary; full rationale in ADR-0314)

Opt in with a **server config flag**, `KDIVE_COMPACT_RESPONSES` (default `off`),
and apply the transform in a cross-cutting `on_call_tool` middleware.

### The switch

A new core setting `COMPACT_RESPONSES` in `src/kdive/config/core_settings.py`,
modelled exactly on `MCP_TOOL_GATEWAY`:

```
COMPACT_RESPONSES = Setting(
    name="KDIVE_COMPACT_RESPONSES",
    parse=_str,
    default="off",
    group="mcp",
    processes=_SERVER,
    help="When on/1/true, the server omits null/empty defaulted fields from every "
         "tool response envelope (recursively within items) to cut per-call tokens. "
         "Default off — the full ADR-0019 envelope. Failure fields (error_category, "
         "retryable, detail) are always present on a failure.",
)
```

added to the `SETTINGS` list. A single-source reader
`compact_responses_enabled() -> bool` returns
`(config.get(COMPACT_RESPONSES) or "").strip().lower() in {"on", "1", "true"}`,
mirroring `gateway_enabled()` (`src/kdive/mcp/exposure.py`). It lives in a small
dedicated module (`src/kdive/mcp/verbosity.py`) since it concerns response
shaping, not tool exposure.

### The transform

A new `CompactResponseMiddleware(Middleware)` with an `on_call_tool` hook,
registered in `build_app` (`src/kdive/mcp/app.py`) as the **outermost** response
middleware so it observes the final `ToolResult` — including the failure
envelopes `BindingErrorMiddleware` synthesizes.

Behavior:

1. When the flag is off, `return await call_next(context)` unchanged (fast path;
   no per-call cost when the feature is not enabled).
2. When on, obtain `result = await call_next(context)`. If `result` is not a
   `ToolResult` with a `dict` `structured_content`, return it unchanged.
3. Re-validate that dict into a `ToolResponse` and re-dump it compactly:
   `ToolResponse.model_validate(sc).model_dump(mode="json", exclude_defaults=True)`.
   Return `ToolResult(structured_content=<compact dict>, meta=result.meta)`.
   Constructing a `ToolResult` with only `structured_content` auto-regenerates
   the `content` text block from that dict, so **both** wire fields are compacted
   in one construction (the established `BindingErrorMiddleware` pattern).
4. On `ValidationError` (a result whose `structured_content` is not a valid
   envelope) or any non-dict structured content, return the original result
   unchanged — fail safe, never corrupt a response.

`model_dump(exclude_defaults=True)` is the primitive rather than a hand-written
field-stripper: pydantic recurses into `items` applying the same rule, and it
**cannot drift** as the model evolves. It provably preserves failure fields —
`error_category` (non-`None`), `retryable` (`True`/`False`, both ≠ the `None`
default), and `detail` (non-`None`) are all non-default on a failure and kept.
Re-validation recomputes `retryable` from `error_category` via the existing
model validator, so it stays consistent.

Compaction is **idempotent**: re-validating a compact dict refills the defaults,
and re-dumping drops them again — so the double pass on gateway meta-tools
(`tools.invoke`/`tools.search`, which re-enter the middleware chain) is safe.

## Acceptance criteria

- [ ] `KDIVE_COMPACT_RESPONSES` is a registered `COMPACT_RESPONSES` setting
      (default `off`); `just config-docs` regenerates
      `docs/guide/reference/config.md` and `just config-docs-check` passes.
- [ ] With the flag **off**, a representative tool response (`images.list` with
      ≥1 row) is byte-identical to `main`'s output — verified structured_content
      and content-block equality.
- [ ] With the flag **on**, that same response omits `error_category`,
      `retryable`, `detail`, and any empty `suggested_next_actions`/`refs`/`items`,
      at the top level and within each `items[]` entry; `object_id`, `status`,
      and non-empty `data` remain.
- [ ] With the flag **on**, a failure envelope (e.g. a `not_found` /
      `configuration_error`) still carries `error_category`, `retryable`, and
      `detail`.
- [ ] With the flag **on**, a non-envelope structured content (constructed test
      double) passes through unchanged (no crash, no mutation).
- [ ] Compacting an already-compact response yields the same dict (idempotent).
- [ ] `docs/guide/response-envelope.md` documents the opt-in flag and its
      contract; `just resources-docs` refreshes the packaged snapshot and
      `just resources-docs-check` passes.
- [ ] `just ci` is green.

## Failure modes and edge cases

- **Not a `ToolResponse`.** Some result path returns non-envelope structured
  content → `model_validate` raises → pass through unchanged.
- **`data` holding an explicit `None`/`{}` value.** Left untouched: `data`
  contents are payload, not envelope defaults; `exclude_defaults` does not
  recurse into a plain `dict` value.
- **`retryable=False` on a permanent failure.** Kept — `False ≠ None` default.
- **Gateway on (`tools.invoke`).** Inner and outer results both compacted;
  idempotent, so no corruption.
- **Meta preservation.** The `wrap_result` meta path is not used by the
  envelope schema (no `x-fastmcp-wrap-result`), but the middleware forwards
  `result.meta` defensively so any future meta survives.

## Rollback

Pure additive opt-in. Rollback is `KDIVE_COMPACT_RESPONSES=off` (the default) or
reverting the branch; no migration, no persisted state, no schema change.
