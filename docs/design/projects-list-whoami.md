# `projects.list` — whoami discovery for granted projects

- **Issue:** [#427](https://github.com/randomparity/kdive/issues/427)
- **ADR:** [`../adr/0117-projects-list-whoami.md`](../adr/0117-projects-list-whoami.md)
- **Status:** Draft

## Problem

There is no MCP tool that tells a caller which projects its token grants. Confirmed:
no `projects` registrar in `_PLANE_REGISTRARS` (`src/kdive/mcp/app.py`). An agent
driving the tools must guess project names by trial. The sibling fix (#426) makes
`accounting.report_granted_set` name the caller's *role-bearing* granted set, but a
token whose only membership is role-less still has no discovery surface, and a
caller cannot see its platform roles anywhere.

## Decision summary

See ADR-0117 for the full record. In brief: add a thin, read-only `projects.list`
(whoami) tool that **projects the request context** — no DB, no side effects.

- **Auth:** requires a valid token only (the verifier already gated the transport;
  the handler calls `current_context()` as defence in depth). No platform gate, no
  project gate, no audit — a caller may always read its own grants, and the response
  contains only the caller's own token-derived claims (no cross-tenant data, nothing
  to leak or audit).
- **Response:** a collection envelope (`object_id = "projects"`):
  - one item per granted project, id = the project name, `data = {"project": name,
    "role": <role value or "">}`. A **role-less** membership yields `role: ""` — the
    honest "you are a member but hold no role" signal that #426 deferred here.
  - top-level `data = {"principal": ctx.principal, "platform_roles": [<sorted role
    values>]}`. **Both keys are always present**: `principal` is the non-empty token
    subject, and `platform_roles` is always a JSON list — `[]` (present, not omitted
    or null) for a project-only token — so a client can read it unconditionally.
    `count` is added by `ToolResponse.collection`. (A list value in `data` is allowed
    — see `fixtures.list`, `data={"fixtures": [...]}` — and passes the envelope's
    `validate_json_value` JSON-safety check; the advertised schema stays the flat
    `{"type":"object"}` of ADR-0113.)
  - `suggested_next_actions = ["accounting.report_granted_set"]` so discovery chains
    into usage, as the structured envelope encourages.
- **Determinism / robustness:** items are sorted by project name, and projects are
  deduplicated (`ctx.projects` is not deduplicated upstream — see #426), so the
  response is stable and a duplicated grant yields one item.

## Why a projection, not a DB read

Grants are token-derived: kdive has no DB project table, and `RequestContext` is
built from verified claims (`roles_from_claims` / `platform_roles_from_claims`). So
`projects.list` is a pure function of `current_context()` and needs no pool. The
registrar still takes the pool (the `register(app, pool)` seam) and ignores it.

## Acceptance criteria

- A token granting `{demo: admin}` with platform role `platform_admin` returns
  `status == "ok"`, one item `{"project": "demo", "role": "admin"}`, top-level
  `data.principal` == the subject, and `data.platform_roles == ["platform_admin"]`.
- A **role-less** membership (`projects: ["x"]`, no `roles["x"]`) returns one item
  `{"project": "x", "role": ""}` — the membership is surfaced, not dropped.
- A platform-only token (no projects, has platform roles) returns zero items
  (`count == "0"`), `data.platform_roles` populated, and `data.principal` present.
- A project-only token (no platform roles) returns `data.platform_roles == []` (the
  key is present as an empty list, not omitted or null) and `data.principal` present.
- Items are ordered by project name; a duplicated project in the token yields exactly
  one item.
- No DB connection is opened (the tool is a pure context projection). Token presence
  is the **shared transport-level contract** every tool inherits via `current_context()`
  (the verifier already gated the transport); the handler test drives the unit with an
  injected `RequestContext`, like every other tool test, and does not re-test the
  transport rejection path.
- The tool is fully documented (description + the generated `docs/guide/reference/projects.md`
  entry) and mapped to a behavior test in `test_tool_docs._BEHAVIOR_TESTS_BY_TOOL`.

## Out of scope

- Any change to the viewer floor or to `accounting.report_granted_set` (that is #426).
- A DB-backed project registry (grants remain token-derived).
- Exposing `agent_session` / `client_id` (not part of "what may I touch?"; minimal surface).
