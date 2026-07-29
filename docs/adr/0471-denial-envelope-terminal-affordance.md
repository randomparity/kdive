# ADR 0471 — A denial envelope never names the tool that denied the caller

- **Status:** Accepted
- **Date:** 2026-07-28
- **Issue:** #1596
- **Amends:** [ADR-0019](0019-tool-response-envelope.md)'s `suggested_next_actions` contract for
  the `authorization_denied` category, and applies
  [ADR-0468](0468-wait-as-the-single-point-read.md) §3's "a breadcrumb that re-enters the call
  you were just answered by is a loop" reasoning to the denial path.
- **Relates to:** [ADR-0261](0261-role-filter-success-next-actions.md) /
  [ADR-0421](0421-schema-generated-kdivectl-verbs.md) (`visible_next_actions`),
  [ADR-0123](0123-tool-error-detail-surfacing.md) (suppressed `detail`),
  [ADR-0129](0129-systems-teardown-admin-authority.md) (`missing_checks` in `data`).

## Context

An `authorization_denied` envelope set `suggested_next_actions` to the name of the tool that had
just denied the caller. Observed live during the #1582 cold-start proof, from a
`contributor`-only token calling `ops.diagnostics`:

```json
{"object_id":"diagnostics","status":"error","suggested_next_actions":["ops.diagnostics"],
 "error_category":"authorization_denied","retryable":false,"detail":"access denied"}
```

Every field except `suggested_next_actions` is right. `retryable: false` correctly says a bare
re-invocation cannot help (ADR-0118), and then the breadcrumb tells the agent to re-invoke.
An agent following the navigation contract is steered into a call it cannot complete without a
grant the server cannot issue, so a naive client loops.

The defect was systemic, not local. Enumerating every construction of the category across
`src/kdive/mcp/` found **26** denial sites, of which **21** named their own tool — roughly four
times what #1596 lists. The remaining five already passed no actions, so the surface was also
*inconsistent*: an identical denial condition produced `[]` in `jobs.cancel` and `["ops.diagnostics"]`
in `ops.diagnostics`, for no reason either envelope discloses.

Two mechanisms already exist nearby and neither covers this:

- `visible_next_actions` (ADR-0261, ADR-0421) drops breadcrumbs the caller cannot invoke, but it
  filters on the **project** axis via `project_tool_visible`, and no denial path calls it. It
  also **raises `ValueError`** on an unregistered name, so it cannot be dropped into a new path
  without care.
- `tests/mcp/core/test_next_actions_graph.py` (ADR-0407) guards the *doc-encoded* golden path and
  explicitly places runtime `suggested_next_actions` out of its own scope.

## Decision

### 1. `session.whoami` is the terminal affordance, uniformly

A denial's breadcrumb is the fixed one-element list `("session.whoami",)`
(`DENIAL_NEXT_ACTIONS` in `kdive/mcp/responses.py`), at every denial site on the surface.

The alternative was an empty list — a clean dead end, and the shape ADR-0468 §3 chose for a
terminal job. It was rejected here because a denial is not a terminal job. A terminal job
envelope carries the answer; a denial carries nothing the agent can act on. ADR-0123 rewrites
every `authorization_denied` `detail` to a bare `"access denied"` as a no-leak seam, and that
seam is deliberately untouched by this ADR (see *Deferred*, below). With an empty list the agent
is left holding a failure it cannot diagnose, cannot escalate precisely, and cannot even
characterize — which is half of what #1596 reports.

`session.whoami` is the one step that is always available and always informative:

- it is in `PUBLIC_TOOLS`, so it is invokable by any authenticated caller and can itself never
  deny — the breadcrumb cannot fail the same way the denied call did;
- it is in `CORE_TOOLS`, so it stays listed even under the default-on gateway profile
  (ADR-0456), i.e. it is reachable without a `tools.search` round trip;
- it names the grants the caller actually holds, which is the fact an agent needs in order to
  report a precise blocker to its operator ("I hold contributor on proj-a and no platform role");
- it is not the denied tool and does not itself suggest one, so it terminates rather than loops.

Choosing a *public* breadcrumb is what makes the invariant provable rather than merely observed:
only a **gated** tool can raise an authorization denial, so a breadcrumb drawn exclusively from
`PUBLIC_TOOLS` is disjoint from the set of tools that could possibly have produced the denial.

### 2. `ToolResponse.denied` is the single constructor, and it accepts no actions

All 26 sites now call `ToolResponse.denied(object_id, data=...)`. It takes **no**
`suggested_next_actions` parameter at all. A denial that names its own tool is therefore
unrepresentable, not merely discouraged — a per-site fix would have been 21 mechanical edits
with nothing stopping the 22nd site from drifting back.

`data` is retained because ADR-0129's destructive-op gate surfaces its closed enum of failed
policy-check tokens (`admin_role`, `operator_role`, `profile_opt_in`) there; ADR-0123 suppresses
`detail`, not `data`, so that channel is unaffected.

### 3. `visible_next_actions` is *not* extended to the platform axis

