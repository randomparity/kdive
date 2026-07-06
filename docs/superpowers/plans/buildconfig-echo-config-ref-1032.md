# Plan — `buildconfig` echoes `config_ref`; provider off the agent surface (#1032)

- **Spec:** [`docs/superpowers/specs/2026-07-05-buildconfig-echo-config-ref-1032.md`](../specs/2026-07-05-buildconfig-echo-config-ref-1032.md)
- **Branch:** `feat/buildconfig-echo-config-ref-1032` (off `main`)
- **Guardrails:** `just lint`, `just type` (whole tree), `just test`, `just docs`
  (regenerate `docs/guide/reference/*`), then `just ci` before push. Run a single
  test with `uv run python -m pytest <path>::<name> -q`.
- **Method:** TDD — write/adjust the failing test first, then the code, per task.

## Context

`buildconfig.set`/`list`/`get` return fragment identity but no ready-to-use
`CatalogComponentRef`, so an agent must hand-construct
`{kind:"catalog", provider:???, name}` to reference a fragment from
`runs.create`. `provider` is decorative for build-config catalog refs (the
catalog is keyed by name alone), but the agent-facing schema marks it required,
implying a namespace. Fix: echo a canonical `config_ref` and point agents at it
(never teach "any value works"); `config` is `source='server'`-lane only.

All work is in one package area; tasks are ordered so each leaves the tree green.

---

## Task 1 — `catalog_config_ref` factory (single source of the `system` convention)

**Where it fits:** spec decision 1. Prerequisite for every echo site so the
`provider="system"` value has exactly one definition.

**Files:**
- `src/kdive/build_configs/defaults.py`
- `tests/build_configs/test_defaults.py` (new file; `tests/build_configs/`
  exists — `test_catalog.py`, `test_rules.py`, etc.)

**Do:**
1. Add `def catalog_config_ref(name: str) -> CatalogComponentRef:` returning
   `CatalogComponentRef(kind="catalog", provider="system", name=name)`. Move the
   existing decorative-`provider` explanation comment onto this factory.
2. Redefine `DEFAULT_CONFIG_REF = catalog_config_ref("kdump")`.
3. Export `catalog_config_ref` in `__all__`.

**Tests (write first, watch fail):**
- `catalog_config_ref("x").model_dump() == {"kind":"catalog","provider":"system","name":"x"}`
  — literal, so a `provider` drift (e.g. to `"seed"`) fails.
- `DEFAULT_CONFIG_REF.provider == catalog_config_ref("kdump").provider` and
  `DEFAULT_CONFIG_REF == catalog_config_ref("kdump")`.
- `catalog_config_ref("x").model_dump()` round-trips through
  `parse_component_ref` to an equal `CatalogComponentRef`.

**Acceptance:** the three asserts pass; `DEFAULT_CONFIG_REF` unchanged in value;
no other module references a second `"system"` literal for a config ref (grep
`provider="system"` / `provider='system'` under `src/kdive/` — only the factory
remains).

**Rollback:** revert the factory; inline constant returns.

---

## Task 2 — Echo `data.config_ref` from `set` / `list` / `get`

**Where it fits:** spec decision 2. The behavioral core.

**Files:**
- `src/kdive/mcp/tools/catalog/build_configs.py`
- `tests/mcp/catalog/test_build_configs_tool.py` (existing suite for these
  handlers)

**Do:** import `catalog_config_ref` from `kdive.build_configs.defaults` and add a
`"config_ref": catalog_config_ref(<name>).model_dump()` entry to the `data` of:
1. `set_build_config` success payload (`:189-199`) — `<name>` is the validated
   `name`.
2. `_entry_envelope` (`:202-213`) — `<name>` is `entry.name`.
3. `read_build_config` (`:118-127`) — `<name>` is `entry.name` (the resolved
   row), **and** switch that success envelope's subject id from the `name`
   argument to `entry.name` (spec decision 2 / compat no-op).

**Tests (write first):**
- `set` success `data["config_ref"] == catalog_config_ref(name).model_dump()`.
- each `list` item `data["config_ref"] == catalog_config_ref(item_name).model_dump()`.
- `get` `data["config_ref"] == catalog_config_ref(name).model_dump()` and the
  envelope subject equals the row name.
- Extend/keep the existing payload-shape assertions (`name/sha256/source/...`)
  so the additive key does not regress prior fields.

