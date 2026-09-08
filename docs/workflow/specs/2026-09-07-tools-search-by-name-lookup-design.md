# `tools.search`: by-name lookup, a diagnosable miss, and instructions that carry the recipe

- **Issue:** #2305
- **ADR:** [ADR-0630](../../adr/0630-by-name-tool-lookup-and-diagnosable-search-miss.md)
  (amends [ADR-0472](../../adr/0472-summary-first-tool-search.md) §2)

## Problem

A gateway-mode agent is handed tool names constantly — every `ToolResponse` carries
`suggested_next_actions` naming literal tool names — and has no deterministic way to turn one
into that tool's schema. Three gaps compound:

1. **No by-name argument.** `tools_search` takes `query`, `namespace`, `limit`, `detail`. The
   ADR-0472 §2 exact-name boost fires only when the whole query string equals a tool name, a form
   documented solely in the `tools.search` docstring — which a gateway-mode agent has not read
   when it makes its first call.
2. **Unsupported query forms degrade silently.** `_rank` splits on whitespace with a
   two-character floor and `_score` counts plain substring hits, so
   `select:images.describe,allocations.request` is one token matching nothing and returns
   `{"matches": [], "truncated": false}` — byte-identical to "no such tool". A server-side
   `tool_search_miss` log is the only record.
3. **The instructions omit the recipe.** `_GATEWAY_ON_SURFACE` says only "pass a short
   description of what you want to do": no `limit`, `detail`, `namespace`, or exact-name form.

Cost recorded in the #2305 issue body: one or two extra round trips per first-time tool call,
each returning a multi-thousand-token `detail="full"` payload; `runs.install`'s schema was never
retrieved at all.

## Scope

**In scope** — three additive changes, one PR:

- **A `names` parameter on `tools.search`** (`src/kdive/mcp/tools/gateway.py`). Optional
  `list[str]`, `min_length=1`, `max_length=10`. Each entry is stripped and lower-cased before
  lookup; a name repeated after normalisation is returned once, in its first position. Selects
  exactly the RBAC-visible tools carrying those names, in the caller's order, with no ranking.
  Always returns `SearchDetail.FULL` matches regardless of `detail`, and ignores `limit`, so
  `truncated` is always `false` (ADR-0630 §2). Requested names absent from the RBAC-visible
  candidate set appear in `data.unknown_names`, normalised, sorted and de-duplicated; the key is
  absent when every requested name resolved, and a names-mode miss is logged as
  `tool_search_names_miss` with requested and unresolved counts. Mode precedence is `names` →
  `namespace` → `query` → unfiltered fallback. The `limit` and `detail` `Field` descriptions are
  amended to say `names` mode overrides them, since FastMCP serialises only that text.
- **Two regenerated artifacts**, both CI-gated against the live schemas and both reddened by the
  text above: `docs/guide/reference/tools.md` (`just docs` / `docs-check`) and
  `src/kdive/cli/commands/_generated_verbs.py` (`just cli-verbs` / `cli-verbs-check`), which
  embeds `tools.search`'s first docstring line and each `Field` description and gains a
  `kdivectl tools search --names` flag. No implementation of criterion 1 lands green without it.
