# ADR 0485 — Two purpose-keyed skip sets, because de-duplication and volume control are not the same reason

- **Status:** Accepted
- **Date:** 2026-07-28
- **Issue:** #1654
- **Amends:** [ADR-0268](0268-tool-gateway-dispatcher.md) §6. That section justified one
  skip set, `{tools.invoke, tools.search}`, entirely by `run_middleware=True` re-entry, and
  asserted for every member that "the inner call is the sole recorder". That is true of
  `tools.invoke` and false of `tools.search`, which never re-enters. §6's *mechanism* is
  corrected here and its skip is split in two; the gateway design ADR-0268 §1–§5 and §7
  describe is unchanged.

## Context

`META_TOOLS` (`mcp/middleware/shared.py`) named two tools, and three middlewares
short-circuited on it: `UsageTrackingMiddleware` (no `tool_invocation` row),
`TelemetryMiddleware` (no `mcp.tool/<name>` span, no RED metrics), and
`DenialAuditMiddleware` (no `audit_log` denial row).

Its comment gave one reason for both members:

> Each name here re-enters the middleware chain via `app.call_tool(run_middleware=True)`;
> without the skip the outer chain would double-count every usage/telemetry/denial row.

**That mechanism is real for `tools.invoke`.** `tools_invoke` (`mcp/tools/gateway.py`)
calls `app.call_tool(name, arguments or {}, run_middleware=True)`, so the inner tool runs
the whole chain nested inside the outer `tools.invoke` chain. Every per-call recorder
fires twice, and the second row is not merely redundant — on a denial it is
*misattributed*, keyed to `tools.invoke` rather than to the tool that was actually denied.
The skip is genuine de-duplication and ADR-0268 §6 is right about it.

