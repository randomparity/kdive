# Failed-boot crash signature on `runs.get`

Goal: a failed boot's `runs.get` `data.boot_readiness` carries the crash signature the readiness
scan matched and a one-line `detail` (#2691,
[spec](../specs/2026-09-23-boot-readiness-crash-signature-design.md)).

Architecture: the local-libvirt readiness scan returns the matched literal on `ReadinessResult`;
the booter puts it in `CategorizedError.details`, which the worker already persists as
`failure_context["failure_detail_crash_signature"]`; `failed_boot_attempt` reads it back and
`_boot_readiness_data` renders it with `detail`. Python 3.14, uv, psycopg 3; no dependency or
schema change.

Expected implementation size: 170–230 changed lines (M) — about 30 provider lines, 25 service
lines, 30 read-model lines, 10 docstring and generated-reference lines, 80–130 test lines.

## Global Constraints

- Additive only: existing `boot_readiness` keys and values, `classify_console`'s return value,
  and `ReadinessResult`'s first three fields keep their meaning.
- `expected_crash_matched` stays `False` whenever an expectation is declared (ADR-0413).
- No new crash patterns in `crash_signatures._CRASH_SIGNATURE`.
- Lines ≤ 100 chars; `just lint`, `just type`, `just docs-check` green with zero warnings.
- Plain prose: no "critical", "robust", "comprehensive", "elegant".

## File map

- `src/kdive/providers/local_libvirt/lifecycle/boot/readiness.py` — owns console classification.
  Adds `crash_signature` to `ReadinessResult`, a private `_scan_result`, and a
  `crash_signature` keyword on `_verdict_to_result`. Callers: `LocalExternalBootReadiness`,
  `_real_readiness` (same file).
- `src/kdive/providers/local_libvirt/lifecycle/install.py` — `_await_ready` /
  `_boot_failure_details` add `details["crash_signature"]`.
- `src/kdive/domain/lifecycle/crash_signatures.py` — adds `is_crash_signature`.
- `src/kdive/services/runs/steps.py` — `BootAttempt.observed_crash_signature`; read in
  `failed_boot_attempt`.
- `src/kdive/mcp/tools/lifecycle/runs/common.py` — `_boot_readiness_data` adds `detail`.
- `src/kdive/mcp/tools/lifecycle/runs/registrar.py` — `runs.get` wrapper docstring; regenerate
  `docs/guide/reference/runs.md` with `just docs`.
- Tests: `tests/providers/local_libvirt/test_install.py`,
  `tests/providers/local_libvirt/lifecycle/boot/test_session_mechanisms.py`,
  `tests/mcp/lifecycle/test_runs_tools.py`.

No ownership transition: each change extends the current owner.

## Task 1 — Provider: carry the signature out of readiness

Interfaces produced:
`ReadinessResult(answered: bool, ok: bool, probe_error: ProbeFailure | None = None,
crash_signature: str | None = None)`;
`LocalLibvirtBooter._boot_failure_details(system_id: UUID, first_probe_error: ProbeFailure |
None, crash_signature: str | None = None) -> dict[str, object]`.

Verification:
- Contract: the console scan sets `crash_signature` on the Run booter's probe. Mode:
  focused-test. In `test_install.py`: `test_scan_result_crashed_carries_signature` (UBSAN line →
  `ReadinessResult(True, False, None, "UBSAN:")`), `test_scan_result_ready_and_pending_carry_none`
  (marker → ok result with `None`; pending running → `None`; pending exited → `None` signature),
  and `test_real_readiness_crash_reports_signature` (monkeypatch `readiness_mod.read_console_log`
  to return the UBSAN line, as the existing `_real_readiness` tests do; expect
  `.crash_signature == "UBSAN:"`). Red: `ImportError` on `_scan_result`. Green: `uv run pytest
  tests/providers/local_libvirt/test_install.py -k "scan_result or real_readiness" -q`.