#1596 proposes routing denials through the existing filter, extended to cover platform roles.
Rejected, on the evidence: with a breadcrumb that is public by construction (decision 1), the
filter is provably a no-op — `required_scopes("session.whoami")` is empty and
`project_tool_visible` returns `True` unconditionally for it. Extending the filter and threading
a `RequestContext` (and a `project`, which several platform-scoped denial sites do not have)
through 26 call sites to compute a fixed answer would be machinery that can never change an
outcome. The platform-axis predicate `tool_visible` already exists at `exposure.py` for the
listing path and is left as it is.

If a future decision gives denials a *gated* breadcrumb, that filter becomes load-bearing and
should be added then, with the ctx threading its own cost justifies.

### 4. The invariant is guarded in two independent layers

`tests/guards/test_denial_envelope_actions.py`:

1. **Source.** An AST walk over `src/kdive/` — the whole package, not just `mcp/`, so a denial
   built in a service or provider module is caught too — fails on any call passing
   `ErrorCategory.AUTHORIZATION_DENIED` as an argument, with `mcp/responses.py` as the single
   allowlisted definition site. A new tool hand-rolling
   `ToolResponse.failure(obj, ErrorCategory.AUTHORIZATION_DENIED, suggested_next_actions=[_TOOL])`
   trips it at the moment it is written.
2. **Registry.** Every name in `DENIAL_NEXT_ACTIONS` must be live *and* in `PUBLIC_TOOLS`, and a
   constructed denial envelope must name nothing in `CLASSIFIED_TOOLS`. This is the layer that
   would catch the constant being repointed at a gated tool — a change the AST walk cannot see.

Neither layer alone is sufficient: layer 1 checks the shape of the source and would stay green if
the constant itself went wrong; layer 2 checks the constant and would stay green if a new tool
bypassed the helper.

## Consequences

- **21 denial envelopes stop self-suggesting; 5 more gain a breadcrumb** where they previously
  returned `[]`. The whole `authorization_denied` surface is now uniform.
- **A client asserting `suggested_next_actions == [<the tool>]` on a denial breaks.** This is the
  reported defect, so the break is the fix; ~30 test assertions across the suite moved with it.
- **No migration and no schema change.** `suggested_next_actions` was already `list[str]`; only
  its contents move. Historical `platform_audit_log` / `tool_call_trail` rows are untouched — they
  record what was called, not what was suggested.
- **`detail` is unchanged.** A denied agent still reads `"access denied"` and still cannot tell
  *which* grant is missing. `session.whoami` narrows that gap from the other side (what the caller
  *has*) without disclosing what the tool *requires*.
- **New denial paths cost one line.** `ToolResponse.denied(object_id)` — and the guard makes the
  wrong version fail rather than ship.
- **Two of the 26 sites are unreachable today, and this ADR does not claim otherwise.** #1635
  establishes that FastMCP wraps a handler exception in `ToolError` *inside* the branch the
  middleware chain wraps, so `DenialAuditMiddleware`'s `except RoleDenied` and its
  `except ProjectMembershipDenied` are both dead on the real dispatch path — an envelope that is
  never emitted cannot suggest anything, self-referential or not. Nothing decided here rests on
  those two: the other 24 sites envelope their denial *inside the handler* and demonstrably reach
  the client (the #1582 observation quoted in *Context* is `ops.diagnostics`'s own handler-level
  `_denied()`, not the middleware), and decision 4's source guard is a static property of the tree
  that holds regardless of reachability. The interaction runs the other way: when #1635 restores
  that boundary, both paths will already emit the uniform envelope instead of needing a second
  pass. Fixing #1635 is out of scope here, and this ADR takes no position on how that boundary
  should unwrap.

## Deferred

Naming the missing grant in `detail` is **out of scope** and tracked separately. Doing it would
overturn ADR-0123's `_SUPPRESSED_DETAIL` seam, which deliberately rewrites *every*
`authorization_denied` detail to a fixed constant so no raise site can leak resource existence or
gating structure through the envelope. That is a security-posture decision about disclosing which
platform role gates which tool, and it deserves its own decision record rather than a ride-along
in a navigation bugfix. `src/kdive/domain/errors.py` is unmodified by this ADR.

## Alternatives considered

- **Empty `suggested_next_actions`.** Rejected per decision 1: combined with ADR-0123's suppressed
  `detail`, it leaves a denied agent with literally nothing to act on.
- **Point at the tool's own read-only sibling** (e.g. `ops.diagnostics` → `ops.jobs_list`).
  Rejected: both are gated by the same platform role that just denied the caller, so it
  reproduces the defect one tool over.
- **Point at `projects.list`.** Rejected: it is public and non-looping, but it answers a question
  the caller did not ask; `session.whoami` reports the caller's own grants, including the platform
  roles that gate most of these tools, which `projects.list` does not.
- **Fix the 21 sites in place, keeping `suggested_next_actions` on the failure constructor.**
  Rejected: it is the version that silently drifts back, and it leaves the five already-empty
  sites inconsistent with the rest.
- **Extend `visible_next_actions` to the platform axis and route denials through it.** Rejected
  per decision 3 — with a public breadcrumb it is a provable no-op.
