# Plan — early-boot console-crash guidance (#734, ADR-0227)

Derived from the hardened spec
`docs/specs/2026-06-23-early-boot-console-crash-guidance.md` and
`docs/adr/0227-early-boot-console-crash-postmortem-guidance.md`.

## Context

For an early-boot panic (crash before kdump's kexec capture kernel loads) no vmcore
is ever produced; the operator declares `expected_boot_failure = console_crash` and
the console artifact is the evidence source. Two MCP surfaces dead-end the agent:
`postmortem.triage`/`.crash` surface a bare suppressed `not_found` + `no_vmcore`, and
`vmcore.fetch` on a non-`CRASHED` System returns a null-`detail` `configuration_error`.

The change (two source files, tightly coupled — one shared narrative constant flows
from resolver-carried token to handler-built envelope) is implemented **directly in
this session** with TDD, not via parallel subagents: the tasks are sequential and
share the same two files.

Files touched:
- `src/kdive/mcp/tools/_vmcore_targets.py` (resolver: carry kind on the `NO_VMCORE`
  error, conditionally on `console_crash`; export `EXPECTED_CONSOLE_CRASH`).
- `src/kdive/mcp/tools/lifecycle/vmcore.py` (handler: console-crash redirect on the
  caught `no_vmcore`; non-`CRASHED` detail; shared narrative constant).
- `tests/mcp/_seed.py` (additive: `seed_run_on_system` accepts an optional
  `expected_boot_failure` dict).
- `tests/mcp/test_vmcore_targets.py`, `tests/mcp/lifecycle/test_vmcore_tools.py`
  (new behavior tests).

Guardrails (run before each commit): `just lint`, `just type`, and the focused
tests; `just ci` once before the first push. `ty` is whole-tree (src+tests). No
schema/migration/port/dependency change. Conventional commits, `Co-Authored-By`
trailer. No squash.

Rollback: each task is an independent commit; revert the commit. No persisted state,
no migration, so revert is clean.

## Task 1 — seed helper accepts `expected_boot_failure`

**Where it fits:** test infrastructure prerequisite for Tasks 3-4; the existing
`seed_run_on_system` cannot create a console-crash run.

**Task:** Add an optional keyword `expected_boot_failure: dict[str, Any] | None =
None` to `seed_run_on_system` (`tests/mcp/_seed.py`). When provided, pass it to the
`Run(...)` constructor's `expected_boot_failure` field (it is
`SerializedExpectedBootFailure | None`, a plain dict). Default `None` keeps every
existing caller unchanged.

**Domain-validator constraint (the seed dict must be schema-valid).** The dict is fed
straight into `Run(...)`, which validates `expected_boot_failure` against the domain
`ExpectedBootFailure` model (`src/kdive/domain/lifecycle/__init__.py`): `kind` must be
the Literal `"console_crash"`, and `pattern` is **required** (no default),
`min_length=1`/`max_length=256`, with no empty pipe-split terms / no NUL / ≤16 terms.
A missing or empty `pattern` raises a `ValidationError` at seed time (a confusing
test-infra failure, not a feature bug). Use a single shared valid console-crash dict
constant in the tests, e.g. `{"kind": "console_crash", "pattern": "Kernel panic"}`.

**Files:** `tests/mcp/_seed.py`.

**Acceptance:** existing tests still pass (`uv run python -m pytest
tests/mcp/test_vmcore_targets.py tests/mcp/lifecycle/test_vmcore_tools.py -q`); a run
seeded with `expected_boot_failure={"kind": "console_crash", "pattern": "x"}` round-
trips through `RUNS.get` with that dict. No behavior change to production code.

**Guardrails:** `just lint`, `just type`, the two focused test files.

## Task 2 — resolver carries the console-crash kind on the `NO_VMCORE` error

**Where it fits:** the propagation seam the handler reads (spec Design §1, Resolver).

