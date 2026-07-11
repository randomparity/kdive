# Plan — Worker-readability diagnostics (#699)

- **Spec:** [`docs/specs/2026-06-23-local-libvirt-worker-readability-diagnostics.md`](../../specs/2026-06-23-local-libvirt-worker-readability-diagnostics.md)
- **ADR:** [ADR-0223](../../adr/0223-local-libvirt-worker-readability-diagnostics.md)
- **Branch:** `fix/local-libvirt-worker-perms-699`

## Conventions (apply to every task)

- TDD: write the failing test first, confirm it fails for the expected reason, then the
  minimal implementation, then re-run focused tests + guardrails.
- Error taxonomy: use `ErrorCategory.CONFIGURATION_ERROR` (existing); never invent a
  string. Populate `details` with literal keys.
- Guardrails before each commit: `just lint`, `just type`, `just test` (focused subset
  while iterating), plus `just lint-shell` for the shell task. CI gates each sub-recipe
  individually, so the full `just ci` runs once before push (step 7).
- Conventional-commit subjects ≤72 chars, imperative, with the
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>` trailer.
- Line length 100; absolute imports only; Google-style docstrings on changed public APIs.

## Task 1 — Shared remediation constant + console read category split (B1)

**Where it fits:** the console-read leg of the issue. Boot confirmation fails with a
retry-implying `INFRASTRUCTURE_FAILURE` when virtlogd's `root:0600` log is unreadable;
make it an actionable `CONFIGURATION_ERROR`.

**Files:**
- `src/kdive/providers/shared/runtime_paths.py` — add a module-level
  `WORKER_READABILITY_REMEDIATION` string constant (names the three operator fixes: run
  the worker as root; set `KDIVE_LIBVIRT_URI=qemu:///session`; or grant the worker group
  read access to virtlogd/QEMU output). In `read_console_log`, split the `except OSError`:
  a `PermissionError` raises `CONFIGURATION_ERROR` with
  `details={"operation": "read_console_log", "path": str(path), "error": "PermissionError", "remediation": WORKER_READABILITY_REMEDIATION}`;
  every other `OSError` keeps the existing `INFRASTRUCTURE_FAILURE` block unchanged.
  (`except PermissionError` must precede `except OSError`.)
- `tests/providers/test_runtime_paths.py` — the existing
  `test_read_console_log_permission_failure_is_infrastructure_failure` asserts the OLD
  category; **rewrite it** to assert `CONFIGURATION_ERROR` + the `remediation` detail +
  the preserved `operation`/`path` details. Keep
  `test_read_console_log_other_oserror_is_infrastructure_failure` asserting the
  unchanged `INFRASTRUCTURE_FAILURE` for a non-permission `OSError`, and the
  missing-file-empty test.

**Acceptance:** `PermissionError → CONFIGURATION_ERROR` with `remediation`; other
`OSError → INFRASTRUCTURE_FAILURE`; missing file → `b""`. The `operation` detail is
preserved so the boot handler's re-raise (`runs_boot.py:81`) still fires.

**Guardrails:** `uv run python -m pytest tests/providers/test_runtime_paths.py -q`,
`just lint`, `just type`.

## Task 2 — Host-dump read remap (B2)

**Where it fits:** the `vmcore.fetch method=host_dump` leg. The root-owned core read
escapes uncategorized; remap the `PermissionError` to `CONFIGURATION_ERROR`.

**Prerequisite:** Task 1 (imports `WORKER_READABILITY_REMEDIATION` from
`runtime_paths.py`).

**Files:**
- `src/kdive/providers/local_libvirt/retrieve.py` — in `_capture_via_file`, wrap the
  read body (the `build_id`/`_put_stream`/`_put(extract_redacted)` block, lines ~164-178)
  with `except PermissionError as err:` that raises `CONFIGURATION_ERROR` with
  `details={"system_id": str(system_id), "operation": "read_spooled_core", "error": "PermissionError", "remediation": WORKER_READABILITY_REMEDIATION}`.
  The new `except` sits **inside** the existing `try:` whose `finally` calls
  `_remove_spool(core)`, so cleanup still runs. Import the constant from
  `kdive.providers.shared.runtime_paths`.