**Acceptance:** all three tools echo the canonical ref; existing fields intact;
`get` subject is the row name; `just test` green for the suite.

**Rollback:** drop the `config_ref` keys and restore the `get` subject.

---

## Task 3 — Lane boundary is enforced, not assumed (regression test)

**Where it fits:** spec acceptance "tests pin the invariants". Pins that the
echoed ref works in the server lane and is refused in the external lane.

**Files:**
- `tests/profiles/test_build.py` (existing home for
  `ServerBuildProfile`/`ExternalBuildProfile` parse tests; alongside
  `tests/profiles/test_build_profile_source.py`).

**Do (test-only):**
1. Build `ref = catalog_config_ref("kdump").model_dump()`.
2. Assert `ServerBuildProfile.model_validate({"schema_version":1,"source":"server",
   "kernel_source_ref":"warm","config":ref})` succeeds and `.config` is a
   `CatalogComponentRef` with `name=="kdump"`.
3. Assert `ExternalBuildProfile.model_validate({"schema_version":1,
   "source":"external","config":ref})` raises `ValidationError` (extra `config`
   forbidden).

**Acceptance:** both asserts pass, pinning the lane boundary at the model level
independent of any error message. (Per spec, do **not** assert a clean
`config`-named error at the `runs.create` union boundary — it produces a merged
union error by design.)

**Rollback:** delete the test.

---

## Task 4 — Agent-facing text: `runs.create` Field + buildconfig docstrings

**Where it fits:** spec decisions 3 and 4. The discoverability surface.

**Files:**
- `src/kdive/mcp/tools/lifecycle/runs/registrar.py` (`config` clause of the
  `build_profile` `Field`, `:83-86`)
- `src/kdive/mcp/tools/catalog/build_configs.py` (the `@app.tool` wrapper
  docstrings for `set` `:384`, `list` `:315`, `get` `:352`)

**Do:**
1. `runs.create` Field: in the `source='server'` part of the `config` clause,
   state that the ref to paste is the `config_ref` echoed by
   `buildconfig.set`/`list`/`get` (it fills in the required `provider`), and that
   `runs.validate_profile` is the read-only pre-flight for a profile. Keep the
   worked example. Do **not** add a `source`→`provider` mapping; do **not** state
   `provider` is decorative / "any value works".
2. Wrapper docstrings (`set`/`list`/`get`): note the response carries
   `data.config_ref` to paste into a **`source='server'`** `runs.create` build,
   and point at `runs.validate_profile`. Avoid an unqualified "ready-to-use".
   Keep each docstring one line where the schema renders it (FastMCP serializes
   the first line into the tool description).

**Constraints:** honor the repo doc-style guard (plain, factual prose; the
project's banned-adjective list; "Milestone" not the S-word). Keep
`Field`/docstring text within the existing paragraph shape.

**Tests / verification:** none asserts wording, but confirm the ADR/leak and
docstring guards (whatever the repo runs) stay green; see Task 5 regen.

**Acceptance:** the Field and three docstrings carry the lane qualifier and the
`validate_profile` pointer; no forbidden prose; `provider`-decorative wording
absent from agent-facing text.

**Rollback:** revert the two files' text.

---

## Task 5 — Regenerate reference docs; full guardrails

**Where it fits:** spec acceptance "`just docs` … no drift; `just ci` green".

**Files (generated):** `docs/guide/reference/buildconfig.md`,
`docs/guide/reference/runs.md` (do not hand-edit; regenerate).

**Do:**
1. `just docs` to regenerate the tool-reference docs from the changed docstrings
   / `Field`.
2. `just docs-check` (or the repo's drift check) to confirm no residual drift.
3. Run `just lint`, `just type`, `just test`, then `just ci`.

**Acceptance:** generated docs reflect the new wrapper docstring lines; `just ci`
green (lint, type whole-tree, lint-shell, lint-workflows, check-mermaid, test).

**Rollback:** `just docs` after any code revert re-syncs the generated files.

---

## Sequencing & notes

- Order 1 → 2 → 3 → 4 → 5. Task 1 is a hard prerequisite for 2 and 3 (they import
  the factory). 4 is independent of 2/3 but 5 must run last (regen after all text
  changes).
- No migration, no schema, no auth change. No ADR (spec "No ADR").
- Commit per task with a conventional-commit subject; stage explicit paths.
- Guardrail memory: `just test` alone misses generated-doc drift — always run
  `just docs`/`docs-check` after Field/docstring edits (Task 5).
