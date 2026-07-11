# Plan: `artifacts.get` token-safe window ceiling (#835)

Spec: [2026-06-26-artifacts-get-token-safe-window-835.md](../specs/2026-06-26-artifacts-get-token-safe-window-835.md)
ADR: [ADR-0257](../../adr/0257-artifacts-get-token-safe-window-ceiling.md)

This is a contained, single-session change (one constant, one `min` term, two
docstrings, a regenerated tool reference, and tests). It is executed directly with
TDD, not handed to subagents.

Guardrail commands (run individually before each commit, mirroring CI):

- `just lint` — ruff check + format check
- `just type` — `ty` whole tree
- `just docs-check` — generated tool reference matches a fresh generation
- `uv run python -m pytest tests/mcp/catalog/test_artifacts_tools.py -q` — focused
- `just test` — full suite, before first push (step 7)

## Task ordering note (TDD micro-sequence)

The new tests import `ARTIFACT_GET_WINDOW_MAX_BYTES`. If the constant does not exist
when the test runs, the "red" is an `ImportError`, not the behavioral assertion the
test is meant to prove. So define the constant first (Task 2 step 1 — it is inert
until the `min` term is added), then write the failing test (Task 1, fails on
`next_offset`), then add the `min` term (Task 2 step 2) to go green. Concretely:
Task 2-define → Task 1-write+confirm-red → Task 2-min-term → green.

## Task 1 — Failing tests for the token-safe ceiling

Where it fits: proves the #835 residual is closed and that no ADR-0247 behavior
regresses.

File: `tests/mcp/catalog/test_artifacts_tools.py`

Add, following the existing windowing-test style (`_ctx()`, an in-memory
`store_factory`, REDACTED fixtures, the `data_str` helper):

1. `test_artifacts_get_caps_window_at_token_safe_ceiling`: store a REDACTED ASCII
   artifact larger than 24 KiB (e.g. 40 KiB). Call with `max_bytes=65536`. Assert
   `len(content) == ARTIFACT_GET_WINDOW_MAX_BYTES` (24576), `content_truncated ==
   "true"`, `next_offset == "24576"`. (Spec criterion 1.)
2. Extend/assert paging: from `next_offset`, a second `max_bytes=65536` call returns
   the next ≤24 KiB; concatenating ASCII windows reproduces the source; final
   window has no `next_offset`. (Spec criterion 2 — ASCII fixture.)
3. `test_artifacts_get_explicit_max_below_ceiling_is_exact`: `max_bytes=8000` on a
   >8000-byte artifact returns exactly 8000 bytes (ceiling does not shrink a
   sub-ceiling request). (Spec criterion 4.)

Confirm the new ceiling test FAILS first (current `effective_max` returns 64 KiB,
so `next_offset` would be `65536`, not `24576`).

Import the new constant: add `ARTIFACT_GET_WINDOW_MAX_BYTES` to the existing
`from kdive.mcp.tools.catalog.artifacts.reads import (...)` block (line ~23).

Acceptance: the three assertions above; the ceiling test fails before Task 2 and
passes after.

## Task 2 — Add the ceiling constant and the `min` term

Where it fits: the core fix.

File: `src/kdive/mcp/tools/catalog/artifacts/reads.py`

- **Step 1 (before Task 1's test):** add module constant near
  `ARTIFACT_GET_WINDOW_DEFAULT_BYTES` (line ~55):
  ```python
  ARTIFACT_GET_WINDOW_MAX_BYTES = 24 * 1024
  ```
  with a comment citing ADR-0257 and the token↔byte rationale (non-configurable
  token-safety ceiling; inline_cap can only lower further). This constant is inert
  until step 2, so Task 1's test imports it and fails on behavior, not import.
- **Step 2 (after Task 1's test is red):** in `_artifact_content`, change
  `effective_max` (line 281) to:
  ```python
  effective_max = min(max(max_bytes, 1), inline_cap, ARTIFACT_GET_WINDOW_MAX_BYTES)
  ```
- Update the `artifacts_get` docstring's `effective_max` formula (lines ~230-231) to
  include the ceiling term.

Acceptance: Task 1 tests pass; the ADR-0247 tests (`test_artifacts_get_*`,
including `_clamps_window_to_lowered_inline_cap`, the default-window test, and the
degenerate-cap test) still pass unchanged (8 KiB < 24 KiB; default 16 KiB < 24 KiB).

## Task 3 — Update the param description + regenerate the tool reference

Where it fits: discoverability (spec criterion 6) and the `just docs-check` CI gate.

Files: `src/kdive/mcp/tools/catalog/artifacts/registrar.py`,
`docs/guide/reference/artifacts.md` (generated), `tests/mcp/catalog/test_artifacts_tools.py`

- In `registrar.py` `max_bytes` description (lines ~94-103): state the 24 KiB
  token-safe ceiling in addition to the existing `KDIVE_ARTIFACT_INLINE_MAX_BYTES`
  mention, e.g. "The server caps the window at the smaller of a 24 KiB token-safe
  ceiling and KDIVE_ARTIFACT_INLINE_MAX_BYTES (default 65536)".
- Update the param-schema discoverability assertion in the tool-docs test (line
  ~912) so it still passes (it checks `KDIVE_ARTIFACT_INLINE_MAX_BYTES` is in the
  description; keep that and optionally also assert the ceiling is mentioned).
- Run `just docs` to regenerate `docs/guide/reference/artifacts.md`; review the diff.

Acceptance: `just docs-check` passes; the tool-docs param test passes.

## Commit sequencing

1. Task 2 + Task 1 as one logical commit (`fix: cap artifacts.get window at a
   token-safe 24 KiB ceiling (#835)`) — test + implementation together is the TDD
   unit. (Write the test first within the working tree, confirm red, then add the
   impl, then commit once green — small and bisectable.)
2. Task 3 as a second commit (`docs: surface the artifacts.get token-safe window
   ceiling in the tool reference`).

## Rollback / cleanup

Pure additive guard (one constant, one extra `min` term). Reverting the two commits
restores ADR-0247 behavior exactly; no migration, no persisted state, no config
surface. Nothing to clean up beyond standard branch teardown.