- Contract: the external-boot probe keeps its verdicts and now carries the literal. Mode:
  focused-test. The two updated tests in Step 4. Green: `uv run pytest
  tests/providers/local_libvirt/lifecycle/boot/test_session_mechanisms.py -q`.
- Contract: the booter puts the signature in `details` and the worker persists it. Mode:
  focused-test. Test `test_boot_readiness_failure_carries_crash_signature` in `test_install.py`:
  `_Readiness(answered=True, ok=False, crash_signature="UBSAN:")`; expect
  `details["crash_signature"] == "UBSAN:"` and
  `_failure_context(err, SecretRegistry())["failure_detail_crash_signature"] == "UBSAN:"`; and
  `test_boot_timeout_has_no_crash_signature`: `answered=False`; expect no `crash_signature` key.
  Red: `KeyError`. Green: `uv run pytest tests/providers/local_libvirt/test_install.py -k
  "boot_readiness_failure or boot_timeout or verdict_to_result" -q`.

Steps:
1. Add the tests above; add a `crash_signature: str | None = None` field to the `_Readiness`
   fake and pass it into `ReadinessResult`. Run; expect red.
2. In `readiness.py` add the trailing `ReadinessResult` field; extend `_verdict_to_result`:

   ```python
   def _verdict_to_result(
       verdict: ConsoleVerdict, *, exited: bool, crash_signature: str | None = None
   ) -> ReadinessResult | None:
       if verdict is ConsoleVerdict.READY:
           return ReadinessResult(answered=True, ok=True)
       if verdict is ConsoleVerdict.CRASHED:
           return ReadinessResult(answered=True, ok=False, crash_signature=crash_signature)
       if exited:
           return ReadinessResult(answered=True, ok=False)
       return None
   ```

   Split `classify_console` so the signature is kept:

   ```python
   def _scan_console(data: bytes, marker: str) -> tuple[ConsoleVerdict, str | None]:
       text = data.decode("utf-8", errors="replace")
       marker_re = re.compile(rf"(?:^|[^\S\n]){re.escape(marker)}[^\S\n]*$", re.MULTILINE)
       marker_match = marker_re.search(text)
       region = text if marker_match is None else text[: marker_match.start()]
       crash = first_crash_signature(region)
       if crash is not None:
           return ConsoleVerdict.CRASHED, crash.group(0)
       return (ConsoleVerdict.READY if marker_match is not None else ConsoleVerdict.PENDING), None


   def classify_console(data: bytes, *, marker: str = _READINESS_MARKER) -> ConsoleVerdict:
       """Classify a console capture as ready, crashed, or pending."""
       return _scan_console(data, marker)[0]


   def _scan_result(data: bytes, *, exited: bool) -> ReadinessResult | None:
       verdict, signature = _scan_console(data, _READINESS_MARKER)
       return _verdict_to_result(verdict, exited=exited, crash_signature=signature)
   ```

   Replace `_verdict_to_result(classify_console(X), exited=E)` with `_scan_result(X, exited=E)`
   in `LocalExternalBootReadiness.__call__` (both reads) and `_real_readiness` (both reads).
3. In `install.py`, pass `result.crash_signature` from the answered branch of `_await_ready`;
   `_boot_failure_details` adds `details["crash_signature"] = crash_signature` when not `None`.
4. Update `test_prepared_window_discards_prior_marker_before_new_crash` and
   `test_external_boot_readiness_terminal_domain_gets_one_final_read` to expect
   `ReadinessResult(True, False, None, "Kernel panic")`. Run the green commands; expect pass.
5. Commit `feat(local-libvirt): carry the readiness crash signature into boot failure details`.

## Task 2 — Read path: surface `observed_crash_signature` and `detail`

Interfaces consumed: the `failure_detail_crash_signature` job `failure_context` key from Task 1.
Produced: `is_crash_signature(text: str) -> bool`;
`BootAttempt(job_id, error_category, observed_crash_signature: str | None = None)`.

