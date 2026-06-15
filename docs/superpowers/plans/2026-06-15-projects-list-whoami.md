# Plan — `projects.list` (whoami) discovery tool (#427)

- **Spec:** [`../../design/projects-list-whoami.md`](../../design/projects-list-whoami.md)
- **ADR:** [`../../adr/0117-projects-list-whoami.md`](../../adr/0117-projects-list-whoami.md)
- **Branch:** `feat/projects-list-whoami-427`
- **Execution mode:** direct in this session (one small new module + registrar wiring +
  generated docs; tightly coupled, not worth subagent fan-out).

## Guardrails (run before every commit)

- `just lint` (ruff check + format check)
- `just type` (ty)
- `uv run pytest -q tests/mcp/catalog/test_projects_tools.py tests/mcp/core/test_tool_docs.py`
- `just docs` (regenerate the tool reference) then `just docs-check` (CI gate) + `just docs-links`
- Full local gate before push: `just ci`

## Conventions

- TDD: failing test first, confirm the failure reason, minimal impl, refactor green.
- Mirror `src/kdive/mcp/tools/catalog/fixtures.py` (the closest analog: a plain
  authenticated read with no platform gate, no audit; registrar `register(app, pool)`).
- Drive the handler with an injected `RequestContext` (the repo unit contract).
- 100-char lines, Google-style docstrings, absolute imports, no relative paths.
- Tool name `projects.list`; module `src/kdive/mcp/tools/catalog/projects.py`.

## Task 1 — The `projects.list` tool module

**Where it fits:** the discovery primitive — a pure projection of `current_context()`.

**Files:** `src/kdive/mcp/tools/catalog/projects.py` (new) +
`tests/mcp/catalog/test_projects_tools.py` (new).

**Steps (TDD):**
1. **Failing tests first** (mirror the spec acceptance criteria), driving a handler
   function `whoami(ctx)` directly with an injected `RequestContext`:
   - role-bearing: `{demo: admin}` + `platform_admin` → one item
     `{"project":"demo","role":"admin"}`, `data.principal == subject`,
     `data.platform_roles == ["platform_admin"]`, `status=="ok"`.
   - role-less: `projects:("x",)`, `roles={}` → one item `{"project":"x","role":""}`.
   - platform-only: `projects:()`, platform roles set → zero items, `count=="0"`,
     `data.platform_roles` populated, `data.principal` present.
   - project-only: `roles={demo: viewer}`, no platform roles → `data.platform_roles == []`
     (present, empty list — not absent/None).
   - ordering + dedup: `projects:("c","a","a","b")` with roles → items `[a,b,c]` (sorted,
     one `a`).
   - the `platform_roles` value is a JSON list (serialize the envelope and assert it is
     a `list`, confirming list-valued `data` is JSON-safe).
2. Run the tests; confirm they fail because the module/handler does not exist.
3. **Implement** `whoami(ctx: RequestContext) -> ToolResponse`:
   - dedupe + sort: `for project in sorted(set(ctx.projects))`.
   - per-project item: look the role up with an explicit `None` check (clearer than a
     truthiness test and immune to any future falsy `Role` member):
     `role = ctx.roles.get(project)`; `"role": role.value if role is not None else ""`.
   - top-level: `ToolResponse.collection("projects", "ok", items, data={"principal":
     ctx.principal, "platform_roles": sorted(r.value for r in ctx.platform_roles)},
     suggested_next_actions=["accounting.report_granted_set"])`.
   - `register(app, pool)` registers `projects.list` with `_docmeta.read_only()` and
     `meta={"maturity": "implemented"}`; the inner `@app.tool` async fn calls
     `whoami(current_context())`. `pool` is accepted (the seam) and unused — name it
     `_pool` to satisfy lint.
4. Focused tests + `just lint` + `just type` green.

**Acceptance:** every spec acceptance criterion holds; no DB connection opened.

## Task 2 — Register the plane

**Where it fits:** expose the tool on the live MCP surface.

**Files:** `src/kdive/mcp/app.py`.

**Steps:**
1. Add `projects` to the `from kdive.mcp.tools.catalog import (...)` block (keep the
   list sorted as it is now).
2. Add `_pool_only_plane_registrar(projects.register)` to `_PLANE_REGISTRARS`
   (near the other catalog registrars).
3. `just type` + `just lint`.

**Acceptance:** `build_app` registers `projects.list`; `app.list_tools()` includes it.

## Task 3 — Documentation guard + generated reference

**Where it fits:** the ADR-0047 doc guard requires every tool be documented and mapped
to a behavior test; the agent-facing reference is generated from the live registry.

**Files:** `tests/mcp/core/test_tool_docs.py`, `docs/guide/reference/projects.md` (new,
generated), `docs/guide/reference/index.md` (generated).

**Steps:**
1. Add `"projects.list": ("tests/mcp/catalog/test_projects_tools.py",)` to
   `_BEHAVIOR_TESTS_BY_TOOL`.
2. **Description source (decided):** the generated reference takes the tool's
   description from the `@app.tool` docstring unless `_TOOL_DESCRIPTION_OVERRIDES`
   has an entry (`fixtures.list` relies on its docstring). Write the inner tool
   docstring as a single clean line with no `|` or newline (the generator raises on a
   table-breaking character), e.g. *"List the projects the caller's token grants, with
   each project's role and the caller's platform roles (whoami)."* Do **not** add a
   `_TOOL_DESCRIPTION_OVERRIDES` entry — the single-line docstring is the description,
   keeping one source of truth (matches `fixtures.list`).
3. Run `just docs` to regenerate the reference (creates `projects.md`, updates
   `index.md`). Review the generated entry matches that one-liner (no params table:
   `projects.list` takes no arguments).
4. `uv run pytest -q tests/mcp/core/test_tool_docs.py` (every-tool-documented +
   behavior-test-mapping guards) and `just docs-check` (generated reference matches).

**Acceptance:** `test_tool_docs` passes (description present, behavior-test mapped);
`just docs-check` clean.

## Task 4 — Guardrails + branch review

1. Full `just ci`.
2. Confirm m2-gate is **not** touched: the M2 portability gate is dormant (not in CI;
   #426 merged green while touching an unallowlisted core file), and a post-M2 feature
   tool is not an M2 portability exception — do not add it to `ALLOWED_FILES` or the
   gate meta-test.
3. Adversarial-review the branch diff (`/challenge --base main`); address findings.

## Rollback / cleanup

- Purely additive: one new read-only tool + registration + generated docs + tests.
  Revert is a single `git revert` of the feature commits. No migration, no schema, no
  data, no external-service change, no runtime load (no pool use).

## Out of scope

- The viewer floor / `accounting.report_granted_set` (#426).
- A DB-backed project registry; exposing `agent_session`/`client_id`.
