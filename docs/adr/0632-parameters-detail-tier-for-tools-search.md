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
whether the parameter is required. Measured over the 124 registered tools, a full projected
schema runs 67 B to 14,185 B with a median of 499 B, while those three facts for the same tool
come to 2 B to 737 B with a median of 119 B — 28.9% of the schema at the median and 14.4% of it
in aggregate. `runs.install` is 1,993 B of schema for a 257 B argument list.

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

The tier is narrowed by the same provider-kind projection `full` already applies, so it can never
advertise a parameter the projection removes.

### 2. `parameters` rides the `full` tier too

ADR-0472 §1 made the summary keys a strict subset of the full keys, so a client parses one shape
with optional keys instead of branching on a mode discriminator. Adding a middle tier that
introduced a key `full` does not carry would break that: stepping up to more detail would *lose*
a key. So `full` carries `parameters` as well, and the three tiers stay monotone — each tier's
key set is a superset of the tier below it.

That is the same trade ADR-0472 §1 made when it put `summary` in both modes, and it is redundant
in the same way: `parameters` is derivable from `input_schema` by any caller that holds it. The
measured cost is +14.4% on a full response in aggregate.

### 3. The tier reports the argument list, not the argument constraints

`parameters` carries the three facts #2341 asks for and stops there. A parameter's value
constraints — an inline `enum`, a `pattern`, a numeric bound — are not in the entry, and a
parameter whose type renders as a definition name is telling the caller that the definition is in
`full`. The tier's contract text says this so an agent knows when the middle tier is not enough,
rather than discovering it through a validation failure.

## Consequences

- An agent that knows which tool it wants gets the argument list at roughly a seventh of the
  aggregate schema cost, and the two-step flow ADR-0472 teaches gains a cheaper second step.
- `full` responses grow 14.4% in aggregate (median +119 B per match). `full` is the tier a caller
  opts into deliberately, and the tier added here is what a cost-sensitive caller uses instead, so
  the growth lands on the path that was already the expensive one.
- The three tiers are monotone, so "ask for more detail" can never remove a key. A client that
  already treats `input_schema` and `description` as optional treats `parameters` the same way,
  and every existing caller that passes no `detail` gets a byte-identical response.
- `type` is a display string with no schema grammar behind it. It is for a reader deciding what to
  pass, and a client must not parse it as a type expression; `input_schema` remains the machine
  contract. A union renders in member order, so `string|null` and `null|string` are both
  reachable renderings of different schemas.
- Rendering a `$ref` by its definition name surfaces private pydantic model names such as
  `_RunsListPayload` in the agent-facing response. Those names are already in the `full` tier's
  `$ref` targets and `$defs` keys, so this discloses nothing the schema did not, but it does put
  them on a cheaper and more frequently taken path.
- A parameter whose schema matches none of the recognised shapes renders as `unknown` rather than
  being omitted, so the argument list stays complete and an unrenderable type is visible instead
  of silent.
- `names` mode still forces `full` (ADR-0630 §2) and is untouched: a by-name lookup exists to
  obtain the schema, and the middle tier does not carry one.

## Considered & rejected

- **A pruned `input_schema` under the existing key.** verified: the two tiers would then disagree
  about what `input_schema` means while ADR-0472 §1 guarantees that no key changes meaning between
  modes (`docs/adr/0472-summary-first-tool-search.md` §1), and a client holding a pruned schema
  could not tell it from a complete one.
- **A `parameters` key on the middle tier only, not on `full`.** verified: measured at +14.4%
  aggregate, the saving is real, but it makes the tiers non-monotone — a caller stepping from
  `parameters` to `full` loses the `parameters` key and has to re-derive the list by walking
  `$defs`. ADR-0472 §1 paid the same class of cost for the same shape-stability reason.
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