Verification:
- Contract: `failed_boot_attempt` reads a valid signature and drops any other value. Mode:
  focused-test. Extend `_seed_boot_job` with `failure_context: dict[str, str] | None = None`
  (inserted into the `failure_context` column). Tests
  `test_failed_boot_attempt_reads_crash_signature` (`"UBSAN:"` → `"UBSAN:"`) and
  `test_failed_boot_attempt_drops_unknown_crash_signature` (`"rm -rf"` → `None`). Red:
  `AttributeError`. Green: `uv run pytest tests/mcp/lifecycle/test_runs_tools.py -k
  "failed_boot_attempt or boot_readiness or readiness_failure or declared_panic or detail" -q`.
- Contract: `runs.get` renders signature and `detail` per the spec table. Mode: focused-test.
  `test_get_run_declared_panic_with_ubsan_signature` (declared `panic`, `readiness_failure`,
  signature `UBSAN:`) expects `observed_crash_signature == "UBSAN:"`, `detail ==` the crash
  sentence plus the declared `panic` clause, `expected_crash_matched is False`;
  `test_get_run_declared_panic_silent_timeout` (`boot_timeout`, no context) expects `None` and
  the timeout sentence plus the `panic` clause;
  `test_envelope_for_run_boot_failure_detail_without_signature` parametrizes the
  `readiness_failure` and `null`-category sentences with no expectation (no clause);
  `test_envelope_for_run_boot_failure_detail_names_console_crash_pattern` declares
  `{"kind": "console_crash", "pattern": "my oops"}` and expects the clause to name `my oops`.
  Update `test_get_run_surfaces_failed_boot_attempt`, `test_failed_boot_attempt_surfaces_failed_job`
  and `test_failed_boot_attempt_null_category` for the added keys. The unchanged success path: new
  `test_get_run_expected_crash_observed_has_no_boot_readiness` inserts a succeeded boot step with
  `{"boot_outcome": "expected_crash_observed"}` plus a failed boot job carrying a signature and
  asserts `"boot_readiness" not in resp.data`; add `or expected_crash_observed` to the green
  command. The boot handler's expected-crash branch (catches the booter's `CategorizedError`) is
  gated by `uv run pytest tests/jobs/handlers/test_runs_boot.py -k expected_crash -q`.
- Contract: agent-facing docstring and generated reference name both fields. Mode:
  focused-test. `just docs-check` fails red if the reference is stale; green after `just docs`.

Steps:
1. Write the tests above; run; expect red.
2. `crash_signatures.py`:

   ```python
   def is_crash_signature(text: str) -> bool:
       """True iff ``text`` is exactly a literal ``first_crash_signature`` matches (#2691)."""
       return _CRASH_SIGNATURE.fullmatch(text) is not None
   ```

3. `steps.py`: add the `BootAttempt` field and `"observed_crash_signature"` to `as_data()`;
   in `failed_boot_attempt`:

   ```python
   value = job.failure_context.get("failure_detail_crash_signature")
   signature = value if isinstance(value, str) and is_crash_signature(value) else None
   return BootAttempt(job.id, job.error_category, observed_crash_signature=signature)
   ```

4. `common.py`: `_boot_readiness_data` sets `data["detail"] = _boot_failure_detail(run,
   boot_readiness)`; `_boot_failure_detail` returns the spec table's sentence and, when the Run
   declared an expectation, appends ``; the declared `K` crash was not recorded as matched``
   with `K` = the `pattern` string for kind `console_crash`, else the `kind` string; no clause
   when that value is not a `str`.
5. `registrar.py`: extend the Boot-failure paragraph of the `runs.get` docstring with
   `observed_crash_signature` and `detail`; run `just docs`.
6. Run the green commands, `just lint`, `just type`; commit
   `feat(runs): surface the observed crash signature on boot_readiness`.
