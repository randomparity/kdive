# ADR 0117 — Add a read-only `projects.list` (whoami) discovery tool (#427)

- **Status:** Accepted
- **Date:** 2026-06-15
- **Deciders:** kdive maintainers
- **Builds on (does not supersede):** [ADR-0006](0006-oidc-rbac-attribution.md) /
  [ADR-0043](0043-platform-scoped-rbac-tier.md) (token-derived project + platform
  grants — unchanged), [ADR-0019](0019-tool-response-envelope.md) (the `ToolResponse`
  envelope), [ADR-0113](0113-flat-tool-output-schema.md) (flat advertised schema).
- **Issue:** [#427](https://github.com/randomparity/kdive/issues/427).
- **Spec:** [`../design/projects-list-whoami.md`](../design/projects-list-whoami.md).
- **Sibling:** [ADR-0116](0116-granted-set-project-naming.md) (#426 granted-set naming).

## Context

No MCP tool reports which projects a caller's token grants (no `projects` registrar in
`_PLANE_REGISTRARS`). An agent must guess project names by trial. Grants are
token-derived — kdive has no DB project table; `RequestContext` is built from verified
claims (`roles_from_claims` / `platform_roles_from_claims`). The #426 granted-set fix
names the caller's *role-bearing* projects in a usage report, but a role-less
membership has no discovery surface and platform roles are not reported anywhere.

## Decision

We will add a thin, read-only `projects.list` (whoami) tool that **projects
`current_context()`** with no DB access and no side effects. It returns a collection
envelope: one item per granted project (`{"project", "role"}`, with `role: ""` for a
role-less membership) plus top-level `{"principal", "platform_roles"}`. Items are
sorted by project name and deduplicated. It requires a valid token (defence in depth
via `current_context()`) but no platform or project gate and emits no audit row — the
response is the caller's own token claims, with no cross-tenant data.

## Consequences

- An agent can discover "what may I touch?" in one call and chain into
  `accounting.report_granted_set` (named in `suggested_next_actions`). Role-less
  membership — invisible in the granted-set usage report by design (ADR-0116) —
  becomes visible here.
- A new public tool on the MCP surface: a new module
  `src/kdive/mcp/tools/identity/projects.py`, a registrar entry in `_PLANE_REGISTRARS`,
  a regenerated agent-facing reference (`docs/guide/reference/projects.md` + index),
  and a `test_tool_docs._BEHAVIOR_TESTS_BY_TOOL` mapping. No schema, migration,
  dependency, or auth-model change; the advertised `outputSchema` stays the flat
  `{"type":"object"}` (ADR-0113).
- The tool is read-only and side-effect-free, so there is nothing to roll back beyond
  removing the tool; it adds no load (no pool connection).

## Alternatives considered

- **A single success envelope with nested `projects`/`roles` arrays instead of items.**
  Rejected: the codebase's idiom for a `*.list` read is a collection of items (e.g.
  `accounting.report_granted_set`, `fixtures.list`), and per-project items let a client
  key by project. The whoami summary (principal, platform roles) still rides in
  top-level `data`.
- **Gate the tool behind the viewer floor / a platform role.** Rejected: a caller must
  always be able to read its *own* grants — that is the entire point of a discovery
  primitive — and the projection exposes nothing the token does not already assert. A
  gate would defeat the use case and add an audit obligation for a self-read.
- **A DB-backed project registry.** Rejected as out of scope and contrary to the
  token-derived grant model (ADR-0006/0043): there is no project table, and inventing
  one for discovery would add a write surface and a source-of-truth split.
- **Omit role-less membership (mirror the granted-set's role-bearing set).** Rejected:
  surfacing role-less membership is the specific gap #426 left to this tool; hiding it
  would leave the same discovery hole the issue exists to close.
