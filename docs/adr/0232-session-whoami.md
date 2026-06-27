# ADR 0232 — Add a read-only `session.whoami` identity probe (#752)

- **Status:** Accepted
- **Date:** 2026-06-23
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0117](0117-projects-list-whoami.md) (the
  granted-projects whoami — unchanged; this is its identity-record sibling),
  [ADR-0006](0006-oidc-rbac-attribution.md) /
  [ADR-0043](0043-platform-scoped-rbac-tier.md) (token-derived project + platform
  grants — unchanged), [ADR-0019](0019-tool-response-envelope.md) (the `ToolResponse`
  envelope), [ADR-0148](0148-rbac-scoped-tool-exposure.md) (per-connection tool
  exposure), [ADR-0170](0170-fielded-tool-output-schema.md) (the fielded envelope
  output schema).
- **Issue:** [#752](https://github.com/randomparity/kdive/issues/752) (part of #746).
- **Spec:** [`../design/session-whoami.md`](../design/session-whoami.md).

## Context

Tool descriptions tell an agent it needs `operator`/`admin`/`platform_admin`, but the
~120-tool surface has no identity or capabilities probe. An agent learns its own
effective role only by attempting a write and catching `authorization_denied` — a
trial side effect on a mutating tool. The identity data is already built server-side
(`RequestContext.principal / projects / roles / platform_roles / client_id`,
`security/authz/context.py`) and serialized for audit (`held_platform_roles`,
`mcp/tools/_platform_auth.py`), but never returned in a `ToolResponse`.

`projects.list` (ADR-0117) already reflects part of the context — granted projects and
their roles, plus `principal` and `platform_roles` in top-level `data` — but as a
per-project collection, and it omits `client_id`. It does not give an agent a single
flat identity record to read in one call.

## Decision

We will add a thin, read-only `session.whoami` tool that **projects
`current_context()`** into one flat success envelope, with no DB access and no side
effects. Top-level `data` carries `principal`, `client_id` (nullable), `projects`
(sorted, de-duplicated list), `roles` (object: role-bearing projects → role value),
and `platform_roles` (sorted list). `object_id` is the principal; `status` is `ok`;
`suggested_next_actions` is `["projects.list"]`.

It requires a valid token (defence in depth via `current_context()`) but **no platform
gate and no project gate**, and emits **no audit row** — the response is the caller's
own token claims, with no cross-tenant data. It is registered in `PUBLIC_TOOLS`
(ADR-0148): callable and visible to any authenticated connection, which admits viewer
and role-less callers.

## Consequences

- An agent can call `session.whoami` and branch on its capabilities without a trial
  write (the issue's acceptance), then chain into `projects.list` for the richer
  per-project view.
- A new public tool on the MCP surface: a new module
  `src/kdive/mcp/tools/identity/session.py`, a registrar entry in `_PLANE_REGISTRARS`
  (`mcp/app.py`), a `PUBLIC_TOOLS` entry (`mcp/exposure.py`), a regenerated
  agent-facing reference (`docs/guide/reference/session.md` + index), and a
  `test_tool_docs._BEHAVIOR_TESTS_BY_TOOL` mapping. No schema, migration, dependency,
  or auth-model change; the advertised `outputSchema` is the app-wide fielded envelope
  (ADR-0170), unchanged.
- Read-only and side-effect-free (no pool connection), so there is nothing to roll back
  beyond removing the tool.
- `session.whoami` and `projects.list` overlap deliberately and both stay — different
  discovery questions ("who am I / full claim set" vs "which projects, what role"). No
  deprecation (the repo's replace-don't-deprecate rule does not apply: neither replaces
  the other).

## Considered & rejected

- **Gate the tool behind the viewer floor (or any role / platform role).** Rejected, on
  the same grounds ADR-0117 rejected it for `projects.list`: a caller must always be
  able to read its *own* identity — that is the entire point of a capability probe — and
  the projection exposes nothing the token does not already assert. A role floor would
  defeat the use case (a role-less or viewer-only agent could not probe before its first
  write) and add an audit obligation for a self-read. `PUBLIC_TOOLS` registration admits
  viewer and role-less callers, which is strictly the acceptance the issue asks for.
- **Extend `projects.list` instead of adding a tool.** Rejected: `projects.list` is a
  per-project collection with a settled contract (ADR-0117); bolting a flat identity
  record and `client_id` onto it would muddy two distinct discovery questions and change
  an accepted envelope shape. A separate single-purpose probe is clearer and cheaper.
- **Return a nested `roles`/`projects` model under a typed output schema.** Rejected:
  the envelope schema is app-wide and flat (ADR-0170); the identity fields ride in
  `data` like every other tool, and the caller reads `structured_content.data`. A
  bespoke per-tool schema is unnecessary and inconsistent with the surface.
- **Compute effective tool capabilities ("which tools may I call").** Rejected as out of
  scope: the probe returns the caller's *claims*; per-tool capability is the existing
  exposure filter (ADR-0148) plus execution-time RBAC. Re-deriving it here would
  duplicate the authority and risk drift.
- **A DB-backed identity/project registry.** Rejected: contrary to the token-derived
  grant model (ADR-0006/0043); there is no project table, and inventing one for a
  self-read would add a write surface and a source-of-truth split.
