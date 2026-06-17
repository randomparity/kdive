# Design: RBAC-scoped tool exposure + usage tracking (#506)

Status: accepted (implemented by ADR-0148)

## Problem

`build_app()` registers all ~71 plane tools unconditionally
(`mcp/app.py` `_PLANE_REGISTRARS`), and every `list_tools` response returns the full
flat catalog to every caller. Tool-selection accuracy for LLM agents degrades as the
catalog grows — the LongFuncEval result cited in #506 shows selection performance
dropping with catalog size even at 128K context. A large flat catalog makes the model
likelier to pick the wrong tool or mis-sequence a workflow.

RBAC is enforced only at **execution** time, inside each handler
(`require_role(ctx, project, Role.OPERATOR)` etc.), so the model is shown — and may
attempt — tools it can never successfully call. #347 ("Evaluate MCP Tool Listing Based
on Profile") asks for the same listing filter from a least-privilege angle; this design
**subsumes** it.

## Goals

1. `list_tools` returns a per-connection catalog that omits tools the caller could never
   invoke under any of its grants, shrinking the catalog for the common (project
   operator/viewer) case.
2. Capture per-call usage data (tool, principal, project, outcome, time) durably, so the
   team can later analyse *which* tools each caller class actually uses and decide
   whether a finer workflow/phase-scoped exposure is worth building.

## Non-goals

- **Workflow/phase-scoped opt-in tool groups.** Deferred until the usage data justifies
  the added agent-interaction contract. This design adds the measurement backend, not
  the phase machine.
- **A new analytics/read tool.** The catalog-reduction goal is undercut by adding tool
  surface; reading/aggregating the usage data is a follow-up. This PR only *captures* it.
- **A security control.** List filtering is an accuracy/UX optimisation, not a boundary.
  Execution-time `require_role` / `require_platform_role` / the destructive-op gate remain
  the only enforcement. Hiding a tool from `list_tools` does not protect it; a caller that
  already knows the name can still call it and is still denied at execution. The filter is
  advisory and must **fail open** (show more, never break discovery), never fail closed.

## Mechanism

### Listing filter

FastMCP 3.4.0 exposes an `on_list_tools` middleware hook
(`MiddlewareContext[ListToolsRequest]` → `Sequence[Tool]`). A new
`ToolExposureMiddleware` wraps it, reads the connection's verified-token
`RequestContext` (via `current_context()`, the same accessor the other middleware use),
and returns only the tools the context may invoke.

`list_tools` is **connection-scoped** but project roles are **per-project**
(`RequestContext.roles: Mapping[project, Role]`). The filter therefore uses the
**union** of the caller's grants and hides a tool only when the caller could invoke it in
**no** granted project — the conservative rule that satisfies "filtering never hides a
tool the caller is otherwise authorized and expected to use":

- A tool requiring project role `R` is shown iff the caller holds role ≥ `R` in at least
  one granted project (`R` ranks `viewer < operator < admin`).
- A tool requiring platform role `P` is shown iff the caller's `platform_roles` satisfy
  `P` (honouring the `platform_admin ⊇ platform_auditor` partial order already in
  `_PLATFORM_IMPLIES`).
- A `PUBLIC` tool (open reads, onboarding) is always shown.

### Authorization map

A central, reviewed classification — `mcp/exposure.py` — assigns each tool an
`ExposureScope`, following the existing `_docmeta.DESTRUCTIVE_TOOLS` idiom (grouped
frozensets + a guard test), **not** per-registration metadata across 71 tools. The
default for an unclassified tool is `PUBLIC` (fail-open: an un-triaged new tool is shown,
never silently hidden). The classification must be **≤** the handler's real requirement:
a too-permissive entry only costs catalog size; a too-restrictive entry hides a usable
tool, which is the one outcome the design forbids.

### Composition with #347

This is the listing filter #347 asked for. RBAC-scoping is the mechanism; there is no
separate "profile" filter to compose — a future workflow/phase filter would chain inside
the same `on_list_tools` seam, after this one, intersecting (never widening) the result.

### Usage-tracking backend

A new append-only `tool_invocation` table (modelled on `platform_audit_log`) records one
row per tool call: `ts`, `principal`, `agent_session`, `project` (nullable — not always
resolvable at the dispatch boundary), `tool`, `outcome`
(`ok` | `error` | `denied`, CHECK-constrained), `actor` (the existing operator-cli /
agent / unknown classification), `client_id` (nullable). A `UsageTrackingMiddleware`
records it best-effort: it runs on its own pool connection, and a recording failure logs a
warning and is swallowed — it never fails or delays the tool call (the
`DenialAuditMiddleware` precedent). It sits just inside `TelemetryMiddleware` so it
observes the final enveloped outcome (after `DenialAuditMiddleware` has converted a
denial into an `authorization_denied` envelope), classifying `denied` from that category
and `error` from any other failure envelope or propagated exception.

`outcome='denied'` is deliberately distinguished because a denied call is direct evidence
the agent reached for a tool it could not use — the exact signal a future exposure
refinement wants.

## Failure modes

- **No verified context in `on_list_tools`** (should not happen under required JWT auth):
  fail open — return the unfiltered catalog and log. Discovery must never break.
- **Filter raises:** fail open — return the unfiltered catalog and log.
- **Recording raises / pool unavailable:** swallow, log a warning; the call result is
  unaffected.
- **Caller with no grants** (no projects, no platform roles): sees only `PUBLIC` tools —
  correct, since it can invoke nothing else.

## Success criteria (falsifiable)

1. A viewer-only single-project token's `list_tools` excludes every platform-scoped and
   project-admin/operator-scoped tool and includes the `PUBLIC` + project-viewer set; the
   returned count is strictly less than the full catalog.
2. A `platform_admin` token granted `admin` on its project sees the **full** catalog —
   identical to today's behaviour (no regression for the privileged path).
3. Every registered tool name has a classification entry (completeness guard), so a newly
   added privileged tool cannot silently leak into a low-privilege catalog.
4. No tool classified as requiring role `R` is hidden from a caller holding ≥ `R`.
5. Each dispatched tool call writes exactly one `tool_invocation` row carrying the correct
   `tool`, `principal`, and `outcome`; a forced recording failure leaves the tool result
   unchanged.

## Guard tests

- `mcp/exposure` unit tests: the union rank rule, platform-role implication, PUBLIC
  always-shown, and the conservative "never hide ≥R" property.
- A completeness test asserting `exposure` classifies every tool `build_app` registers
  (parametrised over the live `app.local_provider._components`, the same accessor
  `_advertise_flat_output_schema` uses).
- A `ToolExposureMiddleware` test driving `on_list_tools` with viewer-only and
  fully-privileged contexts and asserting the filtered counts.
- A migration round-trip test for `0039_tool_invocation.sql` (mirrors existing
  `tests/db` migration coverage) and a `UsageTrackingMiddleware` test: a recorded row per
  outcome class, and best-effort swallow on a failing recorder.

See ADR-0148 for the decision record and rejected alternatives.
