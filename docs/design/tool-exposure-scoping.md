# Design: RBAC-scoped tool exposure + usage tracking (#506)

Status: proposed (decided by ADR-0148; moves to accepted when the implementing PR merges)

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

**Prerequisite (verify, do not assume):** `current_context()` reads the request-scoped
access token via FastMCP's `get_access_token()`. That this resolves inside the
`on_list_tools` hook — and not only inside `on_call_tool` — is a load-bearing assumption.
If it returns `None` there, the filter fails open (below) and silently never fires, so the
plan's first task is a spike confirming the token resolves in `on_list_tools`, and the
verification includes a transport-level assertion (success criterion 1), not only an
injected-context unit test.

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

The fail-open default and the completeness guard are a **pair**: at runtime an
unclassified privileged tool is over-advertised (shown to everyone) rather than hidden,
and the completeness guard test — a normal `pytest` test, so it runs under `just test`,
which CI hard-gates individually (the repo runs `just lint/type/test` separately, not via
the `ci` umbrella) — is what forces classification of every new tool. The guards verify
that the filter and the map are *consistent and complete*; they do **not** verify the map
matches the handlers' actual `require_role` calls (ADR-0148 rejected source-introspection),
so map↔enforcement correctness rests on manual review at classification time.

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
records it best-effort: a recording failure logs a warning and is swallowed — it never
fails the tool call (the `DenialAuditMiddleware` precedent). Recording happens *after*
`call_next` returns, when the tool body has already released its own pool connection, so
the recorder's connection does not double-hold against the same call. To keep recording
from delaying response delivery under concurrent load, the recorder acquires its
connection with a **bounded, non-blocking timeout** and drops the row (logged) rather than
waiting when the pool is saturated.

It sits just inside `TelemetryMiddleware` so it observes the final outcome after
`DenialAuditMiddleware` runs. Outcome classification covers every denial path, not only
the enveloped ones:

- `denied` — the returned envelope's `error_category` is `authorization_denied` (the
  `RoleDenied` / `ProjectMembershipDenied` cases `DenialAuditMiddleware` converts), **or**
  the call raised an `AuthorizationError` (its subclasses `DestructiveOpDenied` and the
  base non-member denial *propagate* past `DenialAuditMiddleware` rather than becoming an
  envelope — classifying only on the envelope category would miscount those as `error`).
- `error` — any other failure envelope or propagated exception.
- `ok` — a success envelope.

`outcome='denied'` is deliberately distinguished because a denied call is direct evidence
the agent reached for a tool it could not use — the exact signal a future exposure
refinement wants — so it must capture the destructive-gate and non-member denials too,
not just the enveloped role denial.

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
   returned count is strictly less than the full catalog. Verified **both** at the unit
   level (injected `RequestContext`) **and** end-to-end over the real HTTP transport with a
   minted viewer token — the latter is what proves the token actually resolves in
   `on_list_tools` and the filter fires in production, not just under injection.
2. A `platform_admin` token granted `admin` on its project sees the **full** catalog —
   identical to today's behaviour (no regression for the privileged path).
3. Every registered tool name has a classification entry (completeness guard), so a newly
   added privileged tool cannot silently leak into a low-privilege catalog.
4. No tool classified as requiring role `R` is hidden from a caller holding ≥ `R`.
5. Each dispatched tool call writes exactly one `tool_invocation` row carrying the correct
   `tool`, `principal`, and `outcome`; a denial via a propagated `AuthorizationError`
   records `outcome='denied'` (not `error`); a forced recording failure leaves the tool
   result unchanged.

Out of scope (named follow-ups, not built here): `tool_invocation` retention/aggregation
(the table grows with traffic, polling loops included) and a usage-analytics read tool.

## Guard tests

- `mcp/exposure` unit tests: the union rank rule, platform-role implication, PUBLIC
  always-shown, and the conservative "never hide ≥R" property.
- A completeness test asserting `exposure` classifies every tool `build_app` registers
  (parametrised over the live `app.local_provider._components`, the same accessor
  `_advertise_flat_output_schema` uses).
- A `ToolExposureMiddleware` test driving `on_list_tools` with viewer-only and
  fully-privileged contexts and asserting the filtered counts, plus a fail-open test
  (no/None context → unfiltered catalog).
- A transport-level test (wire harness / `live_stack`) that a minted viewer token's
  `list_tools` over HTTP is reduced — proving the token resolves in `on_list_tools`.
- A migration round-trip test for `0039_tool_invocation.sql` (mirrors existing
  `tests/db` migration coverage) and a `UsageTrackingMiddleware` test: a recorded row per
  outcome class, and best-effort swallow on a failing recorder.

See ADR-0148 for the decision record and rejected alternatives.
