# Plan — `session.whoami` identity probe (#752)

- **Spec:** [`../../design/session-whoami.md`](../../design/session-whoami.md)
- **ADR:** [`../../adr/0232-session-whoami.md`](../../adr/0232-session-whoami.md)
- **Branch:** `feat/752-session-whoami` (worktree
  `/home/dave/src/kdive-worktrees/issue-752`)

This is one cohesive, tightly-coupled change (a new public read-only tool plus the
registrations a new tool requires). It is implemented directly in this session
(no subagent fan-out): the tasks below are sequential and share the same files.

## Guardrails (run before every commit)

- `just lint` — `ruff check` + `ruff format --check`
- `just type` — `ty check` (whole tree, src + tests)
- focused tests: `uv run python -m pytest tests/mcp/catalog/test_session_tools.py tests/mcp/core/test_app.py tests/mcp/core/test_exposure.py tests/mcp/core/test_tool_docs.py -q`
- `just docs-check` after `just docs` (the tool reference is generated + CI-gated)
- full suite once before first push: `just test`

## Conventions

- Absolute imports only; Google-style docstrings on public APIs; ≤100 lines/function;
  line length 100; plain factual prose (no "robust"/"comprehensive").
- Tool returns a `ToolResponse` (`mcp/responses.py`); the app-wide fielded outputSchema
  (ADR-0170) is applied by the build_app sweep — no per-tool schema.
- "New tool = registrations": registrar entry in `_PLANE_REGISTRARS` (`mcp/app.py`),
  `PUBLIC_TOOLS` entry (`mcp/exposure.py`), `_BEHAVIOR_TESTS_BY_TOOL` mapping
  (`tests/mcp/core/test_tool_docs.py`), regenerated `docs/guide/reference/`.

## Task 1 — Failing tests for the handler projection (TDD red)

**Where it fits:** AC#1, #2, #4 of the spec — the pure `whoami(ctx)` projection.

**Files:** new `tests/mcp/catalog/test_session_tools.py`.

Write tests (no implementation yet) against a `whoami(ctx)` function in
`kdive.mcp.tools.catalog.session`:

1. Full context (principal, client_id, two projects with roles, one role-less project,
   two platform roles) → success envelope; `object_id == principal`, `status == "ok"`;
   `data["principal"]`, `data["client_id"]` match; `data["projects"]` is the sorted
   de-duplicated union (role-bearing + role-less); `data["roles"]` maps only role-bearing
   projects to role values; `data["platform_roles"]` is the sorted platform values list;
   `suggested_next_actions == ["projects.list"]`.
2. Duplicate project entries in `ctx.projects` → `data["projects"]` de-duplicated.
3. Empty/minimal context (subject only) → `projects == []`, `roles == {}`,
   `platform_roles == []`, `client_id is None`; every key present.
4. `agent_session` is **not** present anywhere in `data`.

Build `RequestContext` directly (frozen dataclass) with `Role`/`PlatformRole` enums.

**Acceptance:** tests run and fail with ImportError/AttributeError (no handler yet).

**Run:** `uv run python -m pytest tests/mcp/catalog/test_session_tools.py -q` → red.

## Task 2 — Implement the handler + registrar (TDD green)

**Where it fits:** the tool module itself.

**Files:** new `src/kdive/mcp/tools/catalog/session.py`.

Mirror `catalog/projects.py`:

- `whoami(ctx: RequestContext) -> ToolResponse` — pure projection. Build
  `projects = sorted(set(ctx.projects))`; `roles = {p: r.value for p, r in
  sorted(ctx.roles.items())}` (role-bearing only — `ctx.roles` already holds only
  role-bearing entries); `platform_roles = sorted(r.value for r in ctx.platform_roles)`.
  Return `ToolResponse.success(ctx.principal, "ok",
  suggested_next_actions=["projects.list"], data={"principal": ctx.principal,
  "client_id": ctx.client_id, "projects": projects, "roles": roles,
  "platform_roles": platform_roles})`.
- `register(app, _pool)` — `@app.tool(name="session.whoami",
  annotations=_docmeta.read_only(), meta={"maturity": "implemented"})` wrapping
  `current_context()` → `whoami`. Module + handler docstrings cite ADR-0232.

**Acceptance:** Task 1 tests pass.

**Run:** focused tests green; `just lint`; `just type`.

## Task 3 — Wire the registrar and exposure

**Where it fits:** make the tool live on the registry and visible to viewers.

**Files:** `src/kdive/mcp/app.py`, `src/kdive/mcp/exposure.py`.

- `app.py`: import the new module; add `_pool_only_plane_registrar(session.register)`
  to `_PLANE_REGISTRARS` (it needs no provider assembly), before the last
  `_register_lifecycle_prompts` entry.
- `exposure.py`: add `"session.whoami"` to `PUBLIC_TOOLS` (it requires no role, like
  `projects.list`). The completeness guard (`test_app.py`) then passes.

**Acceptance:** `test_app.py` (CLASSIFIED|PUBLIC == registry) and `test_exposure.py`
pass; `session.whoami` appears in the live tool list and is exposed to a viewer-only
context.

**Run:** `uv run python -m pytest tests/mcp/core/test_app.py tests/mcp/core/test_exposure.py -q`.

## Task 4 — Exposure + behavior-map tests and generated docs

**Where it fits:** AC#3 (viewer admission) + the doc-completeness gates.

**Files:** `tests/mcp/core/test_tool_docs.py` (map entry), regenerated
`docs/guide/reference/session.md` + `docs/guide/reference/index.md`, and an exposure
admission test (in `test_session_tools.py` or `test_exposure.py`).

- Add `"session.whoami": ("tests/mcp/catalog/test_session_tools.py",)` to
  `_BEHAVIOR_TESTS_BY_TOOL` so `test_active_tools_have_a_covering_test` passes.
- Add a test asserting `required_scopes("session.whoami") == frozenset()` (public) and
  that a viewer-only connection sees the tool via the exposure filter — AC#3.
- Run `just docs` to generate `docs/guide/reference/session.md` and update the index;
  review the diff; `just docs-check` must pass.

**Acceptance:** `test_tool_docs.py` passes; `just docs-check` clean.

**Run:** `uv run python -m pytest tests/mcp/core/test_tool_docs.py -q`; `just docs-check`.

## Task 5 — Full guardrails + branch review

Run `just lint`, `just type`, full `just test`. Then the branch adversarial-review loop
(`--base main`) and `security-review` (the tool exposes caller identity — confirm no
over-disclosure beyond the caller's own claims). Address findings, commit per fix.

## Rollback / cleanup

Pure additive, no migration, no persistence. Rollback = revert the commits / drop the
module + its four registration edits. The generated `docs/guide/reference/session.md`
and index row are regenerated artifacts, removed by re-running `just docs` after the
revert.
