# ADR 0148 — RBAC-scoped tool exposure in `list_tools` + usage tracking

- **Status:** Proposed
- **Date:** 2026-06-17
- **Deciders:** kdive maintainers

## Context

`build_app()` registers all ~71 plane tools unconditionally and `list_tools` returns the
full flat catalog to every caller (`mcp/app.py` `_PLANE_REGISTRARS`). LLM tool-selection
accuracy degrades as the catalog grows (#506 cites LongFuncEval: selection drops with
catalog size even at 128K context). RBAC is enforced only at execution time inside each
handler (`require_role(ctx, project, Role.OPERATOR)`), so the model sees — and may
attempt — tools it can never successfully call.

#347 asks for the same `list_tools` filter from a least-privilege angle. #506 is the
accuracy-driven view and notes the two share a mechanism. The maintainer chose: ship
RBAC-scoped exposure now, and add a usage-tracking backend so the data needed to justify
a finer (workflow/phase) exposure later is captured from the start. The design spec is
`docs/design/tool-exposure-scoping.md`.

Two facts shape the mechanism:

- FastMCP 3.4.0 has an `on_list_tools` middleware hook returning `Sequence[Tool]` — the
  natural per-connection filtering seam, alongside the existing telemetry/denial-audit
  middleware. No FastMCP-internal mutation (unlike ADR-0113's `_components` sweep).
- `RequestContext` carries **per-project** `roles: Mapping[project, Role]` plus
  connection-level `platform_roles`. `list_tools` is connection-scoped, so the filter can
  only reason about the *union* of the caller's grants.

## Decision

### 1. Filter `list_tools` per connection via a new `ToolExposureMiddleware`

The middleware's `on_list_tools` reads the verified-token `RequestContext`
(`current_context()`) and returns only the tools the caller may invoke under the
**conservative union rule**: hide a tool only when the caller could invoke it in **no**
granted project and holds no platform role that satisfies it.

- Project tool requiring role `R`: shown iff `max(roles over granted projects) ≥ R`
  (`viewer < operator < admin`).
- Platform tool requiring role `P`: shown iff `platform_roles` satisfy `P` (honouring
  `_PLATFORM_IMPLIES`, `platform_admin ⊇ platform_auditor`).
- `PUBLIC` tool: always shown.

The filter is **advisory and fails open** — on a missing context or any internal error it
returns the unfiltered catalog and logs. It is **not** a security control: execution-time
`require_role` / `require_platform_role` / the destructive-op gate remain the only
enforcement (ADR-0006, ADR-0020, ADR-0043). Hiding a tool from the listing does not
protect it.

### 2. Classify tools in a central reviewed map (`mcp/exposure.py`)

Each tool maps to an `ExposureScope`, using grouped frozensets + a guard test — the
existing `_docmeta.DESTRUCTIVE_TOOLS` idiom — not per-registration metadata across 71
tools. The default for an unclassified tool is `PUBLIC` (fail-open on exposure). A
classification must be **≤** the handler's real requirement: too-permissive only costs
catalog size; too-restrictive hides a usable tool, the one forbidden outcome. A
completeness guard test asserts every registered tool is classified so a new privileged
tool cannot silently leak into the low-privilege catalog.

This is the filter #347 requested; a future workflow/phase filter chains inside the same
`on_list_tools` seam and intersects (never widens) this result.

### 3. Capture usage in an append-only `tool_invocation` table

Migration `0039_tool_invocation.sql` adds a table modelled on `platform_audit_log`:
`id`, `ts`, `principal`, `agent_session`, `project` (nullable), `tool`, `outcome`
(`ok` | `error` | `denied`, CHECK-constrained), `actor` (NOT NULL default `'agent'`),
`client_id` (nullable). A `UsageTrackingMiddleware.on_call_tool` records one row per call
**best-effort**: own pool connection, recording failure logged and swallowed, never fails
or delays the call (the `DenialAuditMiddleware` precedent). It sits just inside
`TelemetryMiddleware` (which stays outermost) so it observes the final enveloped outcome
after `DenialAuditMiddleware` maps a denial — classifying `denied` from
`authorization_denied`, `error` from any other failure envelope or propagated exception,
`ok` otherwise. `denied` is kept distinct because a denied call is direct evidence the
agent reached for a tool it cannot use — the signal a later exposure refinement wants.

## Consequences

- A project operator/viewer (the common case) sees a materially smaller catalog; a
  caller with no grants sees only the `PUBLIC` core. The fully-privileged path
  (`platform_admin` + project `admin`) is unchanged from today.
- The classification map carries a maintenance obligation: a new tool must be classified,
  enforced by the completeness guard. The map can drift *more permissive* than a
  handler's real requirement without harming safety (only catalog size); the guard and
  the "≤ requirement" rule keep it from drifting *more restrictive*.
- Every tool call now writes one durable analytics row. The table grows with traffic
  (polling loops included); retention/aggregation is a future concern, noted as
  follow-up, not built here.
- `tool_invocation` is distinct from `audit_log` (security transitions/denials) and
  `platform_audit_log` (cross-project reads). It is operational analytics, not an audit
  trail, and carries no `args_digest`.

## Considered & rejected

- **Per-registration tool metadata (a `required_role` tag on each `@app.tool`).**
  Co-locates classification with enforcement (less drift) but means ~71 invasive edits and
  relies on every future registration remembering the tag. The central map + completeness
  guard reaches the same safety in one reviewable file, matching the `DESTRUCTIVE_TOOLS`
  precedent. Rejected.
- **Static source-introspection of `require_role` calls to derive the map.** Enforcement
  is imperative inside handlers with no declarative source; a reliable scanner is brittle
  and high-effort. The fail-open "≤ requirement" rule plus the union filter makes a hand
  map safe enough. Rejected.
- **Treat list filtering as a security boundary / fail closed.** A filtered listing is
  trivially bypassed by a caller that knows the tool name, and failing closed would break
  tool discovery on any context glitch. The filter is advisory; execution RBAC is the
  boundary. Rejected.
- **Workflow/phase-scoped opt-in groups now.** The accuracy win is larger but it adds a
  new agent-interaction contract and risks hiding a tool mid-workflow. Deferred until the
  `tool_invocation` data shows it is warranted. Rejected for this round (the maintainer's
  explicit "start with RBAC, add the tracking backend" choice).
- **Add a usage-analytics read tool now.** Adding tool surface undercuts the
  catalog-reduction goal; capture is the in-scope deliverable, reading is follow-up.
  Rejected for this round.
- **Fold usage tracking into `TelemetryMiddleware` / OTel metrics.** Telemetry
  deliberately omits principal/project labels to avoid free-cardinality metric labels
  (ADR-0090 §4). Per-call high-cardinality analytics belong in a table, not a metric.
  Rejected.
