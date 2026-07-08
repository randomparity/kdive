# ADR 0314 — Opt-in compact response envelope

- **Status:** Accepted
- **Date:** 2026-07-08
- **Deciders:** kdive maintainers
- **Issue:** #1035 (`BLACK_BOX_REVIEW.md` pain point P4, Epic #998)
- **Spec:** [compact-response-envelope-1035](../specs/2026-07-08-compact-response-envelope-1035.md)
- Extends ADR-0019 (uniform response envelope), ADR-0087 (config registry),
  ADR-0170 (advertised envelope output schema). Supersedes nothing.

## Context

The uniform `ToolResponse` envelope (ADR-0019) carries nine fields so an agent
learns one shape across every plane. Six default null/empty on a success row
(`suggested_next_actions`, `refs`, `error_category`, `retryable`, `detail`,
`items`, and `data` when empty). FastMCP serializes the returned model with a
hard-coded full dump (`pydantic_core.to_jsonable_python`,
`fastmcp/tools/base.py:324`), so nulls are kept, and the identical JSON is
emitted twice — as `structured_content` and as the `content` text block.

Because `items` is `list[ToolResponse]` (`responses.py:90`), each row of a
collection response is a *full* envelope repeating those six fields. On
token-heavy list tools (`images.list`, `audit.query`, `runs.list`) the fixed
per-row overhead scales with page size and is pure cost for an agent paying per
token (`BLACK_BOX_REVIEW.md` P4). The reporter asked for an **opt-in** compact
mode rather than unconditional null-stripping, to stay faithful to the
learn-one-envelope intent.

Two facts constrain the mechanism. First, FastMCP 3.4.2 exposes no global tool
serializer, and the per-tool `serializer=` only shapes the `content` text block,
not `structured_content` — so trimming must intercept the built `ToolResult`, not
the serializer. Second, there are 28 `collection()` call sites across 27 files,
so any per-tool approach carries a large, repetitive surface.

## Decision

Add an **opt-in server config flag** and compact in one cross-cutting middleware.

1. **Switch — a config setting, default off.** `COMPACT_RESPONSES`
   (`KDIVE_COMPACT_RESPONSES`, default `off`, group `mcp`, server process) in
   `config/core_settings.py`, modelled on `MCP_TOOL_GATEWAY`. A single-source
   reader `compact_responses_enabled()` (new `mcp/verbosity.py`, mirroring
   `gateway_enabled()`) is the only interpreter of the value.

2. **Transform — an `on_call_tool` middleware.** `CompactResponseMiddleware`,
   registered outermost in `build_app`, so it sees the final `ToolResult`
   including binding-error envelopes. When the flag is off it passes through
   untouched (no per-call cost). When on it guards on an exact envelope shape —
   only a dict whose top-level keys are a subset of the `ToolResponse` fields is
   compacted, because the model's default `extra="ignore"` would otherwise let a
   superset dict validate and silently drop its extra keys — then re-validates the
   `structured_content` into a `ToolResponse` and re-dumps with
   `model_dump(mode="json", exclude_defaults=True)`, returning a fresh
   `ToolResult(structured_content=<compact>, meta=result.meta)`. Constructing a
   `ToolResult` from only `structured_content` regenerates the `content` text
   from the compact dict, so both wire fields shrink in one step (the existing
   `BindingErrorMiddleware` pattern). Anything else — a superset/non-envelope
   dict, a `ValidationError`, or non-dict content — passes through unchanged.

`exclude_defaults=True` is chosen over a hand-written field-stripper: it recurses
into `items`, cannot drift as the model evolves, and keeps the failure fields that
carry information — `error_category` (non-`None`) and `retryable` (`True`/`False`,
both ≠ the `None` default) are non-default on every failure and always kept, while
`detail` is kept when non-`None` (a direct `failure()`) and correctly dropped when
`None` (a worker-plane `from_job` FAILED envelope, null by design). Re-validation
re-derives `retryable` from `error_category` via the model validator, so the
ADR-0019 invariant is preserved. Compaction is idempotent (re-validating a compact
dict refills defaults, re-dumping drops them again), so the gateway meta-tool
double pass is safe. A consumer must read an omitted field as its documented
default — key-absence is not a distinct signal.

## Consequences

- Deployments serving token-conscious agents set one env var; every response
  drops null/empty boilerplate at every `items` depth (measured ~64% smaller on a
  small `images.list`). No tool signature, no schema, no migration changes.
- Default output is byte-identical to today — existing tests and clients are
  unaffected; the advertised `ENVELOPE_OUTPUT_SCHEMA` already types the six as
  optional/nullable, so compact output stays schema-valid.
- The flag is deployment-wide, not per-call: an operator opts a deployment in;
  an individual agent cannot toggle it per request. Accepted for a `priority:low`
  token optimization; per-call control can layer on later via the gateway
  (`tools.invoke`) without reworking this.
- One re-validate + re-dump per call when enabled — CPU for token savings, an
  acceptable trade for an explicitly opted-in mode.

## Considered & rejected

- **Unconditional null-omission (global serializer / always-on).** Simplest, but
  changes the default wire shape for every deployment, churning tests and any
  client that reads a key expecting presence, and the reporter explicitly
  preferred opt-in. Rejected as the default; available as the flag's `on` state.
- **Per-call `verbosity="compact"` parameter on list tools.** Most granular and
  discoverable, but adds a param to 20+ tool wrappers (the FastMCP serializer
  can't help, so each would thread it) — the largest surface for a low-priority
  optimization. Rejected on blast radius.
- **Per-session, agent-set verbosity.** More agent-native, but needs session-state
  plumbing and a setter surface for a nice-to-have. Rejected as over-built for
  the scope; revisit if per-agent control is needed.
- **Register a FastMCP tool `serializer=`.** Cannot trim the token-heavy
  `structured_content` — the serializer only shapes the `content` text block
  (`fastmcp/tools/base.py:306,324`). Rejected as ineffective.
- **Hand-written recursive field-stripper keyed on the six defaults.** Works but
  duplicates the model's default knowledge and drifts when a field is
  added/renamed. Rejected in favor of `exclude_defaults=True`.
