# Plan: validate/flag build-image volume at build-host registration (#627)

- **Spec:** [docs/specs/2026-06-20-validate-build-image-volume.md](../../specs/2026-06-20-validate-build-image-volume.md)
- **ADR:** [0196](../../adr/0196-validate-build-image-volume-at-registration.md)
- **Branch:** `feat/validate-build-image-volume-627`
- **Base:** `main`

## Conventions (apply to every task)

- Python 3.14, `uv`. Absolute imports only; 100-char lines; ruff set `E,F,I,UP,B,SIM`.
- Guardrails before each commit: `uv run python -m pytest <focused> -q`, then `just lint` and
  `just type` (whole-tree). Full `just ci` before push.
- Conventional-commit subject ≤72 chars, imperative; end every commit with
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- TDD: write the failing test, run it, confirm it fails for the expected reason, then the
  minimal implementation, then re-run focused + guardrails.
- Return the project's `ToolResponse`/`CategorizedError` types with the most specific
  `ErrorCategory`. Pointers are literal identifiers, not prose.
- Stay strictly in the file scope below. Do **not** touch `diagnostics/checks.py`,
  `runs.profile_examples`, or the build_hosts **list** registrar (siblings own those).

## Task 1 — registration name heuristic (TDD)

**Where it fits:** acceptance criterion #1 — a guest/boot rootfs registered as an ephemeral
build host is rejected, not silently accepted.

**Files:**
- `src/kdive/mcp/tools/ops/build_hosts/register.py` (implementation)
- `tests/mcp/ops/test_build_hosts.py` (tests)

**Steps:**

1. Add failing tests in `tests/mcp/ops/test_build_hosts.py` driving
   `register_ephemeral_libvirt_build_host` with an injected pool + admin ctx:
   - `base_image_volume="fedora-kdive-remote-base-43.qcow2"` → `ToolResponse` failure,
     `error_category == CONFIGURATION_ERROR`, `data["reason"]` names the guest-rootfs/toolchain
     mistake, `data` carries the `ops.diagnostics --with-buildhost-agent` pointer; assert **no
     row was inserted** (query `build_hosts` by name → none / use `get_by_name`).
   - upper/mixed case (`"FEDORA-KDIVE-REMOTE-BASE-43.QCOW2"`) → rejected.
   - control: `base_image_volume="kdive-build-base.qcow2"` (the existing
     `test_register_ephemeral_creates_row` default) still creates the row — keep the existing
     happy-path test green.
   - the existing empty-string rejection test
     (`test_register_ephemeral_without_base_image_volume_config_error`) unchanged.
2. Run the new tests; confirm they fail (the guest-rootfs name currently registers a row).
3. Implement in `register.py`:
   - Add module constant `_GUEST_ROOTFS_VOLUME_MARKER = "kdive-remote-base"` with a short
     comment citing the ADR-0188 catalog convention.
   - In `_ephemeral_plan`, **after** the existing non-blank check, if
     `_GUEST_ROOTFS_VOLUME_MARKER in request.base_image_volume.lower()` return a
     `configuration_error` whose `data` carries both `reason` and the actionable `detail`
     (build-image staging pointer + `ops.diagnostics --with-buildhost-agent`).
   - Extend `_config_error` to accept an optional `detail` (and any extra `data`) OR add a
     small sibling helper; keep `reason` mandatory. Prefer extending `_config_error` with an
     optional keyword so the existing callers are unchanged. Match the `data.reason`/`detail`
     shape ADR-0174 uses.
4. Run focused tests + `just lint` + `just type`. Commit:
   `fix(build): reject a guest-rootfs volume at ephemeral build-host registration`.

**Acceptance check:** the four registration tests pass; no DB write occurs on rejection; the
SSH path and the existing happy/empty paths are untouched.

**Rollback:** revert `register.py` + the added tests; the heuristic is additive, no migration.

## Task 2 — build-time toolchain-missing pointer (TDD)

