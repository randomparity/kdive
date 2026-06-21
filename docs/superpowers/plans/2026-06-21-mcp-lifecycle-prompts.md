# Plan — MCP prompts surface for canonical lifecycle workflows (#624)

Derived from `docs/design/2026-06-21-mcp-lifecycle-prompts.md` and
[ADR-0202](../../adr/0202-mcp-lifecycle-prompts.md). Single feature branch
`feat/mcp-lifecycle-prompts-624`; implemented in this session with TDD.

## Where this fits

`build_app()` (`src/kdive/mcp/app.py`) registers tools and doc resources but no MCP
prompts. This adds a third registrar plane (`mcp/prompts/registrar.py`) that registers
three canonical lifecycle prompts as thin pointers into the real tools, tagging any
`partial` step with its maturity reason so an agent is never silently steered into a
not-yet-proven tool. Mirrors the doc-resources registrar (ADR-0151).

## Guardrail commands (run before every commit)

- `just lint` — ruff check + format check
- `just type` — ty over src + tests (whole tree)
- focused tests: `uv run python -m pytest tests/mcp/prompts -q`
- `just test` — full suite (before the first push)
- `just adr-status-check`, `just check-mermaid`, `just docs-links`, `just docs-paths`
  — doc gates (already green for the spec/ADR commit)

CI hard-gates `lint`, `type`, `lint-shell`, `lint-workflows`, `check-mermaid`, `test`
individually (not only via `just ci`).

## Conventions to honor

- Absolute imports only (`from kdive.mcp.prompts...`), no relative.
- Google-style docstrings on the public `register` and the dataclasses.
- ≤100 lines/function, line length 100, lint set `E,F,I,UP,B,SIM`.
- Cite ADR-0202 in the new module docstring (the adr-status-check gate then requires
  ADR-0202 to be Accepted — it already is).
- Doc-style: plain factual prose; no "robust/comprehensive/critical/elegant".
- Tests mirror the package tree: `tests/mcp/prompts/test_lifecycle_prompts.py`.

## Task 1 — Pure prompts registrar with maturity-aware rendering (TDD)

**Files:**
- new `src/kdive/mcp/prompts/__init__.py` (empty package marker)
- new `src/kdive/mcp/prompts/registrar.py`
- new `tests/mcp/prompts/__init__.py`
- new `tests/mcp/prompts/test_lifecycle_prompts.py`

**Module shape (`registrar.py`):**
- `@dataclass(frozen=True, slots=True) ToolMaturity` — `maturity: str`, `reason: str | None`.
- `@dataclass(frozen=True, slots=True) Step` — `tool: str`, `purpose: str`.
- `@dataclass(frozen=True, slots=True) PromptSpec` — `name`, `title`, `description`,
  `summary`, `steps: tuple[Step, ...]`.
- `CANONICAL_PROMPTS: tuple[PromptSpec, ...]` — the three journeys, exact tool names and
  one-line purposes from the spec's "Journey content" section, including the precondition
  line woven into each downstream prompt's `summary`.
- `_render_body(spec: PromptSpec, tool_maturity: Mapping[str, ToolMaturity]) -> str` —
  builds the markdown body: summary line, numbered tool sequence (each `partial` step
  suffixed `  [partial: <reason>]`, falling back to `[partial]` when reason is None), and
  the fixed Notes block. Validation happens here per step:
  - tool absent from `tool_maturity` → `RuntimeError` naming the prompt and tool;
  - referenced tool `planned` → `RuntimeError` naming the prompt and tool.
- `register(app: FastMCP, *, tool_maturity: Mapping[str, ToolMaturity]) -> int` — for
  each spec, render the body, build a no-arg closure returning that body, register a
  `FunctionPrompt` via `Prompt.from_function(fn, name=..., title=..., description=...)`
  and `app.add_prompt(...)`. Returns the count. Use a factory to bind each body so the
  three closures do not share a late-bound variable.

**TDD order (write the test first, watch it fail, then implement):**
1. `test_render_tags_partial_steps_with_reason` — `_render_body` on a spec with a known
   partial step and a fabricated maturity map renders `[partial: <reason>]` on that step
   and no tag on an implemented step. (RED: module/function absent.)
2. `test_render_partial_without_reason_falls_back_to_bare_tag`.
3. `test_unknown_tool_raises` — a `PromptSpec` step naming a tool absent from the map →
   `RuntimeError` matching the tool name.
4. `test_planned_tool_raises` — map marks a referenced tool `planned` → `RuntimeError`.
5. `test_register_returns_count_and_lists_three_prompts` — register against a bare
   `FastMCP("probe")` with a fabricated map covering every referenced tool; assert
   `register(...) == len(CANONICAL_PROMPTS) == 3` and the three names are listed via
   `await app._list_prompts()`.
