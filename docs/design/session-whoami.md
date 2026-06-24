# `session.whoami` — read-only identity / capability probe

- **Issue:** [#752](https://github.com/randomparity/kdive/issues/752)
- **ADR:** [`../adr/0232-session-whoami.md`](../adr/0232-session-whoami.md)
- **Status:** Draft
- **Sibling:** [ADR-0117](../adr/0117-projects-list-whoami.md) /
  [`projects-list-whoami.md`](projects-list-whoami.md) (the granted-projects whoami).

## Problem

Many tool descriptions say "Requires operator/admin/platform_admin," but the surface
has no identity/capabilities probe. An agent learns its own effective role only by
attempting a write and catching `authorization_denied`. That is a trial-and-error
side effect on a mutating tool, which is exactly what a capability probe exists to
avoid.

The data is already built server-side and never returned to the caller:
`RequestContext` carries `principal`, `projects`, `roles`, `platform_roles`, and
`client_id` (`security/authz/context.py`); `held_platform_roles(ctx)`
(`mcp/tools/_platform_auth.py`) already serializes the platform set — but every call
site feeds it into an audit record, never into a `ToolResponse`.

`projects.list` (ADR-0117) already projects part of this — the granted projects and
their roles, plus `principal` and `platform_roles` in top-level `data`. It does **not**
return `client_id`, and it is shaped as a per-project collection, not a single flat
identity record an agent can read in one shot to branch on its own capabilities.

## Decision summary

See ADR-0232 for the full record. In brief: add a thin, read-only `session.whoami`
tool that **projects the request context** into one flat success envelope — no DB, no
side effects, no new data plumbing.

- **Auth:** requires a valid token only. The verifier already gated the transport; the
  handler calls `current_context()` as defence in depth (it raises if no context is
  bound). **No platform gate, no project gate, no audit** — the same profile as
  `projects.list`. A caller may always read its own identity; the response contains
  only the caller's own token-derived claims (no cross-tenant data, nothing to leak or
  audit). This is the settled choice from ADR-0117: gating a self-claims whoami behind
  a role floor would defeat the probe — a role-less or viewer-only agent must be able
  to discover its own (lack of) capabilities without a trial write.

  Because the tool requires no role, it is registered in `PUBLIC_TOOLS` (visible to any
  authenticated connection). This *admits* viewer and role-less callers, which is the
  acceptance the issue requires; it is strictly more permissive than viewer-gated and
  hides nothing a viewer could call.

- **Response:** a single success envelope (`object_id = ctx.principal`,
  `status = "ok"`) with top-level `data`:
  - `principal` — the token subject (non-empty string).
  - `client_id` — the token's `azp`/`client_id`, or `null` when the claim is absent
    (`RequestContext.client_id` is already `str | None`).
  - `projects` — the sorted, de-duplicated list of granted project names (a JSON list
    of strings; `[]` for a token with no project membership).
  - `roles` — an object mapping each **role-bearing** project to its role value
    (`{"proj-a": "admin"}`); `{}` when the caller holds a role on no project. A
    role-less membership appears in `projects` but not in `roles` — the same honest
    "member but no role" distinction `projects.list` draws.
  - `platform_roles` — the sorted list of platform role values (a JSON list;
    `[]`, present-not-null, for a project-only token).
  - `suggested_next_actions = ["projects.list"]` so the probe chains into the richer
    per-project view.

  `RequestContext` also carries `agent_session` (`str | None`); it is **intentionally
  not returned**. It is a session-correlation token for audit attribution, not an
  identity or capability claim, and the issue's field list omits it. Returning it would
  widen the probe's surface beyond its purpose.

- **Determinism / robustness:** `projects` is sorted and de-duplicated (`ctx.projects`
  is not de-duplicated upstream); `platform_roles` is sorted by value; `roles` is
  rendered from `ctx.roles` (already a per-project map). All values are JSON-safe and
  pass the envelope's `validate_json_value` check. The advertised `outputSchema` is the
  app-wide fielded envelope schema (ADR-0170) — every `ToolResponse` tool advertises
  the same envelope; the caller reads the identity fields from `structured_content.data`.

## Why this is not redundant with `projects.list`

`projects.list` answers "which projects may I touch, and with what role?" as a
collection. `session.whoami` answers "who am I, and what is my full claim set?" as one
flat record — adding `client_id` (absent from `projects.list`) and a single-call shape
an agent reads to branch before deciding whether a write is even worth attempting. The
two share the underlying context projection but serve different discovery questions;
both are kept (no deprecation), and `session.whoami` names `projects.list` as its
next action.

## Acceptance criteria (falsifiable)

1. Calling `session.whoami` with a synthetic `RequestContext` returns a success
   envelope whose `data` carries `principal`, `client_id`, `projects`, `roles`, and
   `platform_roles` equal to that context's claims.
2. `projects` is sorted + de-duplicated; `platform_roles` is sorted; `roles` contains
   only role-bearing projects.
3. A viewer-only caller (role `viewer` on its project, no platform role) is **admitted**
   — the tool returns its identity, it is not denied, and the tool is exposed to that
   connection (in `PUBLIC_TOOLS`).
4. An empty/minimal context (a token subject only: no projects, no roles, no platform
   roles, no `client_id`) returns `projects: []`, `roles: {}`, `platform_roles: []`,
   `client_id: null` — every key present, none omitted.
5. The tool is read-only (`readOnlyHint`), `implemented` maturity, opens no pool
   connection, and writes no audit row.

## Out of scope

- No DB project registry (contrary to the token-derived grant model, ADR-0006/0043).
- No change to `projects.list`, the auth model, the envelope schema, or any migration.
- No new effective-capability computation (e.g. "which tools may I call"): the probe
  returns the caller's *claims*; tool-level capability is the existing per-tool gate.