**It is absent for `tools.search`.** `tools_search` reads `registered_tools(app)`, filters
with `tool_visible`, ranks, and returns a `ToolResponse`. There is no `app.call_tool` on
that path and no inner call at any depth. So there is no inner record for the skip to
defer to: membership did not de-duplicate a second row, it suppressed the *only* row that
would ever exist. No `tool_invocation` row, no span, no RED metric — for what ADR-0456
(#1581/#1582) made an agent's primary discovery call by defaulting the gateway on.

ADR-0268 §6 does state a second, independent reason in its last sentence — "this
*improves* the ADR-0148 usage data: it measures real work, not dispatcher noise" — which
is a volume/signal-quality argument, not a re-entry argument, and it does apply to
`tools.search`. That is why the status quo was defensible and why this is a decision
rather than a one-line deletion. What was not defensible was one set carrying two
different reasons under a comment that stated only the reason that does not apply to half
its membership: a stated-but-untrue invariant, which is what let the gap sit unnoticed
through #1625's review.

The two recording planes do not have the same cost, and that is what the single set
flattened away:

- A `tool_invocation` row is an unconditional per-call **write to Postgres**, taken from a
  pool whose acquire budget is 1 second and whose exhaustion silently drops rows
  (ADR-0148 §3's swallow, counted since ADR-0449). Discovery is the highest-frequency call
  an agent makes; a row per search is real, unbounded, growing write load on the same pool
  that serves real work.
- A span and three metric points are **in-process, cheap, and sampleable**, and they are
  the plane where discovery is actually diagnosed: search latency, error rate, and
  (via the existing `tool_search_miss` structured log alongside them) zero-result queries.

## Decision

### 1. Replace `META_TOOLS` with two purpose-keyed frozensets

```python
REENTRANT_TOOLS: frozenset[str] = frozenset({"tools.invoke"})
UNMETERED_TOOLS: frozenset[str] = frozenset({"tools.search"})
```

Each set's name *is* its justification, so a future member is added to the set whose reason
it actually satisfies, and a reader of any one skip site can see which reason applies
without reconstructing it. `META_TOOLS` is removed outright rather than aliased — a name
that meant "both reasons at once" is the defect, so keeping it available would let the
conflation return.

`REENTRANT_TOOLS` is the de-duplication set: a member dispatches through
`app.call_tool(..., run_middleware=True)`, so the inner call is the authoritative record
and the outer chain must record nothing. `UNMETERED_TOOLS` is the volume set: a member does
not re-enter and has no inner recorder, so skipping it is a deliberate choice to forgo the
only record, taken because the per-call cost on that plane is not worth its signal.

### 2. Each middleware skips the sets whose reason applies to it

| middleware | skips | why |
|---|---|---|
| `UsageTrackingMiddleware` | `REENTRANT_TOOLS \| UNMETERED_TOOLS` | de-duplication *and* volume: a row is a per-call DB write |
| `TelemetryMiddleware` | `REENTRANT_TOOLS` | de-duplication only; spans/metrics are cheap and sampled |
| `DenialAuditMiddleware` | `REENTRANT_TOOLS` | de-duplication only; the misattributed-row hazard is re-entry-specific |

The net behavioural change is exactly one thing: **`tools.search` is now traced and
metered.** It emits an `mcp.tool/tools.search` span and its RED counter/histogram points
like any other tool, so discovery latency and error rate become answerable from the
telemetry that already exists. It still writes no `tool_invocation` row.

The `DenialAuditMiddleware` arm is a correctness alignment with no observed behaviour
change: `tools.search` is in `CORE_TOOLS` (`mcp/exposure.py`) and RBAC-filters its results
internally via `tool_visible` rather than raising, so a `RoleDenied` escaping it is not a
path this ADR has pinned. No audit-row loss is claimed for it. It moves to
`REENTRANT_TOOLS` because that is the set whose *reason* the arm implements — the
misattributed second denial row is a re-entry hazard — not because a row is known to be
lost today.

### 3. What the usage plane still cannot answer, stated rather than implied

Declining the row means `tool_invocation` cannot answer *what are agents searching for*,
*how often do they search before invoking*, or *which searches return nothing*. Those are
real questions a later exposure refinement would ask, and this ADR does not answer them.
Two signals partially cover them without a per-call row — the existing `tool_search_miss`
/ `tool_search_namespace_miss` structured logs (zero-result queries and namespaces) and,
now, the span/metric pair (call rate and latency). If a future exposure-tuning effort needs
the argument-level detail, the decision to revisit is this one, and the reason to revisit
it will be a named question, not the absence itself.

## Consequences

- **`tools.search` gains a span and RED metrics.** New cardinality is one additional
  `tool` label value on counters that are already per-tool — bounded, not agent-controlled.
- **Usage-plane counts are unchanged.** `tool_invocation` still contains no `tools.invoke`
  or `tools.search` rows, so every dashboard, report, and accounting query over that table
  reads exactly as before. No migration, no schema change, no MCP or RBAC surface change.
- **The false invariant is gone.** Each skip site names the set whose reason it
  implements, so "why is this tool skipped here" is answerable at the call site.
- **A third reason would need a third set,** not a third member of an existing one. That is
  the intended cost: it forces the reason to be stated before the skip is taken.
- **The gateway's de-duplication guarantee is untouched.** `tools.invoke` remains in the
  skip set of all three middlewares, so ADR-0268 §6's single-row-per-real-call property and
  the audit-attribution correctness that depends on it are preserved exactly.

## Rejected alternatives

- **Keep one set, fix only the comment** (issue option 2). Cheapest, and it would remove
  the untrue statement, but it leaves discovery unobservable on *both* planes and leaves
  one set meaning two things — the next member added to it would face the same ambiguity.
  The comment was the symptom; the conflated set was the defect.
- **Remove `tools.search` from the skip entirely** (issue option 1), recording a
  `tool_invocation` row per search. Answers the most questions, and costs a Postgres write
  on the highest-frequency agent call, taken from the pool that serves real work under a
  1-second acquire budget whose exhaustion drops rows silently. Not worth it for a call
  that consumes no resource and mutates nothing.
- **Record discovery under a distinct outcome or meta marker** (issue option 3). Keeps
  invocation counts clean while persisting searches, but it pays the same per-call write
  and additionally splits the `outcome` vocabulary — ADR-0148 keeps `denied` distinct
  precisely because outcome values are load-bearing, and adding a non-outcome value to that
  column to encode "this was a meta call" overloads the wrong field.
- **A per-middleware boolean on a single set** (`META_TOOLS` plus `skip_usage` /
  `skip_telemetry` flags). Same expressive power, but the reason stays implicit in which
  flags happen to be set, and a reader at the skip site sees a flag rather than a reason.
- **Keep `META_TOOLS` as an alias for the union.** Would spare the import churn in tests,
  and would leave in place the exact name whose meaning is the defect.
