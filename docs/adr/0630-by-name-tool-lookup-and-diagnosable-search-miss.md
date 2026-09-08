# 0630 — By-name tool lookup and a diagnosable `tools.search` miss

## Status

Accepted (2026-09-07)

- **Amends:** [ADR-0472](0472-summary-first-tool-search.md) §2's single-tool fetch contract — the
  exact-name query is no longer the only way to reach one tool's schema. §2's ranking guarantee
  itself is unchanged.
- **Issue:** #2305

## Context

[ADR-0472](0472-summary-first-tool-search.md) §1 made `tools.search` matches summaries by
default, so an agent that has chosen a tool needs a second call to fetch its schema. §2 made that
second call deterministic by ranking a query whose *whole text* equals a tool name ahead of every
other hit.

That guarantee holds and is unreachable by the agent that needs it most. A gateway-mode client is
handed tool names constantly — every `ToolResponse` carries `suggested_next_actions` naming
literal tool names — but the exact-name form is documented only in the `tools.search` docstring,
which a gateway-mode agent has not read when it makes its first call. The gateway-on instructions
say only "pass a short description of what you want to do", naming neither `limit`, `detail`,
`namespace`, nor the exact-name form.

An agent that does not know that form reaches for a conventional operator grammar instead.
Tokenisation is a whitespace split with a two-character floor and scoring is a substring count,
so `select:images.describe,allocations.request` is one token occurring in no tool's searchable
text and returns `{"matches": [], "truncated": false}` — byte-identical to the response for a
capability the server does not have. Every wrong guess costs a round trip carrying a
multi-thousand-token `detail="full"` payload. Two properties are missing and they compound: no
way to turn a name the server itself handed the agent into that tool's schema, and no way to tell
a query that found nothing from a query the server did not understand.

## Decision

### 1. A `names` parameter, not a second gateway tool

`tools.search` gains an optional `names: list[str]`, bounded to 1–10 entries. When present it
selects exactly the RBAC-visible tools with those names, in the caller's given order, and no
ranking runs. Each requested name is stripped and lower-cased before lookup, so `names` is as
case-forgiving as `query` already is; a name repeated after normalisation is returned once, in
its first position.

ADR-0472 rejected a separate `tools.describe(name)` tool, and that reasoning is unchanged: a
parameter on the tool that already owns discovery adds no tool, no new RBAC surface, and no
second response shape. §2's exact-name ranking guarantee is untouched — `names` is a different
mode reached by a different argument. Mode precedence is `names`, then `namespace`, then `query`,
then the unfiltered fallback: most specific first, so a supplied name is never silently
reinterpreted as search text.

### 2. `names` mode always returns full detail, and its own cardinality bound replaces `limit`

A by-name lookup exists to obtain the schema, so `names` mode returns `SearchDetail.FULL` matches
whatever `detail` says. It also ignores `limit`: truncating an explicit enumeration discards the
caller's stated intent rather than trimming a ranked tail, so `truncated` is always `false` here.

Ignoring both payload levers is why the cardinality bound is 10, not the `_SEARCH_LIMIT_MAX` of
50 that bounds `limit`. Measured per-name over the live registry, a full match spans 0.4 KB to
15.9 KB with a median of 1.4 KB, so the ten largest come to 69 KB and fifty would be several
times the 54 KB response ADR-0472 records overflowing a client's context — on the path the new
instructions teach first. Ten is `limit`'s default, so the mode's ceiling is the one a caller
already gets, and the span rather than the median is what the parameter descriptions state.
FastMCP serialises only `Field` text, so each of the three parameters says what the others do
to it.

### 3. Unknown and unauthorised names collapse into one `unknown_names` list

A requested name that is not in the caller's RBAC-visible candidate set is reported in
`data.unknown_names` — sorted, de-duplicated — rather than silently vanishing from `matches`.
That list does not distinguish "no tool carries this name" from "a tool carries it and your
grants do not reveal it".

The reason is not disclosure. An unregistered name is already distinguishable on the next call —
`tools.invoke` answers it with "No tool named X is registered or enabled" — so a per-name split
would confirm little the registry does not already concede.

The reason is that the split buys nothing here. `names` converts a name into a schema; an agent
handed a name it cannot use needs a different tool, not a grant taxonomy, and `namespace` mode
already returns `namespace_required_grants` for the plane that name belongs to. One list keeps
the mode's response to one conditional key with one meaning — "you did not get this one back".

`tools.invoke` is deliberately *not* offered as the recovery for the hidden-tool half. Measured
against a viewer token, `tools.invoke("control.force_crash", {})` returns a `configuration_error`
naming a missing required argument rather than an authorization verdict, so the probe answers the
wrong question — and recommending it would tell an agent to call a destructive tool to find out
whether it may. The docstring names `namespace` mode instead.

### 4. A two-value `reason` on a zero-match query

When `query` mode returns no matches, `data.reason` carries one of:

- `no_usable_tokens` — no token in `query` cleared the two-character floor, so nothing was
  searched.
- `no_token_matched` — the query was tokenised and searched, and not one token occurred in the
  searchable text of any tool visible to the caller.

