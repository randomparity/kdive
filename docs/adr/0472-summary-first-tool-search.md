# ADR 0472 — Summary-first `tools.search` and a namespace authorization signal

- **Status:** Accepted
- **Date:** 2026-07-28
- **Issue:** #1597
- **Amends:** [ADR-0456](0456-agent-operator-mcp-exposure-profiles.md) §3's search-metadata
  contract.

## Context

[ADR-0456](0456-agent-operator-mcp-exposure-profiles.md) §6 flipped the gateway on by default, so
`tools.search` is now the primary discovery path for every normal agent client rather than a
fallback. The real-client cold-start proof recorded in
[`2026-07-27-mcp-exposure-profiles-proof-record-1582.md`](../design/2026-07-27-mcp-exposure-profiles-proof-record-1582.md)
(#1582) surfaced two costs that only matter once search is on the default path.

**Every match carried its full projected `input_schema`.** ADR-0456 §3 required results to carry
"projected input schema, description, annotations, and maturity metadata", and the implementation
read that as *unconditionally*. Measured on this branch's parent, one default `limit: 10` query
(`"boot a built kernel"`, contributor grants) returned a 26.9 KB envelope of which 13.9 KB was
inline schemas and a further 11.3 KB was full tool descriptions — most of it about the nine tools
the agent was going to discard. The live proof measured ~54 KB on a broader query and overflowed
the client's context into a persisted file. The discovery tool that exists to make a large registry
cheap was the most expensive call in the session.

**A namespace the caller is not authorized for was indistinguishable from a nonexistent one.**
Candidates are RBAC-filtered before ranking, so `tools.search(namespace="ops")` on a contributor
token returned an empty match list — the same response a typo produces. The server's own
instructions advertise `ops` in their namespace table of contents to every caller, so the agent had
been told the plane exists and then told it is empty. In the proof the agent recovered only by
reading `resource://kdive/docs/guide/safety-and-rbac.md`.

Both are refinements of #1578's schema-aware search, not regressions: the behavior was correct for
a fallback tool and is wrong for a default one.

## Decision

### 1. Matches are summaries by default; full detail is opt-in

`tools.search` gains a `detail` parameter (`summary` | `full`) defaulting to `summary`.

- A **summary** match carries `name`, `summary`, `annotations`, and `maturity`. `summary` is the
  first paragraph of the tool's description, whitespace-collapsed to a single line — the
  capability sentence an agent needs to *choose* a tool, without the invocation notes, RBAC prose,
  and failure taxonomy that follow it in a KDIVE tool docstring.
- A **full** match carries those four keys plus `description` (the complete description) and
  `input_schema` (the projected schema, exactly as before).

Summary keys are a strict subset of full keys, and no key changes meaning between modes: `summary`
is always the first paragraph and `description`, when present, is always complete. A client can
therefore parse one shape and treat `input_schema`/`description` as optional rather than branching
on a mode discriminator.

This amends ADR-0456 §3: the projected schema and the full description are now returned on demand
rather than unconditionally. **The safety metadata is not** — `annotations` and `maturity` ride
every match in both modes, so an agent can never reach a tool it is unable to classify. That is the
half of §3 the classification contract actually depends on; a tool invoked with no arguments at all
is still classified, and a tool invoked *with* arguments necessarily went through a `full` fetch
first.

### 2. An exact tool name ranks first, so a single-tool schema fetch is deterministic

Making schemas opt-in is only safe if an agent that found a name in a summary result can reliably
get that name's schema. Query ranking is by score then name ascending, so a query for `runs.boot`
could rank another tool first merely by mentioning the string — `limit: 1` would then fetch the
wrong schema.

A query whose whole text equals a candidate's tool name (case-insensitively, stripped) now sorts
ahead of every other hit. `tools.search(query="runs.boot", detail="full", limit=1)` is therefore a
deterministic one-tool schema fetch, and that two-step flow — cheap summary search, then one full
fetch for the chosen tool — is what the `tools.search` docstring teaches.

### 3. Namespace mode reports whether the plane exists, and what grant it needs

In namespace mode the response carries `namespace_status`:

- `ok` — the plane exists and at least one of its tools is visible to the caller.
- `unauthorized` — the plane has registered tools, and every one of them is RBAC-filtered for this
  caller. The response also carries `namespace_required_grants`: the sorted union of the
  `ExposureScope` values across the plane's tools, i.e. the any-of set of grants of which holding
  any one would reveal at least one tool in the plane.
- `unknown` — no registered tool carries that prefix.

A namespace miss (`unauthorized` or `unknown`) is logged alongside the existing query miss, so
namespace vocabulary drift is as visible to curation as query vocabulary drift.

### 4. The disclosure is bounded by the published RBAC matrix

`unauthorized` deliberately reveals something the pre-#1597 empty result hid, so the bound is
explicit: it discloses strictly less than documentation KDIVE already publishes.

- *That the plane exists* is already advertised to every caller, unfiltered, in the server
  instructions' namespace table of contents (`NAMESPACE_TOC`) — the same table that made the empty
  result confusing.
- *Which grants it needs* is already published per tool, for every tool and every profile, by the
  generated role→tool visibility matrix in
  [`docs/guide/safety-and-rbac.md`](../guide/safety-and-rbac.md) (#347). A union over one prefix is
  derivable from that table by anyone who can read it.
- **No tool names are returned.** `namespace_required_grants` is a set of grant names, never a
  list of tools, so the response never enumerates a capability the caller cannot see.

The signal stays advisory, exactly like the exposure filter it explains
([ADR-0148](0148-rbac-scoped-tool-exposure.md)): execution-time RBAC is unchanged and remains the
only boundary.

## Consequences

- The default discovery call gets roughly an order of magnitude cheaper. Measured on this branch,
  the default `limit: 10` query above drops from 26.9 KB to 2.8 KB; `namespace="debug"` drops from
  6.8 KB to 3.0 KB. Discovery becomes a call an agent can issue freely, which is the behavior the
  gateway was defaulted on for.
- Reaching an invocable schema now costs a second round trip for the chosen tool. That is the
  intended trade: one 1–3 KB schema instead of ten.
- `detail: "full"` with `limit: 50` can still return a large payload. That is opt-in and bounded by
  the caller's own `limit`; the defect this ADR fixes was the *default*, and adding a second,
  smaller cap for full mode would silently return fewer results than the caller asked for.
- An agent can tell an empty plane from a forbidden one without reading a doc resource, and learns
  the grant to ask for rather than only that it lacks one.
- `describe_tool` takes a required keyword-only `detail`, so a future caller must choose a mode
  rather than inherit whichever default was convenient.

## Rejected alternatives

- **Limit-conditioned schemas** (include schemas only when `limit` is small). The response shape
  would depend on an argument that means "how many results", so an agent could not ask for cheap
  results *and* many of them, and could not predict which shape it would get.
- **A separate `tools.describe(name)` tool.** It adds a third gateway tool for what an exact-name
  query already does, and epic #1576 spent thirteen issues removing tools whose whole body was a
  narrower call into an existing one.
- **Truncating descriptions to a byte budget.** A character cap cuts mid-sentence and makes the
  summary's length an implementation detail; the docstring's first paragraph is already the
  authored summary, enforced by the ADR-0047 documentation guard.
- **Reporting `unauthorized` with the filtered tool names.** It would enumerate capabilities the
  caller cannot invoke, going beyond the published matrix's per-tool grant table for no gain the
  grant union does not already provide.
- **Leaving the namespace signal out entirely.** The alternative recovery path is reading a doc
  resource, which costs more context than the schemas this ADR removed.
