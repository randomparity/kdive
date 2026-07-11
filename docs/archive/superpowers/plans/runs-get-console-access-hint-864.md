# Implementation plan: console read-path hint on `runs.get` (#864)

- **Spec:** `docs/specs/2026-06-26-runs-get-console-access-hint-864.md`
- **ADR:** `docs/adr/0262-runs-get-console-access-hint.md`
- **Issue:** #864
- **Branch:** `feat/console-fetch-raw-hint-864`
- **Scope owned:** `src/kdive/mcp/tools/lifecycle/runs/common.py` and its unit test
  `tests/mcp/lifecycle/test_runs_tools.py`. Do **not** touch `allocations/`,
  `mcp/exposure.py`, or shared role-filter helpers (other agents own those).

## Goal

When `runs.get` surfaces `refs.console`, also surface `data.console_access` naming the
two VIEWER-accessible read paths for the redacted console artifact
(`artifacts.search_text`, `artifacts.get`), and never `artifacts.fetch_raw`. Additive
`data` on an existing read path; no schema/RBAC/migration/tool-surface change.

## Guardrail commands (run before every commit)

- `just lint` — ruff check + format check (read-only, mirrors CI)
- `just type` — `ty check`
- `just test` — pytest (focused subset during TDD: `uv run pytest
  tests/mcp/lifecycle/test_runs_tools.py -q`)
- Before first push, the full gate: `just ci` (note: `check-mermaid` fails locally on a
  missing node module in the checker itself — an environment gap, not a content failure;
  CI has the dependency). Also confirm `just docs-check` passes (the `data` key is
  freeform so the generated tool reference should be unchanged, but verify).

## Tasks (single tightly-coupled change — implement directly, TDD)

### Task 1 — Failing tests first

In `tests/mcp/lifecycle/test_runs_tools.py`, add unit tests that drive
`runs_common.envelope_for_run` directly (matching the existing
`test_envelope_for_run_*` pattern with `_run_model` + `StepProgress`):

1. `test_envelope_for_run_surfaces_console_access_hint` — `SUCCEEDED` run with
   `StepProgress(install="succeeded", boot="succeeded", boot_outcome="ready",
   console_evidence_artifact_id="<id>")`. Assert:
   - `resp.refs["console"] == "<id>"`
   - `resp.data["console_access"] == {"ref": "console", "search":
     "artifacts.search_text", "full_text": "artifacts.get"}`
   - `"artifacts.fetch_raw" not in resp.data["console_access"].values()`
2. `test_envelope_for_run_console_access_hint_for_expected_crash` — same but
   `boot_outcome="expected_crash_observed"` with a console id; assert `console_access`
   present (covers the second boot outcome that carries console evidence).
3. `test_envelope_for_run_console_access_hint_absent_without_console_ref` —
   `SUCCEEDED` run with `StepProgress(..., console_evidence_artifact_id=None)`; assert
   `"console_access" not in resp.data` and `"console" not in resp.refs`.
4. `test_envelope_for_run_console_access_hint_absent_without_step_progress` —
   `SUCCEEDED` run, no `step_progress`; assert `"console_access" not in resp.data`.

Run the focused subset and confirm tests 1/2 fail (KeyError / missing key) for the
expected reason before implementing.

### Task 2 — Minimal implementation

In `src/kdive/mcp/tools/lifecycle/runs/common.py`:

1. Add a module-level constant near `_run_artifact_refs`:
   ```python
   _CONSOLE_ACCESS_HINT: dict[str, str] = {
       "ref": "console",
       "search": "artifacts.search_text",
       "full_text": "artifacts.get",
   }
   ```
   with a comment explaining the two VIEWER read paths and why `fetch_raw` is excluded
   (cite ADR-0262 / #864).
2. In `envelope_for_run`, after `console_ref` is computed (currently line ~232), when
   `console_ref is not None` set `data["console_access"] = cast(JsonValue,
   dict(_CONSOLE_ACCESS_HINT))` (fresh copy per envelope) before the `return`.
3. Update the `_run_artifact_refs` docstring so the `console` paragraph names both read
   paths and the `console_access` affordance, and records that `fetch_raw` is excluded.

Acceptance check: the four tests pass; `kernel`/`debuginfo`/`build-log` refs and all
other `data` keys are unchanged.

### Task 3 — Guardrails + generated docs

- Run `just lint`, `just type`, focused `just test`.
- Run `just docs-check` and `just resources-docs-check`; if either reports drift,
  regenerate with `just docs` / `just resources-docs` and review the diff (expected:
  no change, since `data` is freeform and no tool description/schema changed).

## Verification gaps / risks

- The affordance value is the tool *name* `artifacts.get`; for a console artifact above
  the inline-serve ceiling, `artifacts.get` returns a `download_uri` instead of paged
  content — still the same tool, so the hint stays correct. No code handles that here.
- No DB/integration test needed: the change is in the pure-unit envelope builder, driven
  directly with injected `Run`/`StepProgress` (the project's prescribed boundary for
  `envelope_for_run`, per existing tests).

## Rollback

Single-commit revert of the `common.py` change restores prior behavior; the affordance
is purely additive, so no data migration or compatibility step is needed.
