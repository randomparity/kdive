# Plan — Console head fix: drop the local cross-boot byte offset (#836)

- **Spec:** [`../specs/2026-06-26-console-head-no-cross-boot-offset-836.md`](../specs/2026-06-26-console-head-no-cross-boot-offset-836.md)
- **ADR:** [`../../adr/0258-local-console-no-cross-boot-offset.md`](../../adr/0258-local-console-no-cross-boot-offset.md)

Tightly-coupled single logical change (a function loses a parameter; two call sites
follow). Implemented directly in this session with TDD, not subagent-driven. Guardrails per
task: `just lint`, `just type` (whole tree), and the focused tests named below; full
`just ci` before push.

## Task 1 — `read_console_log` reads the whole file (drop `offset`)

**File:** `src/kdive/providers/shared/runtime_paths.py`
**Tests:** `tests/providers/test_runtime_paths.py`

- TDD: update `tests/providers/test_runtime_paths.py` first.
  - Remove the five offset tests (`test_read_console_log_offset_*`, lines ~122-149) — they
    test the removed rotation-guard/offset behavior.
  - Keep `test_read_console_log_returns_existing_bytes`, `_missing_file_is_empty`,
    `_permission_failure_is_configuration_error`, `_other_oserror_is_infrastructure_failure`.
  - Confirm the suite fails to import/collect against the new signature before editing source
    (the offset tests reference the removed param).
- Source: drop the `offset: int = 0` parameter and the `if 0 < offset <= len(data)` slice;
  return `path.read_bytes()` (whole file) with the unchanged `FileNotFoundError` → `b""`,
  `PermissionError` → `CONFIGURATION_ERROR`, `OSError` → `INFRASTRUCTURE_FAILURE` handling.
  Update the docstring to state the serial `<log>` is `append="off"` (truncated per boot,
  ADR-0258), so the whole file is this Run's boot window.
- Acceptance: `read_console_log(path)` has one positional param; whole-file + error tests
  pass; no caller passes `offset`.
- Guardrails: `uv run python -m pytest tests/providers/test_runtime_paths.py -q`, `just lint`,
  `just type`.

## Task 2 — local boot handler threads no byte offset

**File:** `src/kdive/jobs/handlers/runs_boot.py`
**Tests:** `tests/jobs/handlers/test_runs_boot.py`, `tests/mcp/lifecycle/test_runs_tools.py`

- TDD: update `tests/jobs/handlers/test_runs_boot.py` first.
  - Replace `test_mark_boot_window_local_is_file_size` + `test_mark_boot_window_local_zero_when_log_absent`
    with one test asserting the local mark is `0` regardless of the on-disk log size.
  - Keep `test_mark_boot_window_remote_uses_snapshotter` and
    `test_mark_boot_window_degrades_to_zero_on_failure` (remote path unchanged).
  - Replace `test_read_redacted_console_honors_offset` with
    `test_read_redacted_console_reads_whole_log` (no offset arg; returns the whole file).
  - Rewrite `test_local_slice_excludes_prior_boot_panic` to model truncate-on-start: the
    per-System log holds **only** this boot's bytes (libvirt truncated the prior boot), capture
    reads the whole file, and `_generic_panic_matches` is False for a clean boot whose log
    never contained the prior panic.
- Source:
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
    consumes it). Update its docstring to note `mark` is the remote part index; local is
    captured whole.
- Update `tests/mcp/lifecycle/test_runs_tools.py`: the `fail_read_console_log(_path, _offset=0)`
  stub (~line 4709) drops `_offset`; review the ~line 3675 comment about the recorded
  boot-window mark for accuracy.
- Acceptance: local capture reads the whole per-System log; remote part-index path and its
  tests unchanged; `test_runs_boot.py` + the changed `test_runs_tools.py` cases pass.
- Guardrails: `uv run python -m pytest tests/jobs/handlers/test_runs_boot.py tests/mcp/lifecycle/test_runs_tools.py -q`,
  `just lint`, `just type`.

## Task 3 — render the serial `<log>` with `append="off"`

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

## Task 4 — full guardrails + branch review

- Run the full `just ci` locally (lint, type whole-tree, lint-shell, lint-workflows,
  check-mermaid, test). `live_vm`/`live_stack` markers stay gated; note in the PR that the
  end-to-end head-retention proof needs a live KVM host.
- Run the branch review loop (`/challenge --base main`) and the security review; address
  defensible findings.

## Rollback

Revert the three source edits and their test edits; no schema, migration, or persisted state
is involved (ADR-0258).