6. `test_each_prompt_renders_nonempty_body_naming_every_step_tool` — for each spec,
   `await app._get_prompt(name, {})` then `.render({})`; assert the rendered user message
   text contains every `step.tool` of that spec.

**Acceptance:** all six tests pass; `register` is pure (no FastMCP-internals access);
`uv run python -m pytest tests/mcp/prompts -q` green; `just lint` + `just type` green.

**Rollback:** the module and its tests are additive; deleting `src/kdive/mcp/prompts/`
and the test package removes the feature with no other change.

## Task 2 — Wire the registrar into build_app (TDD)

**Files:** `src/kdive/mcp/app.py`, `tests/mcp/core/test_app.py`.

**Changes:**
- Add `_registered_tools(app: FastMCP) -> Iterator[Tool]` yielding each `Tool` in
  `app.local_provider._components`. Refactor `_advertise_envelope_output_schema` to use
  it (behavior-preserving; the existing zero-tools `RuntimeError` stays).
- Add `_register_lifecycle_prompts(app, pool, assembly)` matching `PlaneRegistrar`:
  build `tool_maturity: dict[str, ToolMaturity]` from `_registered_tools(app)` —
  `maturity = tool.meta.get("maturity", "implemented")`, `reason =
  tool.meta.get("maturity_detail", {}).get("reason")` (guard `tool.meta` is None) — then
  call `prompts_registrar.register(app, tool_maturity=...)`.
- Append `_register_lifecycle_prompts` to `_PLANE_REGISTRARS` **after**
  `_register_doc_resources` (last entry), so every tool meta exists when it reads.

**TDD order:**
1. `test_build_app_registers_lifecycle_prompts` — build the app (existing fixtures /
   pool double as in `test_build_app_registers_doc_resources`); assert the three prompt
   names are listed and each renders a body naming its steps. (RED: no prompts yet.)
2. `test_lifecycle_prompts_disclose_partial_steps` — build the app; for a step known
   `partial` in the live registry (e.g. `runs.build`), assert its rendered line carries
   `[partial`; for an `implemented` step (e.g. `runs.create`) assert no tag. This is the
   maturity-disclosure assertion against the real registry.
3. `test_lifecycle_prompts_expected_maturity_matches_registry` — the independent
   drift guard: a hardcoded `EXPECTED_STEP_MATURITY` table in the test (tool → expected
   maturity, human-reviewed) asserted equal to the live registry's maturity for every
   referenced step. A promotion/demotion fails here until the table is updated.
4. `test_prompts_add_no_tools` — assert the registered tool-name set is identical with
   and without `_register_lifecycle_prompts` in `_PLANE_REGISTRARS` (graceful
   degradation / no behavioral coupling), reusing the monkeypatch pattern in
   `test_build_app_uses_injected_composition_secret_registry`.

**Acceptance:** new tests pass; `test_exposure_map_covers_every_registered_tool` still
passes (prompts are not tools, so the exposure map is untouched); `just type` green
(the `_registered_tools` helper and `ToolMaturity` typecheck under strict ty).

**Rollback:** remove the appended registrar entry and the helper; revert
`_advertise_envelope_output_schema` to its inline loop.

## Task 3 — Full guardrails, branch review, ship

1. `just lint && just type && just test` (full suite; architecture/doc tests live
   outside touched dirs).
2. Doc gates (already green): `just adr-status-check`, `just check-mermaid`,
   `just docs-links`, `just docs-paths`.
3. `/challenge --base main` review loop on the branch diff; address findings.
4. `security-review` if required (low surface: no auth/secrets/persistence change).
5. Push; open PR closing #624; drive to green CI + `CLEAN`/`MERGEABLE`.

**Verification gaps / limitations to note in the PR:** no live VM/stack needed (this is
a static, advisory surface); `live_vm` / `live_stack` markers stay gated and untouched.

## Self-contained notes for an implementer

- FastMCP prompt API (fastmcp-slim 3.4.2, verified): `app.add_prompt(Prompt.from_function(fn, name=, title=, description=))`;
  list via `await app._list_prompts()` (objects with `.name`); fetch via
  `await app._get_prompt(name, {})` then `await prompt.render({})` → `PromptResult` with
  `.messages`, each `.content.text`. A function returning `str` → one user-role text
  message.
- Tool meta lives on the registered `Tool.meta` dict; partial shape is
  `{"maturity": "partial", "maturity_detail": {"reason": ..., "detail": ..., "promotion": ...}}`
  (`src/kdive/mcp/tools/_docmeta.py`).
- Reuse the existing private accessor `app.local_provider._components` (already used by
  `_advertise_envelope_output_schema`, ADR-0170) via the new `_registered_tools` helper;
  do not add a second raw access site.
