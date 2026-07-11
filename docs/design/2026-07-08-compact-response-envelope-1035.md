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
   `error_category` and `retryable`. `detail` is retained **iff it is non-null**.
   It is non-null when the category carries a suppressed constant
   (`not_found`/`authorization_denied`, via `suppressed_detail`,
   `errors.py:66-86`) *or* the caller passed an explicit reason; it is `null`
   otherwise (a worker-plane `from_job` FAILED envelope, or a `failure()` raised
   with a diagnostic category and no `detail` arg) and is correctly omitted (a
   `null` detail carries no information — retaining it would defeat the compaction).
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
         "Default off — the full ADR-0019 envelope. A failure envelope always keeps "
         "error_category and retryable; detail is kept when a reason exists.",
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
registered in `build_app` (`src/kdive/mcp/assembly/app.py`) as the **outermost** response
middleware — specifically *outer* of both `DenialAuditMiddleware` (`app.py:51`,
which synthesizes `authorization_denied` envelopes) and `BindingErrorMiddleware`
(`app.py:52`, currently innermost, which synthesizes `configuration_error`
envelopes) — so it observes the final `ToolResult`, including those synthesized
failure envelopes. This ordering is load-bearing: slotting it *inside*
`DenialAudit` would still catch binding errors but silently skip compacting every
`authorization_denied` envelope (a fail-safe miss, not corruption). Verify the
registration position when wiring.

Behavior:

1. When the flag is off, `return await call_next(context)` unchanged (fast path;
   no per-call cost when the feature is not enabled).
2. When on, obtain `result = await call_next(context)`. If `result` is not a
   `ToolResult` with a `dict` `structured_content`, return it unchanged.
3. **Exact-shape guard.** Only compact a dict whose top-level keys are a subset
   of the `ToolResponse` field names (`set(sc) <= set(ToolResponse.model_fields)`)
   *and* which then validates as a `ToolResponse` (validation requires `object_id`
   and `status`). `ToolResponse` has no `model_config`, so pydantic's default
   `extra="ignore"` would otherwise let a *superset* dict validate and silently
   drop its extra keys — that is corruption, not passthrough. The subset guard is
   **necessary but not sufficient** on its own: the actual safety guarantee is the
   surface-wide ADR-0019 invariant that *every* tool returns a `ToolResponse`, so a
   tool's `structured_content` is always a genuine envelope dump — reshaping it
   compactly is correct, never lossy. The guard exists to keep that true even if a
   future tool bypasses the envelope: a dict carrying an undefined key, or one that
   fails validation, passes through untouched. It does **not** claim to distinguish
   a coincidentally-envelope-shaped non-envelope dict — that case cannot arise while
   the ADR-0019 invariant holds, which a completeness guard (the ADR-0170 output
   schema sweep over the live registry) already anchors.
4. Re-validate the guarded dict into a `ToolResponse` and re-dump it compactly:
   `ToolResponse.model_validate(sc).model_dump(mode="json", exclude_defaults=True)`.
   Return `ToolResult(structured_content=<compact dict>, meta=result.meta)`.
   Constructing a `ToolResult` with only `structured_content` auto-regenerates
   the `content` text block from that dict, so **both** wire fields are compacted
   in one construction (the established `BindingErrorMiddleware` pattern).
5. On `ValidationError` (a subset-shaped dict that still fails field validation)
   or any non-dict structured content, return the original result unchanged —
   fail safe, never corrupt a response.

### Observability

Compaction is a deployment-wide change to the wire shape, and the absent==default
contract is the one way it can break a non-adopting consumer. So when the flag is
on, `build_app` emits a single startup log line (`compact_responses enabled`) at
assembly time — the same place the gateway flag is read — so a downstream break is
attributable to compaction (not a producer bug) and an operator can confirm the
active mode from the logs. No per-call logging (that would defeat the token goal).

`model_dump(exclude_defaults=True)` is the primitive rather than a hand-written
field-stripper: pydantic recurses into `items` applying the same rule, and it
**cannot drift** as the model evolves. It preserves the failure fields that carry
information: `error_category` (non-`None`) and `retryable` (`True`/`False`, both ≠
the `None` default) are non-default on *every* failure and always kept;
`detail` is kept iff non-`None` — present when the category supplies a suppressed
constant (`not_found`/`authorization_denied`) or the caller passed a reason, and
dropped when `None` (a worker-plane `from_job` FAILED envelope, or a `failure()`
raised with a diagnostic category and no `detail` — null by design, see
`responses.py:86-88`, `errors.py:66-86`). Re-validation recomputes `retryable` from
`error_category` via the existing model validator, so it stays consistent.

