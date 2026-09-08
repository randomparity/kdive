# 0632 — A `parameters` detail tier for `tools.search`

## Status

Accepted (2026-09-08)

- **Amends:** [ADR-0472](0472-summary-first-tool-search.md) §1's two-value `detail` contract.
  §1's summary/full key definitions and its strict-subset rule are kept, not replaced.
- **Cross-references:** [ADR-0630](0630-by-name-tool-lookup-and-diagnosable-search-miss.md) §2,
  whose forced-full override for `names` mode is unchanged here.
- **Issue:** #2341

## Context

[ADR-0472](0472-summary-first-tool-search.md) §1 gave `tools.search` two detail tiers. A
`summary` match carries `name`, `summary`, `annotations`, and `maturity` — enough to *choose* a
tool. A `full` match adds the complete `description` and the projected `input_schema` — enough to
*call* one.

The two bracket the common case rather than covering it. An agent that has already chosen its
tool, and needs only the argument list, has no tier that answers "what arguments does this take".
It must take the whole schema to learn three things per parameter: the name, the type, and
whether the parameter is required.

Measured over the 124 tools returned by `registered_tools(app)` — the runtime path
`describe_tool` reads, not the pre-inlined view `scripts/generate/gen_tool_reference.py`
builds — a projected schema runs 67 B to 14,185 B with a median of 499 B, while those three
facts for the same tool come to 2 B to 737 B with a median of 119 B: 28.9% of the schema at the
median and 14.4% of it in aggregate. `runs.install` is 1,993 B of schema for a 257 B argument
list. The figures come from building the app as `tests/mcp/tools/test_gateway_search.py` does,
walking `registered_tools(app)`, and serialising each tool's `parameters` against the digest
this ADR specifies.

Over-fetching on the path an agent takes after it has already decided is the cost `tools.search`
exists to control, and it is the cost ADR-0472 removed from the *choosing* half of the flow while
leaving it on the *calling* half.

## Decision

### 1. A third tier, ordered between the two that exist

`SearchDetail` gains `PARAMETERS = "parameters"`, declared between `SUMMARY` and `FULL` so the
enum reads in increasing order of cost. A `parameters` match carries the four summary keys plus
one new key, `parameters`: the ordered argument list of the tool's projected input schema, one
entry per parameter, each carrying `name`, `type`, and `required`.

Entries keep the schema's own property order, which is the tool's declaration order, so the list
reads as the signature rather than as an alphabetised set. `type` is a rendered display string,
not a schema fragment: a plain JSON Schema type verbatim (`string`), a union joined with `|` and
de-duplicated in member order (`string|null`), an array with its element type
(`array[string]`), and a `$ref` rendered as the referenced definition's name (`SearchDetail`).
`required` is membership of the schema's top-level `required` list.

The tier reads the same projected schema `full` already reads, through the existing
`_project_or_passthrough`. That is consistency rather than a safeguard, and the distinction is
worth stating because it is easy to over-claim: `project_tool_schema` narrows only nested `$defs`
entries, never the top-level `properties` or `required` lists, so the digest is byte-identical
before and after projection for all 124 tools today. Applying it costs one call that `full`
already makes and keeps the two tiers reading one schema if the projection ever narrows the
property set. It is not a live guarantee that a removed parameter stays hidden, and the two
existing degraded paths — a projection failure and a `registered_kinds()` failure, both of which
fall back to the unprojected schema — mean no such absolute could be made here anyway.

### 2. `parameters` rides the `full` tier too

ADR-0472 §1 made the summary keys a strict subset of the full keys, so a client parses one shape
with optional keys instead of branching on a mode discriminator. Adding a middle tier that
introduced a key `full` does not carry would break that: stepping up to more detail would *lose*
a key. So `full` carries `parameters` as well, and the three tiers stay monotone — each tier's
key set is a superset of the tier below it.