- `tests/providers/local_libvirt/test_retrieve.py` (or the existing retrieve test
  module — locate it first) — construct a `LocalLibvirtRetrieve` with injected seams
  where `read_vmcore_build_id_from_file` raises `PermissionError(13, "Permission denied")`
  (the first read), assert `capture(system_id, HOST_DUMP)` raises a `CategorizedError`
  with `CONFIGURATION_ERROR` + `remediation`, and assert the spool dir was removed.
  Add a sibling test: a seam raising `CategorizedError(MISSING_DEPENDENCY)` propagates
  **unchanged** (not remapped). Keep/confirm the existing happy-path test passes.

**Acceptance:** build-id-read `PermissionError → CONFIGURATION_ERROR` + `remediation`,
spool cleaned; `MISSING_DEPENDENCY` not remapped; success + store-error paths unchanged.

**Guardrails:** `uv run python -m pytest tests/providers/local_libvirt/test_retrieve.py -q`,
`just lint`, `just type`.

**Rollback:** the change is an additive `except` clause; reverting the hunk restores the
prior (uncategorized-propagation) behavior.

## Task 3 — Preflight advisory (B3)

**Where it fits:** early operator guidance before a run hits the wall.

**Files:**
- `scripts/check-local-libvirt.sh` — add a `note_warn(msg, fix)` helper mirroring
  `note_fail` but printing `WARN:`/`  fix:` to stderr and **not** setting `fail`. Add a
  `readonly LIBVIRT_URI="${KDIVE_LIBVIRT_URI:-qemu:///system}"`. After the existing
  checks, if `[[ "${LIBVIRT_URI}" == "qemu:///system" && "${EUID}" -ne 0 ]]`, call
  `note_warn` explaining boot-confirmation + host_dump need the worker to read root-owned
  virtlogd/QEMU output, with the three-fix guidance. Must stay `shellcheck`/`shfmt`
  clean (`set -euo pipefail` already present; quote `${EUID}`).
- `tests/scripts/test_check_local_libvirt.py` — add: (a) a non-root runner with the
  default/system URI prints the advisory (assert a stable substring, e.g.
  `"qemu:///session"` in stderr **and** `returncode == 0`) using the existing
  `_healthy_*` harness; (b) `KDIVE_LIBVIRT_URI=qemu:///session` → advisory absent;
  (c) `KDIVE_LIBVIRT_URI=qemu+ssh://host/system` → advisory absent. Reuse the
  PATH-stub/env-override pattern already in the file.

**Trap (from #694):** `setup-local-libvirt.sh` calls this script and
`tests/scripts/test_setup_local_libvirt.py` asserts `returncode == 0` on the healthy
path. The advisory is **non-failing** (does not touch `fail`), so those exit codes are
unchanged — verify by running that sibling test too. No test asserts empty stderr, so
the new advisory line is safe.

**Acceptance:** advisory present for non-root + system URI (exit 0); absent for session
and remote forms; existing healthy + sibling-setup exit codes unchanged.

**Guardrails:** `just lint-shell`,
`uv run python -m pytest tests/scripts/test_check_local_libvirt.py tests/scripts/test_setup_local_libvirt.py -q`.

## Task 4 — Docs (B4)

**Files:**
- `docs/operating/providers/local-libvirt-walkthrough.md` — in the worker-identity
  section (~lines 151-159): change the boot-confirmation failure category from
  `infrastructure_failure` to `configuration_error`, cross-link
  [ADR-0223](../../adr/0223-local-libvirt-worker-readability-diagnostics.md), and note
  that `check-local-libvirt.sh` now emits an advisory for the non-root/`qemu:///system`
  combination.
- `docs/adr/0211-local-libvirt-host-dump-capture.md` — add a one-line cross-link to
  ADR-0223 on the worker-readability precondition (Decision §1 / Consequences).

**Acceptance:** `just docs-links docs-paths adr-status-check check-mermaid docs-check`
green; the walkthrough no longer claims `infrastructure_failure` for the console case.

## Final verification (step 7)

- Full `just ci` green.
- `git diff --stat` touches only: `runtime_paths.py`, `retrieve.py`,
  `check-local-libvirt.sh`, the three test files, the two docs, and the already-committed
  ADR/spec/plan.
- Confirm no new env var, schema, migration, or MCP-surface change (the issue is
  classification + preflight + docs only).
