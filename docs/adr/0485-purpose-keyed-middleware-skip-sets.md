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
the whole chain nested inside the outer `tools.invoke` chain, and the usage and telemetry
recorders each fire twice. The skip is genuine de-duplication on those two planes and
ADR-0268 §6 is right about it there. It is **not** right about the denial plane — §6's
"second, misattributed `platform_audit_log` row keyed to `tools.invoke`" cannot occur, for
the reason set out in §2. That is a second false claim in the same paragraph, found by
review of this ADR's own first draft, which had repeated it.

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
- A span and two metric points (three on an error outcome) are **in-process** — no network
  round-trip, no durable write, nothing taken from the connection pool that serves real
  work — and the span is additionally **sampled**: `ParentBased(TraceIdRatioBased(ratio))`
  with `ratio` from `OTEL_TRACES_SAMPLER_RATIO`, default 0.1 (`observability/facade.py`),
  so the ratio governs root spans and a span under an already-sampled parent is kept. The
  metric points are not sampled; they are counter and histogram updates in local memory,
  aggregated before export. And this is the plane where
  discovery is actually diagnosed: search latency, error rate, and — via the existing
  `tool_search_miss` structured log alongside them — zero-result queries.

## Decision

### 1. Replace `META_TOOLS` with two purpose-keyed frozensets

```python
REENTRANT_TOOLS: frozenset[str] = frozenset({"tools.invoke"})
UNMETERED_TOOLS: frozenset[str] = frozenset({"tools.search"})
```

Each set's name *is* its justification, so a future member is added to the set whose reason
it actually satisfies, and a reader of a skip site can see which reason applies without
reconstructing it. The usage plane is the one site that skips both, and it names the union
`_UNRECORDED_TOOLS` after what it does rather than after a reason — the two reasons are
stated on the union's definition, which is the only place both apply at once.
`META_TOOLS` is removed outright rather than aliased — a name
that meant "both reasons at once" is the defect, so keeping it available would let the
conflation return.

`REENTRANT_TOOLS` is the de-duplication set: a member dispatches through
`app.call_tool(..., run_middleware=True)`, so the inner chain is the authoritative record
and the outer chain must record nothing. That holds even when the inner dispatch never
reaches a tool — FastMCP builds the middleware context and runs the chain *before*
resolution, resolving inside `call_next`, so an unknown or disabled inner name raises
`NotFoundError` out of `call_next` and is recorded by the inner chain as an `error` outcome
keyed to the name that was asked for, before `tools_invoke` maps it to a
`configuration_error` envelope. There is no pre-dispatch failure the skip silences.

That last point is a claim about a third-party internal, so it is pinned to a version:
verified against **fastmcp 3.4.4**, `fastmcp/server/server.py`'s `FastMCP.call_tool`, where
the `if run_middleware:` branch constructs the `MiddlewareContext` and calls
`_run_middleware` with no prior resolution, and `call_next` recurses with
`run_middleware=False` into the only branch that calls `get_tool` and raises `NotFoundError`.
A dependency bump could invalidate it; a reader doubting the skip should re-check there
first, because the natural reading — that a failed resolution never reaches the chain — is
wrong and has been raised as a defect once already.

`UNMETERED_TOOLS` is the volume set: a member does
not re-enter and has no inner recorder, so skipping it is a deliberate choice to forgo the
only record, taken because the per-call cost on that plane is not worth its signal.

### 2. Each middleware skips the sets whose reason applies to it

| middleware | skips | why |
|---|---|---|
| `UsageTrackingMiddleware` | `REENTRANT_TOOLS \| UNMETERED_TOOLS` | de-duplication *and* volume: a row is a per-call DB write |
| `TelemetryMiddleware` | `REENTRANT_TOOLS` | de-duplication only; spans/metrics stay in-process |
| `DenialAuditMiddleware` | `REENTRANT_TOOLS` | **not** de-duplication — see below; ordering-defensive, and unreachable today |

The net behavioural change is exactly one thing: **`tools.search` is now traced and
metered** — pinned at unit level (see §3). It emits an `mcp.tool/tools.search` span and its
RED counter/histogram points like any other tool, so discovery latency and error rate become
answerable from the telemetry that already exists. It still writes no `tool_invocation` row.

De-duplication is a real hazard on exactly two of the three planes. `UsageTrackingMiddleware`
and `TelemetryMiddleware` sit *outside* the re-entry (`mcp/assembly/app.py`), so without a
skip each would record the dispatcher alongside the inner call. **`DenialAuditMiddleware`
would not, and this ADR does not claim it would.** That middleware is registered in the same
chain, so the re-entered inner call runs its own instance, and that instance *catches*
`RoleDenied` and **returns** `ToolResponse.denied(inner_tool)` — it never re-raises. The
exception therefore cannot reach an outer instance, and the "second, misattributed denial row
keyed to `tools.invoke`" that ADR-0268 §6 warns about is structurally unreachable. This is
independent of #1635: today what crosses the seam is a `ToolError` wrap, and once #1635 lands
the inner arm absorbs it.

`tools.invoke` stays in the skip regardless. The arm is kept as an **ordering-defensive**
guard — it costs nothing and it stops a future middleware reordering, or a future re-raise on
that path, from turning a dispatcher name into an audit row — but it is documented as
unreachable rather than as de-duplication. Stating the hazard as real when it is not would be
the same defect this ADR exists to remove.

Nor is an audit-row *loss* claimed for `tools.search`. It cannot raise `RoleDenied` at all:
it is in `PUBLIC_TOOLS` and absent from `_TOOL_SCOPES` (`mcp/exposure.py`), so no exposure
scope gates it, and `tools_search` (`mcp/tools/gateway.py`) calls no `require_role` anywhere —
it filters its own matches with `tool_visible` and returns them. (`CORE_TOOLS` membership is
sometimes cited here; it is the default-*listed* set and has no bearing on whether a denial
can be raised.)

### 3. How far "now traced and metered" is pinned

At **unit level only.** `tests/mcp/middleware/test_gateway_skip.py` drives
`TelemetryMiddleware` directly, constructed over a fake tracer and meter with hand-built
`SimpleNamespace` contexts; it never goes through `build_app`. So the tests prove the
middleware emits a span and metric points for `tools.search` on both its success and its
error exit — which is what this decision changes — and they do **not** prove the assembled
app wires that middleware such that a real `tools.search` call reaches it.

That end-to-end gap is not this ADR's to close: it is exactly #1640, which exists because
`telemetry.py`'s skip has never been pinned through a real dispatch (deleting it leaves the
suite green). Recording the limit here so a reader does not mistake a green run of the unit
module for end-to-end proof.

### 4. What the usage plane still cannot answer, stated rather than implied

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
  skip set of all three middlewares, so ADR-0268 §6's single-row-per-real-call property is
  preserved exactly on the two planes where it was ever at stake.
- **One of ADR-0268 §6's two hazards turns out never to have existed.** The denial-plane
  double-audit it warns about cannot happen, so that skip is now documented as
  ordering-defensive. Nothing is removed for it — the cost of keeping a guard against a
  hazard that would become real under a reorder is a set lookup — but the *stated reason*
  now matches the code, which is the whole point of this ADR.

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
