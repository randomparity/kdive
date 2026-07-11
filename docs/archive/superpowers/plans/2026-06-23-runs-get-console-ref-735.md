# Plan — Surface `refs.console` on `runs.get` (#735)

- **Spec:** [docs/specs/2026-06-23-runs-get-console-ref.md](../../specs/2026-06-23-runs-get-console-ref.md)
- **ADR:** [ADR-0226](../../adr/0226-runs-get-console-ref.md)
- **Owner:** single session (tasks are tightly coupled across two files — implement
  in-session with TDD, not parallel subagents).

## Where this fits

`runs.get` (`get_run`, `src/kdive/mcp/tools/lifecycle/runs/view.py`) already loads the
`boot` step via `step_progress()` for a `SUCCEEDED` Run and passes the result into
`envelope_for_run`. The boot handler persists the console `evidence_artifact_id` into
that same `boot` step result. This change threads that id from the step read into the
envelope `refs` slot as `console`, with no new DB query and no new MCP request shape.

## Guardrails (run before every commit)

- `just lint` (ruff check + ruff format --check)
- `just type` (ty, whole tree)
- Focused tests: `uv run python -m pytest tests/mcp/lifecycle/test_runs_tools.py -q`
  (the `step_progress` unit tests and the `runs.get` integration tests both live
  here).
- Full gate before first push: `just ci` (lint, type, lint-shell, lint-workflows,
  check-mermaid, test). `check-mermaid` needs `jsdom` (a node dep) which may be absent
  locally; CI installs it. No mermaid in this change, so a local `check-mermaid`
  tooling gap is not a content failure — note it in the PR if it persists.

