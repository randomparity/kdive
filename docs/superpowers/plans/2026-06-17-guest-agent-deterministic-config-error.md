# Plan — Guest-agent deterministic failure classified as configuration_error (#531)

- **Spec:** [../../specs/2026-06-17-guest-agent-deterministic-config-error.md](../../specs/2026-06-17-guest-agent-deterministic-config-error.md)
- **ADR:** [../../adr/0158-guest-agent-deterministic-failure-classification.md](../../adr/0158-guest-agent-deterministic-failure-classification.md)
- **Issue:** [#531](https://github.com/randomparity/kdive/issues/531)
- **Branch:** `fix/guest-agent-deterministic-config-error-531`

## Overview

One behavior change in one file: `GuestAgentExec._agent`
(`src/kdive/providers/remote_libvirt/guest/agent.py`) currently maps **every** caught
`libvirt.libvirtError` to `TRANSPORT_FAILURE` (`retryable=true`). Subcategorize by
`exc.get_error_code()`: a deterministic code → `CONFIGURATION_ERROR` (`retryable=false`);
everything else (incl. a bare error whose `get_error_code()` is `None`) → `TRANSPORT_FAILURE`
(unchanged). Add `libvirt_error` (the error string) and `libvirt_error_code` to `details` on
both branches so the distinction is auditable.

This is a single tightly-scoped change. It is implemented directly in this session with TDD,
not split across implementer subagents.

## Conventions and guardrails (apply to every commit)

- Python 3.13, `uv`. Absolute imports only. Ruff line length 100, lint set `E,F,I,UP,B,SIM`.
  `ty` strict. Google-style docstrings on non-trivial public APIs.
- Pick the most specific existing `ErrorCategory`; never invent strings. Both
  `CONFIGURATION_ERROR` and `TRANSPORT_FAILURE` already exist.
- Guardrail commands (run before every commit):
  - `just lint` — ruff check + format check
  - `just type` — `ty` whole-tree (src + tests)
  - `uv run python -m pytest tests/providers/remote_libvirt/guest/test_guest_agent.py -q`
    (focused) then `just test` (full non-live suite) before the final push.
- Conventional-commit subject ≤72 chars, imperative; end every commit with
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- Doc-style: plain prose; never "critical/robust/comprehensive/elegant"; "Milestone" not
  "Sprint" (applies to code comments too).

## Task 1 — Failing tests for the new classification (TDD red)

**Where it fits:** the spec's "Test plan (TDD)". Write the tests first; confirm they fail for
the right reason (current code raises `TRANSPORT_FAILURE` for the deterministic codes and
omits the new `details` keys).

**File:** `tests/providers/remote_libvirt/guest/test_guest_agent.py`

**Add:**

1. Import the `libvirt_error(code)` helper from the provider conftest
   (`tests/providers/remote_libvirt/conftest.py`) — it builds a real `libvirt.libvirtError`
   whose `get_error_code()` returns the chosen code. Confirm it is importable from the test's
   location (the conftest is a sibling-of-parent; if it is not auto-importable as a fixture,
   import it as `from tests.providers.remote_libvirt.conftest import libvirt_error`, matching
   how sibling provider tests use it).
2. A helper that builds a `GuestAgentExec` whose `agent_command` raises a supplied exception
   on the first call (mirror the existing `boom` closure in
   `test_agent_unreachable_maps_to_transport_failure`, but parametrized over the exception).
3. **Parametrized deterministic test:** for each code in
   `{VIR_ERR_ARGUMENT_UNSUPPORTED, VIR_ERR_ACCESS_DENIED, VIR_ERR_OPERATION_DENIED,
   VIR_ERR_NO_SUPPORT, VIR_ERR_OPERATION_UNSUPPORTED, VIR_ERR_CONFIG_UNSUPPORTED}`, raising
   `libvirt_error(code)` from the agent command makes `run()` raise
   `CategorizedError` with `category is ErrorCategory.CONFIGURATION_ERROR`. Also assert the
   raised error's `details` contains `libvirt_error` (a non-empty str), `libvirt_error_code ==
   code`, and `domain`.
4. **Transient/coded test:** `libvirt_error(VIR_ERR_AGENT_UNRESPONSIVE)` (and
   `VIR_ERR_AGENT_COMMAND_TIMEOUT`, `VIR_ERR_AGENT_UNSYNCED`) → `TRANSPORT_FAILURE`, with
   `libvirt_error_code` set to that code in `details`.
5. **No-code transient test:** extend / keep
   `test_agent_unreachable_maps_to_transport_failure` (bare
   `libvirt.libvirtError("guest agent is not connected")`) → `TRANSPORT_FAILURE`. Assert
   `details["libvirt_error"]` is the message string and `details["libvirt_error_code"]` is
   `None`.
6. Do **not** change the existing timeout test
   (`test_run_times_out_when_the_command_never_exits`) — `_await_exit`'s in-guest timeout is a
   separate raise site and stays `TRANSPORT_FAILURE`. It must remain green untouched.

**Acceptance criteria a reviewer can check:**
- The deterministic-code tests assert `CONFIGURATION_ERROR`; they fail against current `HEAD`
  (which raises `TRANSPORT_FAILURE`).
- The `details`-payload assertions fail against current `HEAD` (no `libvirt_error*` keys
  today).
- Run `uv run python -m pytest tests/providers/remote_libvirt/guest/test_guest_agent.py -q`
  and confirm the new tests fail for exactly those reasons (category mismatch / missing key),
  not an import or collection error.

**Rollback/cleanup:** none — additive test code.

## Task 2 — Subcategorize at the raise site (TDD green)

**Where it fits:** spec Design items 1–3.

**File:** `src/kdive/providers/remote_libvirt/guest/agent.py`

**Change `_agent`'s `except libvirt.libvirtError` branch (currently lines 207–215):**

1. Add a module-level constant near the other module constants (after `_DEFAULT_POLL_S`):
   ```python
   _DETERMINISTIC_CONFIG_CODES: frozenset[int] = frozenset({
       libvirt.VIR_ERR_ARGUMENT_UNSUPPORTED,
       libvirt.VIR_ERR_ACCESS_DENIED,
       libvirt.VIR_ERR_OPERATION_DENIED,
       libvirt.VIR_ERR_NO_SUPPORT,
       libvirt.VIR_ERR_OPERATION_UNSUPPORTED,
       libvirt.VIR_ERR_CONFIG_UNSUPPORTED,
   })
   ```
   Add a short comment citing ADR-0158 and naming the "agent not configured / permission
   denied / unsupported" intent.
2. In the `except` body, read `code = exc.get_error_code()` and build a shared `details` dict:
   `{"domain": _domain_name(domain), "libvirt_error": str(exc), "libvirt_error_code": code}`.
3. Branch: if `code in _DETERMINISTIC_CONFIG_CODES`, raise
   `CategorizedError(<config message>, category=ErrorCategory.CONFIGURATION_ERROR,
   details=details)`. Else raise `CategorizedError(<existing transient message>,
   category=ErrorCategory.TRANSPORT_FAILURE, details=details)`.
4. The config message names a build-host/agent configuration problem (e.g.
   `"qemu-guest-agent is not usable on this build host (not configured, unsupported, or "
   "permission denied)"`); the transient message keeps the existing
   `"qemu-guest-agent command failed (agent unreachable or not connected)"`.
5. Update the `run()` docstring's `Raises:` block to note that a deterministic libvirt error
   now raises `CONFIGURATION_ERROR` while a transient/unknown one stays `TRANSPORT_FAILURE`.
   Keep ≤100 lines/function and cyclomatic complexity ≤8 (this adds one branch).

**Type note:** `exc.get_error_code()` is typed via the stubless libvirt binding; the value is
`int | None` at runtime. `_DETERMINISTIC_CONFIG_CODES` is `frozenset[int]`; `None in
frozenset[int]` is valid and returns `False`, so no guard is needed. If `ty` flags the
membership type, narrow with an explicit `code is not None and code in
_DETERMINISTIC_CONFIG_CODES`, which keeps the same behavior.

**Acceptance criteria:**
- `uv run python -m pytest tests/providers/remote_libvirt/guest/test_guest_agent.py -q` is
  green, including the new tests and the untouched timeout/allowlist/malformed-reply tests.
- `just lint` and `just type` are clean (zero warnings).

**Rollback/cleanup:** revert the single-file change; no schema/state to undo.

## Task 3 — Full guardrails before push

1. `just lint`, `just type` clean.
2. **Regression check for coded-error tests (do this before implementing Task 2).** The only
   way an existing test breaks is if it raises a *coded* `libvirt_error(code)` through the real
   `GuestAgentExec._agent` path (install / build-VM / artifact-channel wrappers) and asserts
   `TRANSPORT_FAILURE` for a code that now lands in the deterministic set. Run:
   ```
   rg -n "libvirt_error\(libvirt\.VIR_ERR_(ARGUMENT_UNSUPPORTED|ACCESS_DENIED|OPERATION_DENIED|NO_SUPPORT|OPERATION_UNSUPPORTED|CONFIG_UNSUPPORTED)\)" tests/
   ```
   At the current `HEAD` this returns **no matches**: existing coded guest-agent tests use
   `VIR_ERR_AGENT_UNRESPONSIVE` (86), `VIR_ERR_OPERATION_FAILED` (55), and
   `VIR_ERR_INTERNAL_ERROR` (1) — all outside the deterministic set, so they keep
   `TRANSPORT_FAILURE` and stay green (verified: `test_install.py:286,329,335,367`,
   `test_build_vm.py:345`). If a future rebase introduces a match, that test must be reasoned
   about explicitly, not auto-updated. `OPERATION_FAILED` and `OPERATION_INVALID` are
   deliberately **not** deterministic (transient/idempotency codes); only `OPERATION_DENIED`
   (permission) is.
3. `just test` (full non-live suite) green — final confirmation across all callers.
4. The bash-backed doc guards (`docs-links`, `docs-paths`, `docs-check`, `check-mermaid`,
   `config-*`) require bash ≥4 and node deps; they may not run on a bash-3.2 host. Verify the
   new relative doc links resolve by hand and confirm `just adr-status-check` passes locally;
   CI runs the full set on Ubuntu.

**Acceptance criteria:** every locally-runnable guardrail green; any guard that cannot run
locally is noted in the PR body with the reason.

## Verification of done

- A deterministic guest-agent libvirt error yields `configuration_error` /
  `retryable=false`; a transient/no-code one yields `transport_failure` / `retryable=true`
  (asserted by tests).
- `details` carries `libvirt_error` + `libvirt_error_code` on both branches.
- No new category, field, column, or migration; the timeout and allowlist raise sites are
  unchanged.
