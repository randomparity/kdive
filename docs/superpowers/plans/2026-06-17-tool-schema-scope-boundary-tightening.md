# Plan — Tighten tool input schemas and per-tool scope boundaries (#507)

Derived from [spec](../../specs/2026-06-17-tool-schema-scope-boundary-tightening.md) and
[ADR-0147](../../adr/0147-tool-schema-and-scope-boundary-tightening.md). Two tightly
coupled tasks on one surface (`systems.*` tool definitions + the doc guard); executed
directly in this session, not by parallel subagents.

Guardrails for every commit (from `AGENTS.md` / `justfile`):
`just lint` · `just type` (whole tree) · the touched tests · `just docs-check` (after
regenerating with `just docs`). Full `just ci` before the first push. Conventional-commit
subjects ≤72 chars + the `Co-Authored-By: Claude Opus 4.8 (1M context)` trailer.

## Task A — `state` filter → `SystemState` enum at the schema layer

**Where it fits:** Task 1 of #507 — lift the one closed-value-set `systems.list` filter
from a bare string to the enum so an invalid value is a schema-layer error.

**Files:**
- `tests/mcp/core/test_tool_docs.py` (new guard test)
- `src/kdive/mcp/tools/lifecycle/systems/registrar.py` (`systems_list` wrapper)

**TDD steps:**
1. Add `test_systems_list_state_filter_is_enum_constrained`. Build the registry via the
   existing `TOOLS` / `_build_tools()`. For `systems.list`'s `state` property: walk the
   schema (it renders as `{"anyOf": [{"enum": [...], "type": "string"}, {"type":
   "null"}]}`) and collect every `enum` list found at any depth; assert the union equals
   `{s.value for s in SystemState}`. For `shape` and `pcie`: assert no `enum` key appears
   anywhere in their property schema (recursive check). Run it → fails (today `state` is a
   bare string with no enum).
2. Change the `systems_list` wrapper `state` annotation from
   `Annotated[str | None, Field(...)]` to `Annotated[SystemState | None, Field(...)]`
   (import `SystemState` from `kdive.domain.state`). Leave `shape`, `pcie`, the
   `Field(description=...)` text, and the `SystemsListRequest(state=state, ...)` call
   unchanged.
3. Run the new test → passes. Run `tests/mcp/lifecycle/test_systems_list.py` → still green
   (`test_unknown_state_is_config_error`, `test_state_filter_rejects_invalid_values` drive
   the handler directly with raw strings, so the post-binding `SystemState(state)` guard
   still produces the `configuration_error` envelope).

**Acceptance check:** the advertised `systems.list` input schema carries the 7-value
`SystemState` enum on `state`; `shape`/`pcie` stay plain strings; handler tests unchanged.

**Rollback:** revert the one-line annotation; the test is additive.

## Task B — scope-boundary per-tool docstrings for the `systems.*` family

**Where it fits:** Task 2 of #507 — fold when-not-to-use / use-X guidance into the
confusable lifecycle tools' per-tool docstrings.

**Files:**
- `tests/mcp/core/test_tool_docs.py` (new guard test)
- `src/kdive/mcp/tools/lifecycle/systems/registrar.py` (four tool docstrings)

**TDD steps:**
1. Add `test_confusable_systems_tools_name_their_alternative`. For each of
   `systems.define`, `systems.provision`, `systems.provision_defined`,
   `systems.reprovision`, look up the tool `description` and assert:
   - the specific alternative tool name matches on a **token boundary** — use
     `re.search(r"\bsystems\.provision\b(?!_)", desc)` for the bare-`provision`
     references so a `provision_defined` mention does not satisfy it; and
   - a negative-guidance cue is present via `re.search(r"\b(instead|rather|not)\b", desc,
     re.IGNORECASE)`.
   Map each tool to its required alternative(s):
   `define → provision`, `provision → define` + `provision_defined`,
   `provision_defined → define`, `reprovision → provision`.
   Run it → fails (current one-liners name no alternative, carry no cue).
2. Rewrite the four per-tool docstrings (one to two sentences, terse for the `list_tools`
   projection), each naming the alternative on a token boundary and carrying an
   `instead`/`rather`/`not` cue, drawn from the `provision.py` module header. Keep the
   existing role suffix ("Operator only." etc.). Suggested shape:
   - `systems.define`: "… Opens a pre-provision rootfs-upload window; use
     `systems.provision` instead when the profile needs **no** upload window. Operator
     only."
   - `systems.provision`: "Mint a System and enqueue provision directly (no upload
     window) — use `systems.define` then `systems.provision_defined` instead when the
     rootfs must be uploaded first. Operator only."
   - `systems.provision_defined`: "Admit a DEFINED System after its upload window closes;
     not for a fresh System — define it with `systems.define` first. Requires operator."
   - `systems.reprovision`: "Reprovision a **ready** System in place; not for creating a
     new System — use `systems.provision` instead. Requires operator and opt-in."
   Ensure each final docstring satisfies both regexes (watch the `\b...\b(?!_)` boundary —
   do not let the only `provision` reference be `provision_defined`).
3. Run the new test → passes. Run `test_every_tool_has_a_description` and
   `test_run_cmdline_docs_describe_debug_args_only` → unaffected.

**Acceptance check:** the four descriptions name their specific alternative (token-precise)
and carry a negative-guidance cue; both measured by the guard.

**Rollback:** revert the docstrings; the test is additive.

## Task C — regenerate the tool reference + full suite + ship

1. `just docs` to regenerate `docs/guide/reference/systems.md` (the four changed tool
   descriptions land there; the `state` `Type` column stays `any` since the generator
   reads only top-level `type`/`description`). Review and commit the regenerated diff.
2. `just docs-check` → green.
3. Full `just ci` (lint, type, lint-shell, lint-workflows, check-mermaid, test) → green.
4. Adversarial-review the branch (`/challenge --base main`) + security review; address
   findings.
5. Push, open PR closing #507, drive to green CI + `MERGEABLE`/`CLEAN`.

## Commit sequence (small, bisectable)

1. `test(mcp): pin systems.list state filter to the SystemState enum` — guard test +
   the annotation change (Task A; one logical change: the schema tightening and its
   guard).
2. `docs(mcp): scope-boundary docstrings for confusable systems.* tools` — guard test +
   the four docstrings + regenerated tool reference (Task B + C step 1).

(The spec/ADR commits already landed earlier on the branch.)