Conventions: ruff line length 100; absolute imports only; Google-style docstrings on
non-trivial public APIs; `ty` strict; pick the most specific `ErrorCategory` (none
needed here — this is a read-side success-path addition). Conventional commits, ≤72
char subject, `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
trailer.

## Task 1 — Carry the console id on `StepProgress`

**File:** `src/kdive/services/runs/steps.py`

1. **Test first** (`tests/mcp/lifecycle/test_runs_tools.py`, alongside the existing
   `test_step_progress_*` tests): extend/add a `step_progress` test asserting that when
   the `boot` row result carries `evidence_artifact_id`, the returned `StepProgress`
   has `console_evidence_artifact_id` equal to that id; and a test that a boot row
   with no `evidence_artifact_id` (and a missing boot row) yields `None`. Update the
   existing `test_step_progress_reads_install_boot_and_outcome` and
   `test_step_progress_missing_rows_are_pending` equality assertions to include the new
   field (they compare against a full `StepProgress(...)`).
2. Run the tests; confirm they fail because `StepProgress` has no
   `console_evidence_artifact_id` field.
3. Add `console_evidence_artifact_id: str | None` to the `StepProgress` dataclass
   (frozen, slots — add the field). In `step_progress()`, when reading the `boot`
   row's `result` mapping (the same block that extracts `boot_outcome`), also read
   `evidence_artifact_id` via the existing `_optional_str` guard and pass it into the
   constructed `StepProgress`. Keep `steps_map()` unchanged (it does not surface the
   id). Update the constructor call to pass the new field (default `None` when no boot
   row / no id).
4. Run the focused step tests + `just lint` + `just type`. Green.

**Acceptance:** `StepProgress` exposes `console_evidence_artifact_id`; populated from
the boot row result; `None` when absent/missing/non-string. Existing `boot_outcome`
behavior unchanged.

**Rollback:** revert the dataclass field + the `step_progress()` read.

## Task 2 — Add `console_ref` to `_run_artifact_refs` and wire the success path

**File:** `src/kdive/mcp/tools/lifecycle/runs/common.py`

1. **Test first** (`tests/mcp/lifecycle/test_runs_tools.py`, alongside the existing
   `test_get_booted_run_*` / `test_get_expected_crash_boot_*` integration tests):
   - `runs.get` on a `SUCCEEDED` run with a `boot` step `succeeded`/`ready` result
     carrying `evidence_artifact_id` → `resp.refs["console"] == <that id>`.
   - Same for `boot_outcome == "expected_crash_observed"` with an
     `evidence_artifact_id`.
   - `runs.get` on a booted run whose boot result has **no** `evidence_artifact_id`
     → `"console" not in resp.refs`.
   - `runs.get` on a not-yet-booted `SUCCEEDED` run → `"console" not in resp.refs`.
   - Resolution: assert the surfaced id equals the persisted console artifact's
     primary key (insert a console artifact row or reuse the boot evidence id the test
     wrote; the assertion is id-equality, no `artifacts.get` round-trip needed since
     the id *is* the primary key — the spec's "resolves via artifacts.get" is
     satisfied by id-equality to the persisted artifact pk).
2. Run; confirm the console-present tests fail (no `console` key today) and the
   absent-case tests pass (guards the no-regression contract).
3. Implement:
   - Add keyword-only `console_ref: str | None = None` to `_run_artifact_refs`; when
     non-`None`, set `refs["console"] = console_ref`. Leave `kernel`/`debuginfo`
     untouched.
   - `envelope_for_run` has a **single** shared `ToolResponse.success(..., refs=`
     `_run_artifact_refs(run), ...)` call at its end, serving every non-failed state
     (CREATED, RUNNING, SUCCEEDED, CANCELED) — there is no SUCCEEDED-specific refs
     call. Thread the id into **that one call site**:
     `refs=_run_artifact_refs(run, console_ref=step_progress.console_evidence_artifact_id`
     `if step_progress is not None else None)`. Do **not** add a SUCCEEDED-only branch
     or split the shared call — `step_progress` is already `None` for every
     non-SUCCEEDED state (`view.py` computes it only for SUCCEEDED; `runs.list` and the
     failed path pass `None`), so the single call site yields the intended behavior
     (console key only when a boot step recorded evidence) with no per-state branching.
   - `_failed_envelope` keeps its `_run_artifact_refs(run)` call (default `None`).
   - Keep the change to `_run_artifact_refs`'s signature additive and minimal so #734
     (which only reads `expected_boot_failure` from this file) does not conflict.
4. Run focused tests + `just lint` + `just type`. Green.

**Acceptance:** `runs.get` surfaces `refs.console` for booted-with-evidence runs (both
outcomes), omits it otherwise; `kernel`/`debuginfo`, failed envelopes, and `runs.list`
unchanged. The wiring touches only the single shared `success(...)` refs argument plus
the additive `_run_artifact_refs` param — no new per-state branch.

**Rollback:** revert the `console_ref` param + the success-path argument.

## Task 3 — Full gate + docs flip + PR

1. Flip the spec `Status: Draft` → `Status: Accepted` (or leave Draft; ADR is the
   ratifying record). The ADR is already `Accepted` with its README row.
2. Run the full `just ci` (note the `check-mermaid` jsdom caveat if it persists).
3. Adversarial branch review (`/challenge --base main`) + `/security-review`; address
   findings.
4. Push, open PR against `main`, body ends `Closes #735`. Drive to green + mergeable.
   Do **not** merge (orchestrator serializes merges; #735 merges before #734).

## Verification gaps / notes

- No live VM or hardware needed: all tests drive the handler directly against a
  migrated Postgres (`migrated_url` fixture), matching the existing `runs.get` tests.
- The `refs.console` id is an unvalidated pointer (same contract as
  `kernel`/`debuginfo`); the test asserts id-equality to the persisted artifact pk,
  not a live `artifacts.get` fetch.
- Cross-issue: the only write to `runs/common.py` is the additive `console_ref` param
  and the one success-path call-site argument — kept surgical so #734's read of
  `expected_boot_failure` in the same file does not conflict.
