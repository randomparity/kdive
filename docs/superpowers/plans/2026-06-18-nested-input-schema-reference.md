# Plan: Render nested MCP input schemas in the tool reference (#566, ADR-0177)

Implements [ADR-0177](../../adr/0177-nested-input-schema-reference.md). Single feature
branch `feat/566-nested-schema-docs`, implemented directly in-session (tightly coupled:
the renderer, the generated docs, and the guard test all move together). Guardrails:
`just lint`, `just type`, `just test` (focused: `tests/mcp/core/test_tool_docs.py`), and
`just docs` + `just docs-check`. Full `just ci` green before pushing.

## Context

`scripts/gen_tool_reference.py` renders each parameter's `Type` cell as
`str(spec.get("type", "any"))`, collapsing structured payloads. The fix is a recursive
schema renderer in that script, regenerated `docs/guide/reference/*.md`, a worked
`build_profile` example, a cross-link from the provisioning `profile`, and a guard test.

## Task 1 — Recursive schema renderer in `gen_tool_reference.py`

**Where it fits:** replaces the scalar `Type` cell (ADR-0177 decision 1).

**Files:** `scripts/gen_tool_reference.py`.

**Do:**
- Add `_MAX_SCHEMA_DEPTH` named constant (semantic recursion bound; the deepest live param,
  `systems.define.profile`, measures 7 semantic levels — descend into `properties` values,
  `items`, and `anyOf`/`oneOf` variants — so set the bound to 12 for headroom). A test pins
  the bound `> measured deepest live depth` so a future deeper schema fails at the test, not
  first in CI doc-gen.
- Add `render_schema_type(spec, depth=0) -> str`: returns the inline type token for a
  subschema. Handles scalar `type`, `enum` (back-ticked comma list), `const` (`` `=v` ``),
  `anyOf`/`oneOf` (`a | b`; collapse `[T, null]` → `T (nullable)`), `array` (`array<item>`),
  bare `object` (`object`). Raises `ValueError` on `$ref`/`$defs` or when `depth >
  _MAX_SCHEMA_DEPTH`.
- Add `render_param_detail(name, spec, required, depth=0) -> list[str]`: emits the Markdown
  sub-list lines for an object's fields / an array's item fields, recursing. Object →
  per-field bullet `- \`field\` — type — required — description`. Used under the table row.
- Keep `ParamDoc` but compute `type` via `render_schema_type` and add a `detail: tuple[str, ...]`
  of pre-rendered sub-list lines (empty for scalars).
- `render_namespace` appends `p.detail` lines after each table (object/array params render a
  "Fields" sub-list below the row; the table `Type` cell stays a one-line summary).

**Acceptance:** `runs.create.build_profile` row shows the `anyOf` variants and each lane's
fields incl. enums/const; `systems.define.profile` shows nested fields; `systems.list.state`
shows the enum values; no structured param renders as a bare `any`/`object`/`array`.

## Task 2 — Worked `build_profile` example + provisioning cross-link

**Where it fits:** ADR-0177 decisions 2 and 3.

**Files:** `scripts/gen_tool_reference.py`.

**Do:**
- Add `_BUILD_PROFILE_EXAMPLES: tuple[dict, ...]` pure constant: one server-lane and one
  external-lane example dict, each a minimal valid `BuildProfile`.
- `render_namespace` (or a per-tool hook) emits a fenced ```json example block under
  `runs.create` after its parameter table.
- For `systems.define`/`systems.provision`, append a sentence linking to
  `systems.profile_examples` (`See \`systems.profile_examples\` for a ready-to-edit example
  per provider.`).

**Acceptance:** `runs.md` contains a JSON `build_profile` example for each source lane;
`systems.md` links `systems.define.profile`/`systems.provision.profile` to
`systems.profile_examples`.

## Task 3 — Docs guard + example-validity test

**Where it fits:** ADR-0177 decision 4 + the example no-drift property (decision 2).

**Files:** `tests/mcp/core/test_tool_docs.py` (new tests); reuse `scripts.gen_tool_reference`
helpers.

**Do:**
- `test_structured_params_render_nested_detail`: for every tool param whose schema is
  structured (`properties`/`items`/`enum`/`anyOf`/`oneOf` present), assert the rendered
  output (`render_schema_type` + joined `render_param_detail`) contains at least one of: a
  field name from `properties`, an enum value, or a variant separator `|`. Fail listing
  offenders. Scalars are exempt.
- `test_build_profile_examples_are_valid`: assert each `_BUILD_PROFILE_EXAMPLES` entry parses
  via `BuildProfile.parse` without raising, and that the set covers both `source` lanes.
- `test_schema_renderer_rejects_ref`: feed `{"$ref": "#/x"}` to `render_schema_type`, assert
  `ValueError`.
- `test_schema_renderer_depth_bound`: feed a synthetic over-deep object, assert `ValueError`.
- `test_max_schema_depth_clears_live_schemas`: assert `_MAX_SCHEMA_DEPTH` exceeds the
  deepest live tool-param semantic depth (so the bound has headroom and a future deeper
  schema trips this test before tripping doc-gen).

**Acceptance:** all four tests pass on the implemented renderer; reverting Task 1 makes
`test_structured_params_render_nested_detail` fail (verify by temporary revert).

## Task 4 — Regenerate docs + full guardrails

**Do:** `just docs`; review the diff to `docs/guide/reference/*.md`; run `just ci`.

**Acceptance:** `just docs-check` clean (regenerated docs committed); `just ci` green.

## Rollback / cleanup

Pure docs+test change, no migration, no runtime/DB effect. Rollback = revert the branch.
The generated `*.md` and the renderer move together; `docs-check` keeps them in sync.

## Rebase note (parallel wave)

#570 also edits `gen_tool_reference.py` (maturity-reason rendering) and #571/#567 add tools /
richer schemas. The renderer is a cohesive function group; after rebasing onto an updated
main, re-run `just docs` to absorb new tools/schemas and re-run `just ci`. New tools with
structured params are automatically covered by the Task 3 guard.
