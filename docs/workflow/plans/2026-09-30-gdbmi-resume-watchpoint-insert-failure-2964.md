# Resume-time watchpoint insert failure and step-verb stalls (#2964) — plan

Goal: a resume that gdb rejects with `Could not insert hardware watchpoint N.` raises
`debug_attach_failure` with `data.code` `watchpoint_insert_failed`; step-verb stall and timeout
results stay as they are and are pinned by tests.

Architecture: `commands/watchpoints.py` owns the watchpoint text match and adds
`watchpoint_insert_failure`; `core/execution.py` `resume` calls it when its resume command
raises. Spec: `docs/workflow/specs/2026-09-30-gdbmi-resume-watchpoint-insert-failure-2964-design.md`;
ADR 0712 amendment of 2026-09-30.

Tech stack: Python 3.14, pytest.

Expected implementation size: 90–140 changed lines (M) — one helper and regex (~35), a
five-line wrap in `resume`, two new tests and two extended tests (~80).

## Global Constraints

- No new dependency. No x86 result change except the added `code`, `verb`, `watchpoint`
  details and the new message on the resume-time insert failure (operator decision).
- `-break-watch` classification and `transport_stall` for a real stall do not change.
- Guardrails: `just lint`, `just type`, `just test-changed`, `just records` (after
  `git fetch origin main`). macOS: `export PATH="/opt/homebrew/opt/coreutils/libexec/gnubin:/opt/homebrew/bin:$PATH"`.

## File map

| File | Change | Owns after |
|---|---|---|
| `src/kdive/providers/shared/debug_common/gdbmi/commands/watchpoints.py` | modify | `_INSERT_FAILED_RE`, `watchpoint_insert_failure` |
| `src/kdive/providers/shared/debug_common/gdbmi/core/execution.py` | modify | `resume` maps the resume `^error` through `watchpoint_insert_failure` |
| `tests/providers/local_libvirt/test_debug_gdbmi.py` | modify | the tests below |

No caller migration: `continue_`, `step`, `next`, `step_instruction`, `finish` in
`core/engine.py` already call `ExecutionControl.resume`.

## Task 1 — classify the resume-time insert failure

Interfaces: consumes `CategorizedError`, `ErrorCategory` (`kdive.domain.errors`), and
`GdbMiEngine.execute_mi_command`, which raises `DEBUG_ATTACH_FAILURE` with details
`{"command": <verb>, "payload": <redacted ^error payload dict>}`. Provides
`watchpoint_insert_failure(exc: CategorizedError, verb: str) -> CategorizedError | None`.

Verification:
- Contract: insert failure on resume is coded. Mode: focused-test.
  `test_resume_classifies_watchpoint_insert_failure` (parametrized over `-exec-continue` via
  `continue_` and `-exec-step-instruction` via `step_instruction`): a `_FakeMiController` whose
  resume verb returns `[{"type": "result", "message": "error", "payload": {"msg":
  "Warning:\nCould not insert hardware watchpoint 2.\nCould not insert hardware breakpoints:\nYou may have requested too many hardware breakpoints/watchpoints.\n"}}]`.
  Expect category `DEBUG_ATTACH_FAILURE`, details `code == "watchpoint_insert_failed"`,
  `verb`, `watchpoint == "2"`, `command` kept; message contains `debug.clear_watchpoint`; no
  `-exec-interrupt` written. Red: `KeyError: 'code'`. Green:
  `just test-verbose tests/providers/local_libvirt/test_debug_gdbmi.py -k watchpoint_insert`.
- Contract: other resume errors pass through. Mode: focused-test. Extend the existing
  `test_step_raises_on_missing_function_bounds` (msg `Cannot find bounds of current function`
  on `-exec-step`) to assert `str(exc.value) == "gdb/MI command failed: -exec-step"` and
  `"code" not in exc.value.details`. Red (controlled fault): make the helper match any msg;
  see the test fail; revert. Green: same command with `-k missing_function_bounds`.

Steps:
1. Add the tests above; run them; see the first red.
2. In `watchpoints.py` add:

```python
_INSERT_FAILED_RE = re.compile(r"Could not insert hardware watchpoint (\d+)\.")


def watchpoint_insert_failure(exc: CategorizedError, verb: str) -> CategorizedError | None:
    """Code a resume ``^error`` for a watchpoint gdb could not insert, else ``None``.

    The same text comes from a stub that cannot insert one and from exhausted debug registers,
    so the code names the failed insert, not a missing capability (ADR 0712, 2026-09-30).
    """
    if exc.category is not ErrorCategory.DEBUG_ATTACH_FAILURE:
        return None
    payload = exc.details.get("payload")
    msg = payload.get("msg") if isinstance(payload, dict) else None
    match = _INSERT_FAILED_RE.search(msg) if isinstance(msg, str) else None
    if match is None:
        return None
    number = match.group(1)
    return CategorizedError(
        f"gdb/MI could not insert hardware watchpoint {number} on resume: the target cannot "
        "insert a hardware watchpoint, or too many are armed; remove one with "
        "debug.clear_watchpoint (see debug.list_watchpoints), then retry",
        category=ErrorCategory.DEBUG_ATTACH_FAILURE,
        details={
            **exc.details,
            "code": "watchpoint_insert_failed",
            "verb": verb,
            "watchpoint": number,
        },
    )
```

3. In `execution.py` import it and replace `resumed = self._engine.execute_mi_command(attachment, verb)` with:

```python
        try:
            resumed = self._engine.execute_mi_command(attachment, verb)
        except CategorizedError as exc:
            insert_failure = watchpoint_insert_failure(exc, verb)
            if insert_failure is None:
                raise
            raise insert_failure from exc
```

4. Run the focused green command; expect all pass. Run `just lint` and `just type`; expect exit 0.
5. Commit `fix(debug): code resume-time watchpoint insert failures (#2964)`.

## Task 2 — pin the step-verb results

Interfaces: `GdbMiEngine.step_instruction`, `GdbMiEngine.next` (`core/engine.py`); no source change.

Verification:
- Contract: a step verb whose interrupt gets no stop keeps `transport_stall`. Mode: focused-test.
  `test_step_verb_without_interrupt_stop_keeps_transport_stall` (parametrized
  `step` / `-exec-step`, `step_instruction` / `-exec-step-instruction`, `next` / `-exec-next`): verb replies
  `^running`, no reads. Expect `INFRASTRUCTURE_FAILURE`, `code == "transport_stall"`, `verb`.
  Red (controlled fault): temporarily change the stall code in `execution.py` and see it fail;
  revert. Green: `just test-verbose tests/providers/local_libvirt/test_debug_gdbmi.py -k step_verb`.
- Contract: a step verb whose interrupt gets a stop keeps `timed_out: True`. Mode: focused-test.
  Extend the existing `test_step_interrupts_on_timeout` parametrization with
  `("step_instruction", "-exec-step-instruction")` and assert `stop.reason ==
  "signal-received"`. Red (controlled fault): drop the `timed_out` update in `resume`
  temporarily; revert. Green: `just test-verbose tests/providers/local_libvirt/test_debug_gdbmi.py -k step_interrupts`.

Steps:
1. Add the new test and the extension; run both green commands; expect pass.
2. Apply each controlled fault, see red, revert, rerun green.
3. Commit `test(debug): pin step-verb stall and timeout results (#2964)`.
