# Plan — Build-profile schema discoverability at the MCP boundary (#482)

- **Spec:** [2026-06-16-build-profile-schema-discoverability.md](../specs/2026-06-16-build-profile-schema-discoverability.md)
- **ADR:** [0137](../../adr/0137-build-profile-schema-discoverability.md)
- **Branch:** `feat/build-profile-schema-482`
- **Execution mode:** direct, this session — the three changes are tightly coupled (the typed
  param in the registrar only behaves correctly once the middleware conversion is wired, and both
  are proven by the same end-to-end test), touch shared files (`middleware.py`), and are too small
  to fan out. TDD throughout.

## Conventions & guardrails (apply to every task)

- Python 3.13, `uv`. Absolute imports only. ≤100 lines/function, ≤100-char lines. Google-style
  docstrings on non-trivial public APIs. Zero warnings.
- Guardrail commands (run before each commit): `just lint`, `just type`, and the focused tests for
  the touched area (`uv run python -m pytest <path> -q`). Run the **full** `just ci`-equivalent
  (`just lint type test docs-check`) once before the first push. `just check-mermaid` fails locally
  on a missing `jsdom` dep (environment gap, not our change — CI installs it); note it, do not
  chase it.
- Commit one logical change at a time, imperative subject ≤72 chars, ending with the
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` trailer.
- The ADR + spec commit already exists on the branch; these tasks add code + docs on top.

## Task 1 — Type the `build_profile` param + wire the binding conversion (TDD)

**Where it fits:** Spec §1 + §2. Publishes the schema and re-envelopes the binding error. These
are one logical change because the typed param without the conversion would leak a raw FastMCP
`ToolError` for malformed input (a regression), so they land together.

**Files:**
- `src/kdive/mcp/tools/lifecycle/runs/registrar.py` — change the `runs.create` `build_profile`
  annotation from `BuildProfileInput` to `ServerBuildProfile | ExternalBuildProfile`; in the
  wrapper body, `dump_build_profile(build_profile)` before constructing `RunCreateRequest`. Import
  `ServerBuildProfile`, `ExternalBuildProfile`, `dump_build_profile` from `kdive.profiles.build`.
  Drop the now-unused `BuildProfileInput` import if nothing else uses it (keep
  `ExpectedBootFailureInput`).
- `src/kdive/mcp/middleware.py` — add
  `"runs.create": _BindingConversion("system_id", _loc_under("build_profile"), _profile_envelope)`
  to `_BINDING_CONVERSIONS`.
- `tests/mcp/core/test_binding_error_middleware.py` — add unit tests (see below).

**TDD steps:**
1. **Failing test first** in `test_binding_error_middleware.py`: drive `BindingErrorMiddleware`
   with a `runs.create` call carrying a malformed `build_profile` (a `ValidationError` whose `loc`
   is under `build_profile`, built the same way `_profile_validation_error` builds the
   `profile`-keyed one but for a `ServerBuildProfile | ExternalBuildProfile` field). Assert the
   envelope is `configuration_error`, `object_id == "sys-1"` (the call's `system_id`), `detail`
   non-empty, and the `errors` list keys are a subset of `{loc, msg, type}`. Also assert a
   non-`build_profile` `ValidationError` on `runs.create` is re-raised. Run it — confirm it fails
   because `runs.create` is not yet in `_BINDING_CONVERSIONS`.
2. **Minimal impl:** add the `_BINDING_CONVERSIONS` entry. Re-run — the middleware test passes.
3. **Type the param** in the registrar + `dump_build_profile` round-trip. Run the existing
   `runs.create` registrar/handler tests (`tests/mcp/lifecycle/test_runs_tools.py` or wherever
   `runs.create` is exercised) — confirm a valid typed profile still produces a `created` Run.
4. Run `just lint type` and the focused tests; fix warnings.

**Acceptance:** the new middleware unit tests pass; existing `runs.create` tests pass; `ty` and
`ruff` clean. A malformed `build_profile` returns the envelope, never a raw `ToolError`.

**Rollback:** revert the two source edits + the test; the param reverts to `Mapping[str, object]`.

## Task 2 — End-to-end client proof (TDD)

**Where it fits:** Spec test plan, the integration proof mirroring
`test_end_to_end_malformed_profile_returns_envelope_not_toolerror` (ADR-0124).

**Files:** `tests/mcp/core/test_binding_error_middleware.py` (or the nearest existing
`runs.create` client-level test module — colocate with the ADR-0124 end-to-end test for parity).

**TDD steps:**
1. **Failing/▸new test:** build a probe `FastMCP` app with `BindingErrorMiddleware`, register a
   `runs.create` tool typed `build_profile: ServerBuildProfile | ExternalBuildProfile`, apply
   `_advertise_flat_output_schema`, drive it through an in-memory `Client`. Assert: (a) a valid
   `{"schema_version": 1, "kernel_source_ref": "linux-6.9"}` profile is accepted and the call
   returns `created`; (b) the published `inputSchema` for `build_profile` carries the union
   (`anyOf`); (c) a malformed `{"schema_version": 1}` profile returns the `configuration_error`
   envelope via `result.data`, not a raised `ToolError`. (Note: the probe must register
   `runs.create` in `_BINDING_CONVERSIONS` — it already is after Task 1, so the real registry
   entry is exercised.)
2. Run it; confirm pass. Run `just lint type`; fix warnings.

**Acceptance:** the end-to-end test passes against the real `_BINDING_CONVERSIONS` entry and the
real `_advertise_flat_output_schema` sweep.

**Rollback:** delete the test.

## Task 3 — Document the config-fragment path + regenerate the reference

**Where it fits:** Spec §3.

**Files:**
- `src/kdive/mcp/tools/lifecycle/runs/registrar.py` — expand the `build_profile`
  `Field(description=…)` per spec §3 (config is a `catalog` `ComponentRef`; omit `config` →
  seeded `kdump` fragment with kdump+debuginfo options; `buildconfig.get` retrieves a named
  fragment to inspect; cross-reference the source-staging doc). Keep it concise — the description
  renders into the generated reference and the published schema.
- `docs/guide/reference/runs.md` — regenerated by `just docs` (do not hand-edit).

**Steps:**
1. Edit the `Field(description=…)`. The description must NOT name a non-existent `buildconfig.list`
   (spec §3 callout). Keep within the param-description style of the sibling params. **The
   description must contain no `|` pipe character and no newline** — `tool_docs()` in
   `scripts/gen_tool_reference.py:275-276` raises `ValueError` on either, hard-failing `just docs`
   / `just docs-check`. Use "or", slashes, or commas and keep it a single line.
2. Run `just docs` to regenerate `docs/guide/reference/runs.md`. Review the diff.
3. Run `just docs-check` — confirm green. Run `test_tool_docs` (`every parameter has a
   description`, coverage map) — confirm green.

**Acceptance:** `just docs-check` green; `runs.md` carries the union schema + config guidance;
`test_tool_docs` passes; the description names only real tools.

**Rollback:** revert the description edit and re-run `just docs`.

## Final verification (before push)

- Full local set: `just lint type test docs-check` green (and the rest of the `ci` recipe set that
  runs without external deps: `lock-check`, `docs-links`, `docs-paths`, `config-docs-check`,
  `env-docs-check`). `just check-mermaid` skipped only for the known local `jsdom` gap; state it in
  the PR body.
- Fold any fixup commits into their logical commit before the first push.
- Branch adversarial review (`/challenge --base main`) + security review, then push + PR.
