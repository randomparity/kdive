# Plan — `artifacts.search_text` bounded-context schema + named rejection (#733)

Implements the spec
[`docs/specs/2026-06-23-search-text-bounded-context.md`](../../specs/2026-06-23-search-text-bounded-context.md)
and [ADR-0225](../../adr/0225-search-text-bounded-context-schema.md). Tightly coupled
(one logical change across the search module, the tool registrar/model, and the
binding middleware), so implemented directly in one session with TDD, not split across
independent subagents.

Guardrails for every commit (from `justfile`): `just lint`, `just type`, and the
focused tests. Before the first push run the full `just ci` (minus the local-only
mermaid gap noted below). Conventional commits, `Co-Authored-By` trailer.

## Task 1 — Central bound constants in `artifact_search.py`

**Where it fits:** R5 requires the schema/model bound and the runtime `_bounded_int`
bound to be provably equal. Make `artifact_search.py` the single source of the three
`(low, high)` pairs so every other site imports them.

**Files:** `src/kdive/security/artifacts/artifact_search.py`.

**Steps:**
- Add module constants `BEFORE_LINES_RANGE = (0, 10)`, `AFTER_LINES_RANGE = (0, 20)`,
  `MAX_MATCHES_RANGE = (1, 50)` (or three pairs of named ints — pick whichever reads
  cleanly and is importable).
- Rewrite the three `_bounded_int(...)` calls in `search_text` to pass `low`/`high`
  from those constants. Behavior is unchanged; the numbers move to named constants.

**Acceptance:** `search_text` still rejects out-of-range integers with
`ArtifactSearchInputError(f"{label} out of range")`; the constants are importable and
equal the values the calls use.

**TDD:** the existing `tests/security/artifacts/test_artifact_search.py` covers the
out-of-range behavior — confirm it stays green; no behavior change here.

## Task 2 — Schema constraints on the tool signature and the model

**Where it fits:** R1 (cap visible in schema) and R5 (model mirrors it).

**Files:** `src/kdive/mcp/tools/catalog/artifacts/registrar.py` (the `@app.tool`
`artifacts_search_text` signature) and
`src/kdive/mcp/tools/catalog/artifacts/reads.py` (`ArtifactSearchRequest`).

**Steps:**
- On each of `before_lines` / `after_lines` / `max_matches`, add `ge=`/`le=` from the
  Task 1 constants and rewrite the `Field(description=...)` to state the range, e.g.
  `"Context lines before each match (0–10)."`. Apply to **both** the registrar
  signature and the model so they cannot drift.

**Acceptance (TDD):**
- New test: build the app (the existing `test_tool_docs` harness builds it with a null
  pool + local keypair) and assert the `artifacts.search_text` parameter schema has the
  expected `minimum`/`maximum` per context field and the description contains the range.
  Write it red first (no constraints yet → fails), then add the constraints → green.
- The existing `test_artifacts_search_text_returns_bounded_matches` (uses
  `before_lines=1`, `after_lines=1`) and any boundary case stay green.

## Task 3 — Re-envelope the binding-time range error

**Where it fits:** R2/R3/R4. With Task 2's constraints, an over-cap arg now fails at
FastMCP arg-binding; the handler never sees it. Re-envelope at the existing
`BindingErrorMiddleware` seam.

**Files:** `src/kdive/mcp/middleware/binding_errors.py`; plus its test module.

**Steps:**
- Add a matcher that returns true iff `exc.errors()` is non-empty and **every** entry
  has `loc[0]` in `{"before_lines", "after_lines", "max_matches"}` and a `type` in
  `{"greater_than_equal", "less_than_equal"}` (range only — `int_parsing` /
  `int_from_float` must NOT match, per the spec non-goal).
- Add a builder that takes the first matching error, looks up that field's
  `(low, high)` from the Task 1 constants, and returns
  `config_error(object_id, detail=f"{field} must be between {low} and {high}",
  data={"reason": "bad_search_input"})` — reusing the existing `config_error` /
  `ConfigErrorReason` seam where it fits (note `bad_search_input` is a bare literal
  reason today, not a `ConfigErrorReason` member; keep the existing literal to satisfy
  R3 and match the handler's existing `data={"reason": "bad_search_input"}`).
- Register `"artifacts.search_text": _BindingConversion("artifact_id", <matcher>,
  <builder>)` in `_BINDING_CONVERSIONS`.

**Acceptance (TDD):**
- New tests in the binding-middleware test module: feed a `ValidationError` shaped like
  FastMCP's (construct one by validating a small Pydantic model with the same
  constraints, or by running the real tool) for each field, low and high edge for at
  least one field, and assert the resulting envelope is `configuration_error`,
  `data.reason == "bad_search_input"`, and `detail` names the field + bound.
- A test that an `int_parsing` error under `before_lines` does **not** match the
  matcher (non-goal boundary).
- Drive it red first (no conversion registered → the over-cap `ValidationError`
  propagates unconverted), then add the conversion → green.

## Task 4 — Equality assertion + regenerate the reference doc

**Where it fits:** R5 (no drift) and R1 (generated reference reflects the range).

**Files:** a test (security or mcp suite) asserting the schema/model bounds equal the
`_bounded_int` constants; `docs/guide/reference/artifacts.md` (regenerated, not
hand-edited).

**Steps:**
- Test: assert the `ArtifactSearchRequest` field constraints (read via the model's JSON
  schema or field metadata) equal the Task 1 constants — so a future edit to one side
  without the other fails.
- Run `just docs` to regenerate `docs/guide/reference/artifacts.md`; review the diff
  (the three context rows now carry the range text). Run `just docs-check` to confirm
  the committed doc matches a fresh generation.

**Acceptance:** `just docs-check` passes; the equality test fails if the bounds drift.

## Verification before push

- `just lint`, `just type`, the focused tests, then full `just ci`.
- Local-only gap: `just check-mermaid` needs `.github/scripts/mermaid-check/node_modules`
  which is absent in this environment (CI installs it); the change adds no mermaid.
  Note this limitation in the PR body and rely on CI for that gate.

## Rollback / cleanup

- Pure additive contract change (constraints + clearer descriptions + a named rejection
  detail); no migration, no port, no dependency. Reverting the branch fully restores the
  prior behavior.
- No new files to clean up beyond the worktree itself.