**Where it fits:** acceptance criterion #2 — a `git: not found`-class build failure surfaces a
pointer to the build-host agent diagnostic.

**Files:**
- `src/kdive/providers/shared/build_host/transports/shell_transport.py` (implementation)
- `tests/providers/build_host/test_shell_transport.py` (tests)

**Steps:**

1. Add failing tests in `test_shell_transport.py` using the existing `_RecordingTransport`:
   - `t.clone(...)` with first result `_ok(returncode=127, stderr="sh: git: not found")`
     → `CategorizedError`, `category == MISSING_DEPENDENCY`,
     `details["diagnostic"] == "ops.diagnostics --with-buildhost-agent"`.
   - first result `_ok(returncode=1, stderr="sh: git: not found")` (non-127 but not-found text)
     → `MISSING_DEPENDENCY` (backstop).
   - first result `_ok(returncode=1, stderr="permission denied")` → still
     `INFRASTRUCTURE_FAILURE` (the existing
     `test_clone_init_non_zero_is_infrastructure_failure` must stay green; add an explicit
     assertion the backstop did not fire).
2. Run the new tests; confirm they fail (today every non-zero init → infrastructure_failure).
3. Implement in `shell_transport.py`:
   - Add module constant `_BUILDHOST_AGENT_DIAGNOSTIC = "ops.diagnostics --with-buildhost-agent"`
     with a comment that the diagnostic flag is referenced as a literal (legal import direction
     is diagnostics→providers, so no import from `checks.py`).
   - Add a pure helper `_is_command_not_found(result: CommandResult) -> bool` returning
     `result.returncode == 127` OR (`"git" in stderr_lower` AND
     (`"not found" in stderr_lower` OR `"no such file" in stderr_lower`)). Compute `stderr_lower`
     from the same redacted tail the error detail uses, so no unredacted bytes are inspected.
   - In `clone`, when `init.returncode != 0`: if `_is_command_not_found(init)` raise
     `MISSING_DEPENDENCY` with the toolchain message, `details["diagnostic"]` =
     `_BUILDHOST_AGENT_DIAGNOSTIC`, and `details["stderr"]` = the redacted tail; else keep the
     existing `INFRASTRUCTURE_FAILURE` "git init failed on remote".
   - Update the `clone` docstring's Raises to add the `MISSING_DEPENDENCY` case.
4. Run focused tests + `just lint` + `just type`. Commit:
   `fix(build): point a toolchain-missing build at the build-host diagnostic`.

**Acceptance check:** the three new tests pass; the existing
`test_clone_init_non_zero_is_infrastructure_failure` and clone-ordering tests stay green; only
the command-not-found shape is reclassified.

**Rollback:** revert `shell_transport.py` + the added tests; no migration.

## Task 3 — guardrails, reviews, ship

**Steps:**

1. Run the **full** `just ci` (lint, type, lint-shell, lint-workflows, check-mermaid, doc
   guards, test). Fix everything; zero warnings. Confirm `tests/mcp/core/test_tool_docs.py`
   stays green (no tool/schema change expected).
2. Run the branch adversarial-review loop (`/challenge --base main`) and the repo
   `security-review`; address every defensible finding, committing per accepted fix.
3. Push the branch; open a PR against `main` with `Closes #627`; drive to green CI **and**
   `mergeStateStatus == CLEAN` / `mergeable == MERGEABLE`. Rebase on `main` if it moves; do not
   self-merge (orchestrator merges serially).

## Verification gaps / notes

- The real `git init` rc 127 path is exercised only under the `live_vm` gate; the unit tests
  drive the categorization via `_RecordingTransport`, which is the project's prescribed boundary
  for `ShellBuildTransport`.
- Change 2 fires for **any** build host whose transport runs `clone` (ssh + ephemeral), which is
  correct: a toolchain-missing SSH host gets the same actionable pointer. Change 1's heuristic is
  ephemeral-only because only ephemeral hosts carry `base_image_volume`.