**TDD:**
1. Failing test in `tests/mcp/test_vmcore_targets.py`: a `console_crash` run with no
   captured core raises the `NO_VMCORE` `CategorizedError` whose
   `details["expected_boot_failure"] == "console_crash"`. A second test: a run with
   **no** `expected_boot_failure` (the existing booted-no-core case) raises
   `NO_VMCORE` whose `details` has **no** `expected_boot_failure` key (only
   `reason`). Run; confirm both fail for the right reason (key absent / present).
2. Implement: add a module constant `EXPECTED_CONSOLE_CRASH = "expected_console_crash"`
   next to the existing reason tokens. In `resolve_run_vmcore_target`, at the
   `vmcore_ref is None` branch, read the run's declared kind
   (`run.expected_boot_failure.get("kind")` when the dict is present) and raise via a
   `_precondition` variant that attaches `expected_boot_failure: "console_crash"` to
   `details` **only when the kind is exactly `"console_crash"`**; otherwise raise the
   existing bare `NO_VMCORE`. Keep `NO_DEBUGINFO`/`NO_BUILD`/absent-Run unchanged.
3. Re-run; green. Keep `_precondition_not_found(reason)` working for the other two
   reasons (add an optional second arg or a sibling helper — do not break the
   existing signature's callers).

**Files:** `src/kdive/mcp/tools/_vmcore_targets.py`,
`tests/mcp/test_vmcore_targets.py`.

**Acceptance:** the `console_crash`-no-core path carries
`details["expected_boot_failure"] == "console_crash"`; every other no-core path's
`details` has no such key; `vmcore_target_failure` for a non-console-crash `no_vmcore`
still yields `data == {"reason": "no_vmcore"}` exactly (assert no extra key — pins the
`safe_error_details` fall-through invariant). `NO_DEBUGINFO`/`NO_BUILD` reasons and the
absent-Run no-reason miss are unchanged.

**Guardrails:** `just lint`, `just type`, `tests/mcp/test_vmcore_targets.py`.

## Task 3 — handler redirect + shared narrative constant

**Where it fits:** the user-facing surface (spec Design §1, Handler).

**TDD:**
1. Failing test in `tests/mcp/lifecycle/test_vmcore_tools.py`: a `console_crash` run
   with no captured core, triaged via `postmortem_triage` (and a second via
   `postmortem_crash`), returns `error_category == "configuration_error"`,
   `data["reason"] == "expected_console_crash"`, `data["expected_boot_failure"] ==
   "console_crash"`, `suggested_next_actions == ["runs.get", "artifacts.list"]`, and
   `detail == <the named narrative constant>`. Assert the constant contains `"kexec"`
   and `"console"`. Add a regression test: a run with **no** `expected_boot_failure`
   and no core still returns the unchanged `not_found` + `data["reason"] ==
   "no_vmcore"` + `suggested_next_actions == ["vmcore.fetch", "runs.get"]`, and
   `"expected_boot_failure" not in data`. Run; confirm failure.
2. Implement: add a module-level narrative constant in
   `mcp/tools/lifecycle/vmcore.py` (one shared string; mentions early-boot crash
   before kexec and that the console artifact, reachable via `runs.get`, is the
   evidence source). In `_postmortem_crash`'s `except CategorizedError as exc`
   handler, before returning `vmcore_target_failure`, branch: when
   `exc.details.get("reason") == NO_VMCORE and exc.details.get(
   "expected_boot_failure") == "console_crash"`, return
   `_config_error(run_id, detail=<constant>, data={"reason": EXPECTED_CONSOLE_CRASH,
   "expected_boot_failure": "console_crash"})` with
   `suggested_next_actions=["runs.get", "artifacts.list"]` (use the `config_error`
   helper that accepts `data` + `detail`; note `config_error` does not take
   `suggested_next_actions`, so build the envelope via the helper that does, or set
   actions on the returned response — match the existing pattern; verify against
   `_common.config_error`'s signature). Import `NO_VMCORE`/`EXPECTED_CONSOLE_CRASH`
   from `_vmcore_targets`.
3. Re-run focused tests; green. Confirm `_postmortem_triage`'s `if resp.status ==
   "error": return resp` passes the redirect straight through with its own actions.

**Files:** `src/kdive/mcp/tools/lifecycle/vmcore.py`,
`tests/mcp/lifecycle/test_vmcore_tools.py`.

**Implementation note (verify, do not assume):** `_common.config_error(object_id, *,
detail, data)` returns a `configuration_error` `ToolResponse` but takes **no**
`suggested_next_actions`. Either (a) `model_copy(update={"suggested_next_actions":
[...]})` the result, or (b) build directly with
`ToolResponse.failure(run_id, ErrorCategory.CONFIGURATION_ERROR, detail=..., data=...,
suggested_next_actions=[...])`. Prefer (b) for one explicit construction; confirm
`ErrorCategory.CONFIGURATION_ERROR` is not suppressed (it is not), so `detail`
survives.

**Acceptance:** matches spec Acceptance bullets 1, 2, 3 for the postmortem surfaces. A
non-viewer still raises `AuthorizationError` (the redirect is unreachable via the
authz path) — assert with the existing `_ctx(role=None)` pattern if a test does not
already cover it for triage.

**Guardrails:** `just lint`, `just type`,
`tests/mcp/lifecycle/test_vmcore_tools.py`.

## Task 4 — `vmcore.fetch` non-`CRASHED` detail

**Where it fits:** spec Design §2; independent of Tasks 2-3 but same file as Task 3.

**TDD:**
1. Extend `test_fetch_vmcore_non_crashed_is_config_error` (or add a sibling) to assert
   the response `detail` is non-null and names the required CRASHED state plus the
   current state (assert a stable substring, e.g. `"CRASHED"` and the state token),
   while `data["current_status"]` is unchanged. Run; confirm the current null-`detail`
   fails it.
2. Implement: in `_fetch_vmcore`, the `if system.state is not SystemState.CRASHED:`
   branch passes a fixed-template `detail` to `_config_error` (e.g. `f"system must be
   in CRASHED state to capture a vmcore; current state = {system.state.value}"`).
   `data={"current_status": system.state.value}` unchanged.
3. Re-run; green.

**Files:** `src/kdive/mcp/tools/lifecycle/vmcore.py`,
`tests/mcp/lifecycle/test_vmcore_tools.py`.

**Acceptance:** spec Acceptance bullet 4 — non-`CRASHED` `vmcore.fetch` returns
`configuration_error` with non-null `detail` and unchanged `data.current_status`.

**Guardrails:** `just lint`, `just type`,
`tests/mcp/lifecycle/test_vmcore_tools.py`.

## Task 5 — full guardrails, branch review, ship

1. Run the full `just ci` once; fix any doc/snapshot/architecture-test fallout. (No
   tool-reference or config-doc regen expected — no new tool, no config change — but
   confirm `docs-check` is clean since `suggested_next_actions` are not part of the
   generated reference.)
2. Run the branch review loop (`/challenge --base main`) and the `security-review`
   skill; address findings.
3. Push, open the PR (`Closes #734`), drive to green + `CLEAN`/`MERGEABLE`. STOP at
   hand-off — the orchestrator merges last (after #735).

## Verification gaps / notes

- No KVM/live path here; everything is unit-testable against the migrated DB pool
  (the existing test pattern). No `live_vm`/`live_stack` gating involved.
- `#735` adds `refs.console` to `runs.get`; this plan does **not** depend on it. The
  narrative points at `runs.get` conceptually. If #735 has already landed when the
  orchestrator rebases, no change is needed here.
- The only cross-file coupling is the `NO_VMCORE` / `EXPECTED_CONSOLE_CRASH` token
  shared between `_vmcore_targets.py` and `vmcore.py`; Task 2 lands the export before
  Task 3 imports it.
