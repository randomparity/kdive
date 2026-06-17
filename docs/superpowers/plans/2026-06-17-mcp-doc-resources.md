# Plan — Expose operator docs and cited ADRs as MCP resources (#515)

Derived from `docs/specs/2026-06-17-mcp-doc-resources.md` and ADR-0151. Tasks are coupled
(they share one small new package), so this is implemented directly in one session with
TDD, not fanned out to independent subagents.

## Conventions (apply to every task)

- Python 3.13, `uv`. Absolute imports only. Ruff line length 100, lint `E,F,I,UP,B,SIM`.
  `ty` strict. Google-style docstrings on public APIs.
- Guardrails before each commit: `just lint`, `just type`, and the focused test. Full
  `just ci` before the first push.
- Cite ADR-0151 in the new module docstrings (the ADR is Accepted, so the
  `adr-status-check` shipped-but-Proposed guard is satisfied).
- Conventional-commit subjects ≤72 chars, ending with the repo `Co-Authored-By` trailer.

## Task 1 — Generator + packaged snapshots

**What:** Add `scripts/gen_doc_resources.py` and the committed snapshots under
`src/kdive/mcp/resources/_content/`.

- Module-level `DOC_RESOURCES` is the single source of the allowlist — define it in
  `src/kdive/mcp/resources/registrar.py` (Task 2) and import it into the script, so the
  script and the registrar cannot diverge. (Implement Task 2's `DOC_RESOURCES` table first
  if needed; the script only needs the `source`/`content_file` fields.)
- `write()` reads each `source` (repo-root-relative) with UTF-8 `read_text` and writes the
  snapshot with `write_text` (same normalization on both sides — spec AC #2).
- `--check` writes into a `tempfile.TemporaryDirectory` and diffs each generated file
  against the committed snapshot; exit 1 with a "stale — run just resources-docs" message on
  any difference, 0 when clean. Stdlib-only is not required (it runs under `uv`), but keep
  it dependency-light.
- Create the two snapshot files by running `write()` once and committing the output.

**Files:** `scripts/gen_doc_resources.py` (new),
`src/kdive/mcp/resources/__init__.py` (new, may be empty),
`src/kdive/mcp/resources/_content/build-source-staging.md` (new, generated),
`src/kdive/mcp/resources/_content/0080-remote-provisioning-disk-image-profile.md` (new,
generated).

**Acceptance:** `python scripts/gen_doc_resources.py --check` exits 0 on a fresh
generate; editing a canonical doc and re-running `--check` exits 1.

**Rollback:** delete the script, the `_content/` files, and `resources/`.

## Task 2 — Resource registrar + allowlist

**What:** `src/kdive/mcp/resources/registrar.py` defining `DOC_RESOURCES` (a tuple of
frozen dataclasses: `uri`, `source`, `content_file`, `name`, `title`, `description`,
`mime_type`) and `register(app: FastMCP) -> int`.

- `register` reads each entry's snapshot from `Path(__file__).parent / "_content" /
  content_file` (importable package data, mirroring `db/migrate.py`'s `SCHEMA_DIR`), and
  calls `app.add_resource(TextResource(uri=entry.uri, name=entry.name, title=entry.title,
  description=entry.description, mime_type=entry.mime_type, text=text))`.
- Missing snapshot file → raise `RuntimeError` naming the file (packaging regression must
  not register an empty resource). Returns the count registered.
- Docstring cites ADR-0151.

**Files:** `src/kdive/mcp/resources/registrar.py` (new).

**Acceptance:** unit test — `register(FastMCP(...))` returns 2; a monkeypatched missing
content file raises `RuntimeError`.

**Rollback:** delete the module; remove the wiring in Task 3.

## Task 3 — Wire into `build_app`

**What:** Register the resource plane in `mcp/app.py`.

- Add an import of the new registrar.
- Add a resource-registrar adapter (resources need no pool/assembly): a small function
  `_register_doc_resources(app, _pool, _assembly)` that calls `doc_resources.register(app)`,
  appended to `_PLANE_REGISTRARS`. (Reuse the existing `PlaneRegistrar` signature so the
  seam stays uniform; the pool/assembly args are ignored.)
- Confirm `_advertise_flat_output_schema` is unaffected — it sweeps `Tool` components only;
  resources are a different component type, so the zero-tool guard still measures tools.

**Files:** `src/kdive/mcp/app.py` (additive: one import, one adapter fn, one tuple entry).

**Acceptance:** `test_app.py` — `build_app(...)` `list_resources()` includes both URIs
verbatim, and `read_resource(uri)` returns text equal to the canonical doc's `read_text`.
Existing `test_build_app_*` and `test_exposure_map_covers_every_registered_tool` still pass
(resources are not tools, so the exposure completeness guard is untouched).

**Rollback:** remove the tuple entry, adapter, and import.

## Task 4 — Tests (TDD; write first within each task above)

- `tests/mcp/core/test_app.py`: add `test_build_app_registers_doc_resources` —
  list_resources contains the two URIs verbatim; read each, assert text equals
  `Path(repo_root / source).read_text()`.
- `tests/mcp/resources/test_doc_resources.py` (new):
  - `register` returns 2 and missing-snapshot raises `RuntimeError`.
  - drift: each `DOC_RESOURCES` snapshot file equals its canonical `source` via shared
    `read_text` (fails locally on un-regenerated edits, not only in the CI shell recipe).

**Files:** `tests/mcp/resources/__init__.py` (new),
`tests/mcp/resources/test_doc_resources.py` (new), `tests/mcp/core/test_app.py` (edit).

## Task 5 — CI wiring + guards

**What:** Make the drift check gate PRs.

- `justfile`: add `resources-docs` (runs the generator write) and `resources-docs-check`
  (runs `--check`); append `resources-docs-check` to the `ci` recipe's dependency list.
- `.github/workflows/ci.yml`: add a `resources-docs-check` step (hosted CI runs sub-recipes
  individually — a guard only in the umbrella `ci` recipe would not gate PRs).
- Run `actionlint`/`zizmor` (via `just lint-workflows`) after editing the workflow.

**Files:** `justfile` (edit), `.github/workflows/ci.yml` (edit).

**Acceptance:** `just resources-docs-check` exits 0; `just ci` includes it; `actionlint`
clean.

**Rollback:** revert the justfile/ci.yml edits.

## Verification gaps / risks

- The `_content/` snapshots ship in `src/`, so the runtime image (which copies `src/`)
  contains them — directly satisfies spec AC #3. There is no test that exercises the
  container, so this is verified by inspection of the `Dockerfile` `COPY` of `src/`.
- The two URIs become a public contract; the round-trip-verbatim assertion guards against a
  future FastMCP normalization change.
- `docs-paths`/`docs-links` guards: the ADR and spec reference real files; re-run both
  after edits.

## Sequencing

Task 2 (define `DOC_RESOURCES`) → Task 1 (generator imports it, produces snapshots) →
Task 3 (wire) → Task 4 (tests; written test-first per item) → Task 5 (CI). Tasks 1–4 are
one logical change set; Task 5 is a separate commit.