That is the same trade ADR-0472 §1 made when it put `summary` in both modes, and it is redundant
in the same way: `parameters` is derivable from `input_schema` by any caller that holds it. The
measured cost, taken on the assembled full-match shape rather than on `input_schema` alone, is
+8.6% in aggregate and +10.6% at the median — 224,509 B to 243,841 B across the catalogue. That
is the same order as the ~10% ADR-0472 §1 accepted for putting `summary` in both modes.

### 3. The tier reports the argument list, not the argument constraints

`parameters` carries the three facts #2341 asks for and stops there. A parameter's value
constraints — an inline `enum`, a `pattern`, a numeric bound — are not in the entry, and a
parameter whose type renders as a definition name is telling the caller that the definition is in
`full`. The tier's contract text says this so an agent knows when the middle tier is not enough,
rather than discovering it through a validation failure.

How often that happens is worth stating rather than leaving as a qualitative caveat, because it
decides whether the tier helps. Call an entry *constructible* when its rendered type is built
only from JSON primitives and arrays of them, and *opaque* when it names a definition or is a
bare `object`. Across the 124 registered tools and their 272 top-level properties:

- 81 tools have at least one parameter and every entry constructible. The tier is the whole
  answer for them.
- 9 tools take no arguments at all, for which the empty list is likewise the whole answer.
- 34 tools carry at least one opaque entry, and **17 of those have no constructible entry at
  all** — the `.list` family plus `accounting.report`, `accounting.usage`, `artifacts.get`,
  `audit.query`, `ops.jobs_list`, `ops.tool_trail`, `reports.generate`, and
  `resources.availability`.

Those 17 collapse to a single entry naming a payload model, so the tier costs a round trip and
returns nothing the caller can build a call from. That is the tier's worst case, it is under a
seventh of the catalogue, and the `detail` contract text names it so an agent facing one of
those tools goes straight to `full`.

## Consequences

- An agent that knows which tool it wants gets the argument list at under a third of the schema
  at the median tool and a seventh of it in aggregate, and the two-step flow ADR-0472 teaches
  gains a cheaper second step — for the 90 of 124 tools the tier answers completely, and
  partially for the 17 more that carry a mix. The contract text quotes the median rather than
  the aggregate: the aggregate is pulled down by a handful of very large schemas, so it is not
  the number that describes the one tool a caller is deciding about.
- For the 17 tools whose only parameter is a payload model, the tier is a wasted round trip. It
  is not wrong there, only unhelpful, and §3 puts the incidence in the contract text so the cost
  is predictable rather than discovered.
- `full` responses grow 8.6% in aggregate and 10.6% at the median (+119 B per match). `full` is
  the tier a caller opts into deliberately, and the tier added here is what a cost-sensitive
  caller uses instead, so the growth lands on the path that was already the expensive one.
- The three tiers are monotone, so "ask for more detail" can never remove a key. A client that
  already treats `input_schema` and `description` as optional treats `parameters` the same way,
  and every existing caller that passes no `detail` gets a byte-identical response.
- `type` is a display string with no schema grammar behind it. It is for a reader deciding what to
  pass, and a client must not parse it as a type expression; `input_schema` remains the machine
  contract. A union renders in member order, so `string|null` and `null|string` are both
  reachable renderings of different schemas.
- KDIVE now renders parameter types in two grammars for two audiences. `render_schema_type`
  (ADR-0177) writes the committed tool reference, where `tools.search.names` reads
  `array<string> (nullable)`; this tier writes the live response, where the same parameter reads
  `array[string]|null`. The divergence is deliberate but it is a maintenance cost: a reader
  comparing the reference against a response sees two spellings of one type. This is accepted
  permanently rather than tracked as work, because the rejected alternative below shows the two
  cannot be merged without either inlining every schema on every search call or making an
  accepted doc-generation decision fail-soft. One condition would reopen it: if the registry ever
  advertises pre-inlined schemas at runtime, `render_schema_type` becomes reusable and this tier
  should adopt it rather than keep a second grammar.