Compaction is **idempotent**: re-validating a compact dict refills the defaults,
and re-dumping drops them again — so the double pass on gateway meta-tools is
safe. `tools.invoke` returns the inner tool's `ToolResult` directly
(`gateway.py:115`: `app.call_tool(..., run_middleware=True)`), so the inner call
is compacted on its own middleware pass and the outer `tools.invoke` pass compacts
the same object again (idempotent, safe) — meaning the token savings **do** reach
gateway-routed calls, the primary beneficiary. The inner envelope is surfaced as
`tools.invoke`'s own top-level `structured_content`, not nested inside its `data`,
so it is genuinely trimmed. (`tools.search` nests tool *schemas* in `data`, which
are payload, not envelopes, and are correctly left untouched.)

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
- [ ] With the flag **on**, the **`content` TextContent text block** (the second
      wire copy, the larger half of the payload) is *also* compacted for that
      response — parse its JSON and confirm it omits the defaulted fields at the top
      level and within `items[]`, not only `structured_content`.
- [ ] With the flag **on**, a middleware-*synthesized* failure envelope — an
      `authorization_denied` (from `DenialAuditMiddleware`) or a recognized binding
      `ValidationError` (e.g. `allocations.request` shape-XOR, from
      `BindingErrorMiddleware`) — is compacted, proving `CompactResponseMiddleware`
      is ordered outer of both.
- [ ] With the flag **on**, a `failure()` envelope whose `detail` is non-null
      (a `not_found`, whose suppressed constant is always present, or a
      binding-error with an explicit `detail`) still carries `error_category`,
      `retryable`, and that `detail`.
- [ ] With the flag **on**, a worker-plane `from_job` FAILED envelope
      (`detail=None`) still carries `error_category` and `retryable`, and
      correctly **omits** `detail` (it is null by design).
- [ ] With the flag **on**, structured content whose top-level keys are a
      *superset* of the envelope (an `object_id`/`status` dict plus an extra key)
      passes through unchanged — the extra key survives, nothing is dropped.
- [ ] With the flag **on**, non-dict / non-envelope structured content (a
      constructed test double) passes through unchanged (no crash, no mutation).
- [ ] With the flag **on**, a list response routed through `tools.invoke`
      (e.g. `tools.invoke(name="images.list")`) is compacted at both the outer
      envelope and each inner `items[]` row (idempotent double pass).
- [ ] With the flag **on**, `build_app` emits exactly one `compact_responses
      enabled` startup log line; with the flag **off**, none.
- [ ] Compacting an already-compact response yields the same dict (idempotent).
- [ ] `docs/guide/response-envelope.md` documents the opt-in flag, its contract,
      **and the absent==default consumer rule** (under compaction an omitted field
      is semantically identical to its documented default — empty list/dict or
      null — so a consumer must not read key-absence as a distinct signal);
      `just resources-docs` refreshes the packaged snapshot and
      `just resources-docs-check` passes.
- [ ] `just ci` is green.

## Failure modes and edge cases

- **Superset / non-envelope structured content.** A dict with keys the envelope
  does not define fails the exact-shape subset guard → pass through unchanged (its
  extra keys survive). A subset-shaped dict that still fails field validation
  raises `ValidationError` → pass through unchanged. Both paths are safe.
- **`data` contents vs an empty top-level `data`.** A *non-empty* `data` dict is
  preserved verbatim — `exclude_defaults` does not recurse into a plain `dict`, so
  a `next_cursor=None` or a nested `{}` *inside* `data` survives (it is payload).
  But an *empty* top-level `data={}` equals the field's default and **is** omitted,
  consistent with the absent==default rule (a plain `success()` with no payload
  compacts to just `object_id` + `status`). A `collection()` always has a non-empty
  `data` (it carries `count`), so its `data` is always kept.
- **Absent==default consumer contract.** Compaction erases the distinction
  between an explicitly-empty field and an absent one (e.g. a CANCELED job's
  `suggested_next_actions=[]` disappears). This is intentional and load-bearing:
  a consumer must read an omitted field as its documented default, never as a
  distinct "unknown" signal. This applies to **first-party** clients too (the
  CLI, `scripts/live-debug.py`), not only external consumers — the existing
  `response.get("items", [])` idiom those scripts already use is compaction-safe,
  and a *populated* collection's `items` is never dropped (only an empty one is),
  so index access on a known-populated collection is unaffected. Documented in
  `docs/guide/response-envelope.md`.
- **`retryable=False` on a permanent failure.** Kept — `False ≠ None` default.
- **Gateway on (`tools.invoke`).** Inner and outer results both compacted;
  idempotent, so no corruption.
- **Meta preservation.** The `wrap_result` meta path is not used by the
  envelope schema (no `x-fastmcp-wrap-result`), but the middleware forwards
  `result.meta` defensively so any future meta survives.

## Rollback

Pure additive opt-in. Rollback is `KDIVE_COMPACT_RESPONSES=off` (the default) or
reverting the branch; no migration, no persisted state, no schema change.
