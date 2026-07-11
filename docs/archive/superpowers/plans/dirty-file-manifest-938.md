# Plan: warm-tree dirty-file manifest + discoverable provenance (#938)

- Spec: [docs/specs/2026-06-30-dirty-file-manifest-938.md](../../specs/2026-06-30-dirty-file-manifest-938.md)
- ADR: [ADR-0282](../../adr/0282-warm-tree-dirty-file-manifest.md)

Execution mode: tightly-coupled single change (shared provenance type threads through five
modules), implemented in one session with TDD per ADR-0282. Not subagent-parallelizable —
every step touches the same widened type.

Guardrails (run before every commit): `just lint`, `just type`, and the focused tests for the
touched module; `just ci` once before push. Conventions: ruff line length 100; native JSON
booleans (no stringified flags); the `@app.tool` wrapper docstring is the agent-facing contract.

## Phase 1 — git probes (`build_artifacts/provenance.py`)

Add two best-effort probes plus a raw-output helper that distinguishes "succeeded with empty
output" from "failed" (the existing `_git_read` collapses both to `None`).

- Add `_git_run(tree, *args, timeout) -> str | None`: raw `stdout` on returncode 0, `None` on
  non-zero exit / `OSError` / `SubprocessError`. Refactor `_git_read` to call it and apply
  `.strip() or None`.
- Add `dirty_tracked_files(tree, *, timeout=DEFAULT_GIT_READ_TIMEOUT) -> list[str] | None`:
  `git diff --name-only -z HEAD`, split on `\0`, drop empties. `None` on probe failure; `[]`
  legitimately empty when there are no tracked-vs-HEAD changes.
- Add `has_untracked_files(tree, *, timeout=DEFAULT_GIT_READ_TIMEOUT) -> bool | None`:
  `git ls-files --others --exclude-standard -z`, `True` iff any non-empty path. `None` on failure.

TDD (tests in `tests/build_artifacts/` mirroring existing provenance tests, or extend the
warm-tree tests in `tests/providers/build_host/test_build_provenance.py` which already build real
git trees): a tracked edit → `dirty_tracked_files` lists the path, `has_untracked_files` False;
an untracked file → `dirty_tracked_files` empty, `has_untracked_files` True; a non-git dir →
both `None`. Acceptance: probes return the documented values and never raise.

Files: `src/kdive/build_artifacts/provenance.py`, test file.
Rollback: drop the new functions; `_git_read` behavior must be byte-identical after the refactor.

## Phase 2 — widen the provenance value type

Widen `dict[str, str | bool]` → `dict[str, str | bool | list[str]]` at every producer and
consumer (dict invariance: producers must widen too, see spec):

- `src/kdive/build_artifacts/results.py` — `BuildOutput.build_provenance`.
- `src/kdive/services/runs/steps.py` — `BuildStepResult.build_provenance`, `dump()` return type,
  and `_optional_provenance_map` to admit a `list` whose elements are all `str` (reject dict,
  number, mixed list → whole map degrades to `None`).
- `src/kdive/mcp/tools/lifecycle/runs/common.py` — `_build_provenance_data` param,
  `envelope_for_run` `build_provenance` param.
- `src/kdive/services/runs/complete_build.py` — `_finalize_external_build` `source_provenance`.
- `src/kdive/domain/external_provenance.py` — `external_source_provenance` return (widen literal
  annotation; it never emits a list, but the type must match for assignability — no new import,
  keep domain layer free of a build_artifacts dependency).

TDD: extend `_optional_provenance_map` coercion tests — a `{"dirty_files": ["a", "b"]}` map round-
trips; `{"dirty_files": ["a", 1]}` and `{"x": 123}` degrade to `None`. Add a `runs.get`
round-trip test (alongside `test_get_succeeded_run_surfaces_build_provenance`) asserting a
persisted `dirty_files` list reaches `data.build_provenance` as a JSON array.

Files as listed + `tests/services/runs/test_steps.py`, `tests/mcp/lifecycle/test_runs_tools.py`.
Rollback: revert the union everywhere (all-or-nothing; a partial revert fails `ty`).

## Phase 3 — assemble the manifest (`providers/shared/build_host/dispatch.py`)

Extend `_with_warm_tree_provenance`: after recording `dirty`, when `dirty` is true, attach the
new fields via a small helper (keep the function ≤100 lines, complexity ≤8):

- `untracked` = `has_untracked_files(kernel_src)` when not `None`.
- `tree_sha` (unchanged).
- `dirty_files` = `dirty_tracked_files(kernel_src)` when non-empty; cap at
  `DIRTY_FILES_MANIFEST_MAX = 100` (module constant) and add `dirty_files_truncated: True` when
  over the cap.

Clean tree (`dirty` false) and non-git tree paths are unchanged.

TDD (extend `tests/providers/build_host/test_build_provenance.py`):
- tracked edit → `{label, resolved_commit, dirty: True, untracked: False, tree_sha, dirty_files:[f]}`.
- untracked-only → `{label, resolved_commit, dirty: True, untracked: True}` (no dirty_files/tree_sha).
- clean → `{label, resolved_commit, dirty: False}` (update existing assertion: stays unchanged).
- >100 tracked files → `dirty_files` capped at 100 + `dirty_files_truncated: True`.
- non-git → `{label}` (unchanged).

Update the two existing warm-tree assertions (`..._records_tree_sha`, `..._untracked_only...`) to
include the new fields.

Files: `src/kdive/providers/shared/build_host/dispatch.py`, test file.
Rollback: revert the helper; existing `dirty`/`tree_sha` behavior unaffected.

## Phase 4 — discoverability (docstrings + prose + generated reference)

- `src/kdive/mcp/tools/lifecycle/runs/registrar.py` — add a `data.build_provenance` paragraph to
  the `runs.get` wrapper docstring naming the shape (`label`, `resolved_commit` decorative when
  dirty, `dirty`, `untracked`, `tree_sha`, `dirty_files`/`dirty_files_truncated`), tracked-git-only.
- Same file — extend the `runs.create` `build_profile` warm-tree enumeration with `dirty_files`
  and `untracked`.
- `docs/operating/build-source-staging.md` — extend the `{label, resolved_commit, dirty,
  tree_sha?}` section with `dirty_files` and `untracked`.
- Regenerate `docs/guide/reference/runs.md` with `just docs`.

TDD: extend `tests/mcp/core/test_tool_docs.py` — assert the `runs.get` description contains
`build_provenance` (and `dirty_files`). Keep the existing `runs.create` provenance doc test green.

Files: `registrar.py`, `docs/operating/build-source-staging.md`, `docs/guide/reference/runs.md`,
`tests/mcp/core/test_tool_docs.py`.
Rollback: revert docstring edits and re-run `just docs`.

## Phase 5 — full guardrails

`just ci` green (it runs `docs-check`, which fails if the committed tool reference is stale —
phase 4 must regenerate it). Then branch adversarial review, then PR.