- Rendering a `$ref` by its definition name surfaces private pydantic model names such as
  `_RunsListPayload` in the agent-facing response. Those names are already in the `full` tier's
  `$ref` targets and `$defs` keys, so this discloses nothing the schema did not, but it does put
  them on a cheaper and more frequently taken path.
- A parameter whose schema matches none of the recognised shapes renders as `unknown` rather than
  being omitted, so the argument list stays complete and an unrenderable type is visible instead
  of silent. No live property reaches it — all 272 top-level properties render through a
  recognised shape — so it is a defensive branch, reachable only by a schema shape the catalogue
  does not currently contain. It is deliberately not the fail-loud `ValueError` ADR-0177 chose
  for the doc generator: a stale committed document should stop a build, but one unrenderable
  property should not fail a live discovery call that is otherwise answerable.
- One level up, that visibility stops: an empty `parameters` list means either that the tool
  takes no arguments — 9 tools today — or that the schema carried no readable `properties` map
  at all, and the wire cannot tell those apart. No registered tool reaches the second case, and
  both degraded projection paths fall back to a schema that still has `properties`. The
  conflation is accepted rather than given a fourth wire state: `full` is the disambiguator, and
  a tool whose arguments sit behind a root model would be the reason to revisit it.
- `names` mode still forces `full` (ADR-0630 §2) and is untouched: a by-name lookup exists to
  obtain the schema, and the middle tier does not carry one.

## Considered & rejected

- **A pruned `input_schema` under the existing key.** verified: the two tiers would then disagree
  about what `input_schema` means while ADR-0472 §1 guarantees that no key changes meaning between
  modes (`docs/adr/0472-summary-first-tool-search.md` §1), and a client holding a pruned schema
  could not tell it from a complete one.
- **A `parameters` key on the middle tier only, not on `full`.** verified: measured at +8.6%
  aggregate on the full-match shape, the saving is real, but it makes the tiers non-monotone — a
  caller stepping from
  `parameters` to `full` loses the `parameters` key and has to re-derive the list by walking
  `$defs`. ADR-0472 §1 paid the same class of cost for the same shape-stability reason.
- **Reusing `render_schema_type` from the doc generator (ADR-0177).** verified: it raises
  `ValueError("schema renderer cannot resolve $ref/$defs; inline the schema")` on any node
  carrying `$ref` or `$defs` (`scripts/generate/gen_tool_reference.py`), and the runtime schemas
  this tier reads carry both — 30 of 124 tools have top-level `$defs` and 10 properties are a
  bare `$ref`, measured through `registered_tools(app)`. The generator only avoids that because
  it renders a pre-inlined view. Reusing it would mean either inlining every schema on every
  search call or making a doc-generation helper fail-soft, and the second changes an accepted
  decision from outside its scope.
- **Carrying inline `enum` values and other value constraints.** judgment: #2341 scopes the tier
  to names, types, and required flags; constraints are what `full` is for, and adding them here
  makes the middle tier a second schema format to keep in step with the first.
- **Naming the tier `signature` or `brief`.** judgment: the response key and the tier would carry
  different words for one thing, and `brief` says how big it is rather than what it holds.
- **A separate `tools.describe(name)` tool.** verified: rejected twice already, in
  `docs/adr/0472-summary-first-tool-search.md` "Rejected alternatives" and
  `docs/adr/0630-by-name-tool-lookup-and-diagnosable-search-miss.md` "Considered & rejected",
  both on the ground that a parameter on the tool that owns discovery adds no tool and no second
  response shape.
- **Doing nothing.** verified: `tools.search`'s own `detail` parameter offers `summary` and `full`
  and nothing else (`src/kdive/mcp/tools/gateway.py`, `SearchDetail`), so the argument list costs
  a median 499 B schema to read 119 B of facts, and #2341 records that gap as the reason it was
  filed.
