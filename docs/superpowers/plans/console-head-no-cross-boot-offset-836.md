# Plan — Console head fix: drop the local cross-boot byte offset (#836)

- **Spec:** [`../specs/2026-06-26-console-head-no-cross-boot-offset-836.md`](../specs/2026-06-26-console-head-no-cross-boot-offset-836.md)
- **ADR:** [`../../adr/0258-local-console-no-cross-boot-offset.md`](../../adr/0258-local-console-no-cross-boot-offset.md)

Implemented directly in this session with TDD, not subagent-driven. Guardrails per commit:
`just lint`, `just type` (whole tree), and the focused tests named below; full `just ci`
before push. Two independent logical commits (each green on its own; either order):

- **Commit A (Task 1):** remove the local cross-boot offset — `runtime_paths.py` (drop the
  `read_console_log` `offset` param) **and** its sole offset-passing caller
  `jobs/handlers/runs_boot.py` land **together**. They are one logical change: dropping the
  param without updating the caller (or vice versa) leaves a red `just type`/test commit
  because `_read_redacted_console` would call `read_console_log(path, offset)` against a
  one-arg signature. (`read_console_log(path)` already returns the whole file under the old
  signature — `offset` defaults to `0` and `if 0 < offset` is false — so the change is purely
  removing now-dead parameter plumbing.)
- **Commit B (Task 2):** render the serial `<log>` with `append="off"` — independent of
  Commit A (libvirt already truncates by default; this pins the contract).

## Task 1 (Commit A) — remove the local cross-boot byte offset

**Files:** `src/kdive/providers/shared/runtime_paths.py`,
`src/kdive/jobs/handlers/runs_boot.py`
**Tests:** `tests/providers/test_runtime_paths.py`, `tests/jobs/handlers/test_runs_boot.py`,
`tests/mcp/lifecycle/test_runs_tools.py`

- TDD (behavioral test first): rewrite `test_local_slice_excludes_prior_boot_panic` in
  `tests/jobs/handlers/test_runs_boot.py` to model truncate-on-start — the per-System log holds
  **only** this boot's bytes (libvirt truncated the prior boot), `_read_redacted_console`
  reads the whole file, and `_generic_panic_matches` is `False` for a clean boot whose log
  never contained the prior panic. Run it red against the current offset code (a test that
  writes only this boot's bytes and asserts the local mark is `0` will fail while
  `_mark_boot_window` still returns the file size), then make it green with the source edits.
- Source — `runtime_paths.py`: drop the `offset: int = 0` parameter and the
  `if 0 < offset <= len(data)` slice; return `path.read_bytes()` (whole file) with the
  unchanged `FileNotFoundError` → `b""`, `PermissionError` → `CONFIGURATION_ERROR`,
  `OSError` → `INFRASTRUCTURE_FAILURE` handling. Update the docstring: the serial `<log>` is
  `append="off"` (truncated per boot, ADR-0258), so the whole file is this Run's boot window.
- Source — `runs_boot.py`:
  - Remove `_console_log_size`.
  - `_mark_boot_window`: when `snapshotter is None` return `0` (local applies no slice — the
    `<log>` is truncated per boot, ADR-0258); keep the remote branch
    (`snapshotter.mark_boot_window`) and its best-effort degrade-to-`0`. Update the docstring.
  - `_read_redacted_console`: drop the `offset` parameter; call
    `read_console_log(console_log_path(system_id))`.
  - `_capture_console_artifact`: drop the `offset` parameter; call `_read_redacted_console`
    without it.
  - `_capture_run_console`: remote branch still passes `mark` as `start_index`; local branch
    calls `_capture_console_artifact` without an offset. Keep the `mark` parameter (remote
    consumes it). Update its docstring: `mark` is the remote part index; local is captured
    whole.
- Tests — `tests/providers/test_runtime_paths.py`: remove the five `test_read_console_log_offset_*`
  tests (~lines 122-149; they test the removed rotation-guard/offset behavior). Keep
  `_returns_existing_bytes`, `_missing_file_is_empty`, `_permission_failure_is_configuration_error`,
  `_other_oserror_is_infrastructure_failure`.
- Tests — `tests/jobs/handlers/test_runs_boot.py`: replace
  `test_mark_boot_window_local_is_file_size` + `test_mark_boot_window_local_zero_when_log_absent`
  with one test asserting the local mark is `0` regardless of on-disk log size; keep
  `test_mark_boot_window_remote_uses_snapshotter` and `_degrades_to_zero_on_failure` (remote
  unchanged); replace `test_read_redacted_console_honors_offset` with
  `test_read_redacted_console_reads_whole_log` (no offset arg; whole file).
- Tests — `tests/mcp/lifecycle/test_runs_tools.py`: the `fail_read_console_log(_path, _offset=0)`
  stub (~line 4709) drops `_offset`; review the ~line 3675 comment about the recorded
  boot-window mark for accuracy.
- Acceptance: `read_console_log(path)` has one positional param and no caller passes `offset`;
  the local mark is `0` and local capture reads the whole per-System log; remote part-index
  path and its tests unchanged; all named test files green.
- Guardrails (one commit): `uv run python -m pytest tests/providers/test_runtime_paths.py
  tests/jobs/handlers/test_runs_boot.py tests/mcp/lifecycle/test_runs_tools.py -q`,
  `just lint`, `just type`.

## Task 2 (Commit B) — render the serial `<log>` with `append="off"`

**File:** `src/kdive/providers/local_libvirt/lifecycle/xml.py`
**Tests:** `tests/providers/local_libvirt/test_provisioning.py`

- TDD: extend the provisioning XML test (~line 1203) to assert
  `serial.find("log").get("append") == "off"` (alongside the existing `file` assertion).
  Confirm it fails before the source edit.
- Source: add `append="off"` to the `<log>` `SubElement` (line 84). Add a one-line comment
  citing ADR-0258: the serial log is truncated per power-cycle, so each boot's capture is the
  whole current file (no cross-boot offset).
- Acceptance: rendered XML serial `<log>` carries `file=…` and `append="off"`.
- Guardrails: `uv run python -m pytest tests/providers/local_libvirt/test_provisioning.py -q`,
  `just lint`, `just type`.

## Task 3 — full guardrails + branch review

- Run the full `just ci` locally (lint, type whole-tree, lint-shell, lint-workflows,
  check-mermaid, test). `live_vm`/`live_stack` markers stay gated; note in the PR that the
  end-to-end head-retention proof needs a live KVM host.
- Run the branch review loop (`/challenge --base main`) and the security review; address
  defensible findings.

## Rollback

Revert the two commits (offset removal; `append="off"`) and their test edits; no schema,
migration, or persisted state is involved (ADR-0258).