These are exhaustive and mutually exclusive for an empty query-mode result: a surviving token
occurring in any visible tool's text gives that tool a non-zero score and therefore a hit, so an
empty result means either no token survived or no surviving token hit anything. An empty visible
candidate set falls under `no_token_matched`, which remains literally true. `no_token_matched` is
the signal the unsupported-operator case was missing: the search ran against the caller's own
words and none of them are vocabulary this server indexes. The key is absent whenever `matches`
is non-empty, and absent in `namespace` and `names` modes, which carry their own signals.

### 5. The gateway-on instructions carry the recipe

`_GATEWAY_ON_SURFACE` names `names=[...]`, `limit`, `detail`, `namespace`, and `data.reason` in
the text a gateway-mode agent reads before its first call. The instructions ship in every
session's context, so this is two sentences, not a transcription of the docstring.

## Consequences

- A name the server handed the agent becomes that tool's schema in exactly one call, with no
  ranking to gamble on, and up to ten names cost one call rather than ten.
- The response `data` gains two conditional keys, `unknown_names` and `reason`. Both are
  additive, no existing key changes meaning, and every existing caller — one passing no `names`
  whose `query` or `namespace` finds something — gets a byte-identical response.
- A `names` list that is empty or longer than ten is rejected by schema validation rather than
  answered, arriving through `tools.invoke` as the `configuration_error` envelope whose
  `field_errors` name `names`.
- A viewer who asks by name for a tool their grants hide is told the name is unresolved; the
  recovery, `namespace` mode's grant signal, is one call away and named in the docstring.
- `unknown_names` carries the normalised (stripped, lower-cased) form rather than the caller's
  literal input, so two spellings of one name yield one entry. `_select_named` keys the
  candidate map on `t.name.lower()`, which is unambiguous only while registered names are
  themselves lower-case; a test pins that convention rather than leaving the lookup to shadow
  silently if it ever changes.
- A new `tool_search_names_miss` record carries requested and unresolved counts in `extra`, not
  the names, matching the shape of the two miss records already there. It reaches production
  logs only as far as those two do, which is not far: `JsonFormatter` (`src/kdive/log.py`)
  renders a fixed field schema plus request context and copies no `extra`, so today only the
  message survives. That gap is pre-existing and shared by all three records; this ADR keeps the
  new record consistent with its siblings rather than diverging one of them around it.
- The existing `tool_search_miss` record gains the `reason` and stops firing for a call that
  passed both `namespace` and `query`: `namespace` wins, so no query ran and the record was
  never accurate.
- Teaching `query` an operator grammar stays unbuilt, and `reason` makes its absence legible
  rather than silent.

## Considered & rejected

- **A separate `tools.describe(names)` tool.** judgment: a third gateway tool for a mode the
  discovery tool can carry as one argument, against the direction of epic #1576 and of ADR-0472's
  own rejection of `tools.describe(name)`.
- **Teaching `query` a `select:` / `+`-prefixed operator grammar.** judgment: a parser, a syntax
  error contract, and a second thing for an agent to get wrong, to reach a destination one
  optional list argument already reaches. ADR-0472 §2 already prefers the smaller surface.
- **Shipping §5 alone — the instructions naming the exact-name form, with no `names` parameter.**
  verified: ADR-0472 §2's boost keys on `query.strip().lower()` equalling one tool name
  (`src/kdive/mcp/tools/gateway.py`, the `_rank` query branch), so it fetches one tool per call
  and only when the query is that name and nothing else. It cannot serve several handed names in
  one call, and it silently returns a ranked near-miss rather than reporting the name as
  unresolved. Cheaper, and it leaves both of those.
- **Honouring `detail` in `names` mode.** verified: `detail` defaults to `SearchDetail.SUMMARY`
  in the `tools_search` signature (`src/kdive/mcp/tools/gateway.py`), and a summary match carries
  `name`, `summary`, `annotations`, `maturity` (ADR-0472 §1) — so a by-name call left at the
  default would return the caller its own input plus a one-line summary, never the schema the
  mode exists for.
- **An `unauthorized_names` bucket mirroring ADR-0472 §3.** judgment: a second bucket for a
  distinction `tools.invoke` already draws on the next call, in a mode whose job is to turn a
  name into a schema.
- **Resolving retired names in `names` mode via `RETIRED_TOOL_NAMES`.** judgment: `names` is the
  deterministic mode; silently substituting a different tool for the one the caller named is the
  opposite of that. `query` mode already ranks the replacement for a retired name (ADR-0456 §3),
  and `unknown_names` sends the caller there.
- **An `unmatched_tokens` list beside `reason`.** verified: `reason` is emitted only when
  `matches` is empty, and an empty query-mode result means no surviving token hit anything — so
  the list would equal the surviving tokens of the caller's own `query` and carry no information
  the caller does not hold.
- **Doing nothing.** verified: the transcript excerpts quoted in the issue body of #2305 record
  the first gateway call rejected for using `max_results`, `runs.install`'s schema never
  retrieved in the session, `tools.search(query="select:images.describe,allocations.request")`
  returning `{"data": {"matches": [], "truncated": false}}`, and
  `+runs install built run onto system cmdline` returning `runs.create`.