- **A `reason` key on a zero-match query** (`src/kdive/mcp/tools/gateway.py`). `_rank` returns the
  ordered candidates plus a miss reason. In `query` mode with no matches, `data.reason` is
  `no_usable_tokens` (no token cleared the two-character floor) or `no_token_matched` (tokens ran,
  none occurred in any visible tool's searchable text). The key is absent when `matches` is
  non-empty and in `names` / `namespace` / fallback modes.
- **Instructions that name the parameters** (`src/kdive/mcp/schema/tool_index.py`).
  `_GATEWAY_ON_SURFACE` gains two sentences naming `names=[...]`, `limit`, `detail`, `namespace`,
  and `data.reason`. The `tools.search` wrapper docstring and every `Field(description=...)` stay
  in sync with the behaviour, since FastMCP serialises only those into the agent-visible schema
  (AGENTS.md, "The wrapper docstring is the agent-facing contract").

**Out of scope**, operator-approved on 2026-09-07 (see the `WORK:SCOPE` annotation on #2305):

- A middle `detail` tier (parameter names/types/required flags only) — follow-up issue.
- `session.whoami` returning `refs` to the agent-index doc resource — follow-up issue.
- Teaching `query` a `select:` / `+`-prefixed operator grammar — not planned; ADR-0630 records
  the rejection.
- Updating `docs/guide/agent-index.md`, whose "get a schema" recipe teaches `detail="full"` with a
  small `limit` and does not mention `names`. Still correct, only incomplete; outside the frozen
  surface — follow-up candidate raised at design review.

**Deferrals carried into implementation:** none.

## Success

1. `tools.search(names=["runs.install", "runs.boot"])` returns both tools with `description` and
   `input_schema`, in the caller's order, without ranking. (#2305 "Expected" bullet 1.)
2. A name the caller's grants do not reveal, and a name no tool carries, both appear in
   `data.unknown_names` and neither appears in `matches`. (#2305 "Proposed approach" item 1;
   ADR-0630 §3.)
3. `names` mode never returns a tool `tool_visible` excludes for the calling context.
4. `names` handles its edges without surprising the caller: `[" Runs.Boot "]` resolves, a name
   repeated after normalisation is returned once in its first position, an empty list and an
   11-entry list are rejected by schema validation, and `limit` and `detail` do not change the
   result. (ADR-0630 §§1–2.)
5. `data.reason` is `no_usable_tokens` for a query of only sub-two-character tokens, and
   `no_token_matched` for a query whose tokens occur in no visible tool — including the
   `select:…` operator form from the issue. The key is absent when `matches` is non-empty.
   (#2305 "Expected" bullet 2.)
6. `build_instructions(gateway_enabled=True)` names `names=`, `limit`, `detail`, and `namespace`.
   (#2305 "Expected" bullet 3.)
7. ADR-0472 §2 does not regress: a query that is exactly a tool name still ranks that tool first,
   and `detail`/`limit`/`namespace`/`query` behaviour is unchanged for every call that passes no
   `names`.
8. The committed `docs/guide/reference/tools.md` and `src/kdive/cli/commands/_generated_verbs.py`
   each match a fresh generation (`just docs-check`, `just cli-verbs-check`).

## Validation

| Contract | Mode | Evidence |
|---|---|---|
| `names` returns the named tools at full detail, in caller order | `focused-test` | `tests/mcp/tools/test_gateway_search.py::test_names_returns_exactly_those_tools_at_full_detail` |
| `names` ignores `detail="summary"` | `focused-test` | `…::test_names_mode_ignores_summary_detail` |
| Unknown and RBAC-hidden names land in `unknown_names` | `focused-test` | `…::test_names_reports_unknown_and_hidden_names`, `…::test_names_mode_is_rbac_filtered` |
| `names` outranks `namespace` and `query` when both are supplied | `focused-test` | `…::test_names_takes_precedence_over_namespace_and_query` |
| `names` sets `truncated` false, ignores `limit`, and carries no `reason` | `focused-test` | `…::test_names_mode_ignores_limit_and_carries_no_reason` |
| `names` normalises case/whitespace and collapses duplicates in first position | `focused-test` | `…::test_names_normalises_and_deduplicates` |
| `names` cardinality rejects `[]` and 11 entries, accepts 10 | `focused-test` | `…::test_names_cardinality_rejects_out_of_bounds`, `…::test_names_cardinality_accepts_the_ceiling` |
| A names-mode miss is logged with counts, not names | `focused-test` | `…::test_names_miss_is_logged_with_counts_only` |
| `reason` is `no_usable_tokens` when every token is filtered | `focused-test` | `…::test_short_token_query_reports_no_usable_tokens` |
| `reason` is `no_token_matched` for the issue's `select:` form | `focused-test` | `…::test_unsupported_operator_query_reports_no_token_matched` |
| `reason` absent on a hit | `focused-test` | `…::test_successful_query_carries_no_reason` |
| ADR-0472 §2 exact-name ranking unchanged | `focused-test` | existing `…::test_exact_tool_name_query_ranks_that_tool_first` and `…::test_exact_name_first_does_not_drop_the_other_hits`, unmodified |
| Gateway-on instructions name the four parameters | `focused-test` | `tests/mcp/test_tool_index.py::test_gateway_on_instructions_name_the_search_parameters` |
| `tool_search_miss` fires only for a query-mode miss and carries the reason | `focused-test` | `…::test_query_miss_log_carries_the_reason_and_skips_namespace_calls` |
| Generated tool reference and CLI verbs match the registry | `focused-test` | `just docs-check` after `just docs`; `just cli-verbs-check` after `just cli-verbs` (both CI-gated) |
| ADR-0630 status/citation coupling | `focused-test` | `just adr-status-check` |
