# `tools.search` — a `parameters` detail tier

- **Issue:** #2341
- **Decision record:** [ADR-0632](../../adr/0632-parameters-detail-tier-for-tools-search.md)
- **Amends:** [ADR-0472](../../adr/0472-summary-first-tool-search.md) §1; cross-references
  [ADR-0630](../../adr/0630-by-name-tool-lookup-and-diagnosable-search-miss.md) §2

## Problem

`tools.search`'s `detail` lever has two positions. `summary` answers "which tool", `full` answers
"how do I call it" by returning the complete description and the whole projected `input_schema`.
Nothing answers "what arguments does this take" on its own. Measured over the 124 registered
tools, the three facts an agent needs to build a call — parameter name, type, required flag —
are 14.4% of the projected schema in aggregate and 28.9% of it at the median; `runs.install` is
257 B of argument list inside 1,993 B of schema. An agent that has already chosen its tool
over-fetches by roughly seven times, which is the cost `tools.search` exists to control.

## Scope

Add `SearchDetail.PARAMETERS = "parameters"`, ordered between `SUMMARY` and `FULL`. A match at
that tier carries the four summary keys plus `parameters`: the projected schema's properties in
declaration order, each entry `{name, type, required}`.

`type` is a rendered display string — a plain type verbatim, a union joined with `|` in member
order and de-duplicated, an array as `array[<element>]`, a `$ref` as the definition's name, and
`unknown` for a shape none of those match. `required` is membership of the schema's top-level
`required` list. The tier uses the existing `_project_or_passthrough` projection, unchanged.

Per ADR-0632 §2 the `full` tier carries `parameters` as well, so the three tiers stay monotone.
The `tools.search` wrapper docstring and the `detail` `Field` description — the agent-facing
contract per AGENTS.md — state what each of the three tiers returns and how the tier interacts
with `names` and `limit`. Generated artifacts that derive from the live tool schemas are
regenerated with the repository's own recipes.

Out of scope, per the frozen charter: `names` mode's forced-`full` override (ADR-0630 §2), a
separate `tools.describe` tool, value constraints such as inline `enum` in the entries, and any
projection change beyond reusing `_project_or_passthrough`.

## Success

1. `detail="parameters"` returns exactly the keys `name`, `summary`, `annotations`, `maturity`,
   `parameters`.
2. Entries carry the projected schema's property names in declaration order, with `required`
   true exactly for the schema's required properties.
3. Each documented rendering shape produces its stated string: plain, union, array, `$ref`,
   and the `unknown` fallback.
4. `detail="full"` carries `parameters` in addition to its existing keys, so each tier's key set
   is a superset of the tier below it.
5. The digest is built from the schema `_project_or_passthrough` returns, so a projection that
   drops a top-level property drops it from `parameters` too.
6. `names` mode still returns `full` matches whatever `detail` says.
7. The wrapper docstring and `detail` `Field` text describe all three tiers — including the limit
   in ADR-0632 §3, that the tier reports the argument list and not value constraints, and that a
   tool whose only parameter is a payload model needs `full` — and the committed generated
   artifacts match a fresh generation.

Deliberately **not** a success criterion: "the tier is cheaper than `full`". The three key sets
are strictly nested, so that ordering holds by construction for every tool and no implementation
could violate it. Success 4 already fixes the containment that makes it true.

## Edge cases

- **A tool with no parameters** yields an empty `parameters` list, not a missing key.
- **A tool whose only parameter is a payload model** — 17 of 124, every `.list` tool among them —
  yields one entry naming that model, which is not enough to build a call. ADR-0632 §3 accepts
  this and requires the `detail` contract text to name it, so the tier's worst case is documented
  rather than discovered.
- **A degraded projection** — `project_listed_tool` raising, or `registered_kinds()` failing —
  falls back to the unprojected schema on the existing paths, exactly as `full` already does. The
  tier inherits that behaviour and adds no new failure mode.

## Validation

- **`SearchDetail` gains a third member reachable through `detail`.** Mode: focused-test —
  `tests/mcp/tools/test_gateway_search.py::test_parameters_tier_returns_the_argument_list`; red
  before the enum member exists (schema validation rejects the value); green with
  `uv run python -m pytest tests/mcp/tools/test_gateway_search.py -k parameters_tier -q`.
- **The `parameters` match key set (Success 1).** Mode: focused-test —
  `test_parameters_tier_returns_the_argument_list` asserts the exact key set.
- **Entry order, names, and required flags (Success 2).** Mode: focused-test —
  `test_parameters_entries_follow_declaration_order_and_required`.
- **Type rendering per shape (Success 3).** Mode: focused-test — parametrized
  `test_type_rendering_covers_each_schema_shape` over the five documented shapes.
- **Tier monotonicity (Success 4).** Mode: focused-test —
  `test_full_tier_still_carries_parameters` plus the updated
  `test_detail_full_adds_schema_and_complete_description` key-set assertion.
- **The digest follows the projection (Success 5).** Mode: focused-test —
  `tests/mcp/test_gateway_projection.py::test_parameters_tier_reads_the_projected_schema`. The
  live projection narrows only nested `$defs`, so a test comparing real projected against real
  unprojected output cannot fail. The test instead stubs `project_listed_tool` with one that
  drops a top-level property and asserts that property is absent from `parameters` — which fails
  against an implementation that digests the unprojected schema.
- **`names` mode override (Success 6).** Mode: focused-test — existing
  `test_names_mode_ignores_summary_detail`, extended to pass `detail="parameters"`.
- **Agent-facing text and generated artifacts (Success 7).** Mode: task-test-not-applicable —
  the docstring and `Field` prose have no executable consumer that could fail meaningfully on
  wording, and inventing a prose-snapshot assertion is explicitly disallowed. The generated
  half is covered by the repository's own gates, `just docs-check`, `just cli-verbs-check`, and
  `just resources-docs-check`, which run in `just ci`.
