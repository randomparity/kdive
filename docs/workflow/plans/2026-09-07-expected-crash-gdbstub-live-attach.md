# Expected-crash gdbstub live attach — implementation plan

Goal: make a reachable gdbstub usable on a Run that declared `expected_boot_failure`, by probing
the stub when the boot records `expected_crash_observed` and admitting the gdbstub transport
against the probed result.

Architecture: the boot worker (`src/kdive/jobs/handlers/runs/`) records a `boot` `run_steps`
result carrying `available_capture` and `inert_capture`; the MCP debug plane
(`src/kdive/mcp/tools/debug/sessions/lifecycle.py`) reads that succeeded boot result to decide
whether a live attach is admissible; `src/kdive/mcp/tools/lifecycle/runs/common.py` renders both
lists into the `runs.get` envelope. This change adds one bounded probe on the worker side and
keys the gate on what that probe recorded.

Tech stack: Python 3.14, `uv`, `pytest`, `ruff`, `ty`.

Expected implementation size: 190–250 changed lines (M) — derived from the file map and the three
task bodies below: roughly 60 source lines across five files and 140 test lines across three.

## Global Constraints

Transcribed from `AGENTS.md` and the frozen scope record:

- Ruff line length 100; lint set `E,F,I,UP,B,SIM`. `ty` runs whole-tree (`src` **and** `tests`).
- Guardrails `just lint`, `just type`, `just test`, `just ci` run bare — never piped through
  `tail`/`head`, never with a trailing `; echo $?`. Capture with
  `just ci > <file> 2>&1 < /dev/null`. Run `just format` before each Python-only commit.
- Doc style: **Milestone**, never "Sprint"; avoid "critical", "robust", "comprehensive",
  "elegant" in ADRs, specs, commit messages, and code comments.
- The `@app.tool` wrapper docstring and `Field(description=...)` text are the agent-facing
  contract. A behavior change agents must know about lands there, not only on the inner handler.
- `scripts/guards/check_adr_status.py` fails any ADR still `Proposed` while cited from `src/` or
  `tests/`, and CI runs per commit, so the `Accepted` status and the first citation from either
  tree must land in the **same commit**
  (`docs/solutions/2026-09-04-adr-status-flip-must-share-the-first-citation-commit.md`).
  ADR-0628 is written `Accepted (2026-09-07)`; do not open it as `Proposed`.
- `docs/adr/README.md` carries no index table (ADR-0504) and the guard has no index-sync
  invariant. Do not add a row; do not edit that file.
- Out of scope, frozen: changing the recorded `boot_outcome`; reversing routing for non-gdbstub
  transports; an on-panic action knob; raising the refusal at `runs.create`; #802's
  `inert_capture_reason` disclosure work. Owned by parallel work, do not edit: `src/kdive/db/`,
  `src/kdive/jobs/worker.py`, `src/kdive/jobs/handlers/system_reclaim.py`,
  `src/kdive/providers/local_libvirt/`, `src/kdive/mcp/tools/gateway.py`.

## File map

| File | Answerable for |
|---|---|
| `src/kdive/jobs/handlers/runs/boot_evidence.py` | modified — the probe, both capture lists, the parsed-profile helper |
| `src/kdive/jobs/handlers/runs/boot.py` | modified — threads the existing `connector` into both expected-crash call sites |
| `src/kdive/mcp/tools/debug/sessions/lifecycle.py` | modified — the scoped refusal, the new detail constant, the `available_capture` reader |
| `src/kdive/mcp/tools/debug/sessions/registrar.py` | modified — the `debug.start_session` wrapper docstring |
| `src/kdive/mcp/tools/lifecycle/runs/common.py` | modified — suppress `inert_capture_reason` for an empty `inert_capture` |
| `tests/jobs/handlers/test_runs_boot.py` | modified — probe gating and capture-list contracts |
| `tests/mcp/debug/test_debug_tools.py` | modified — admission and both refusals |
| `tests/mcp/lifecycle/test_runs_tools.py` | modified — `inert_capture_reason` suppression |
| `docs/adr/0628-expected-crash-admits-a-reachable-gdbstub.md` | created — the decision (already written) |
| `docs/workflow/specs/2026-09-07-expected-crash-gdbstub-live-attach-design.md` | created — the spec (already written) |

## Task 1 — Probe the stub on the expected-crash path

The worker side; it produces the recorded evidence Task 2 reads.

**Interfaces.** Consumed from the existing codebase, each confirmed present in
`src/kdive/jobs/handlers/runs/boot_evidence.py` on this branch:
`def generic_panic_matches(redacted_console: bytes) -> bool` (line 211);
`def gdbstub_reachable(connector: Connector, system_id: UUID) -> bool` (line 249);
`def inert_capture(profile_policy: ProfilePolicy, profile: ProvisioningProfile) -> list[str]`
(line 236); `profile_policy.gdbstub_provisioned(profile) -> bool`,
`ProvisioningProfile.parse(...)`, `SYSTEMS.get(conn, system_id)`, `CaptureMethod.GDBSTUB.value`,
`CaptureMethod.CONSOLE.value`; `class ConsoleArtifact(NamedTuple)` with fields `id`,
`object_key`, `data`.

Provided to later tasks:

- `async def record_expected_crash(conn, job_ctx, run, *, system_id: UUID,
  profile_policy: ProfilePolicy, connector: Connector, artifact: ConsoleArtifact,
  matched_line: str) -> BootStepResult` — gains the keyword-only `connector`.
- `async def evaluate_expected_failure_after_ready(conn, job_ctx, run, *, system_id: UUID,
  profile_policy: ProfilePolicy, connector: Connector,
  artifact: ConsoleArtifact | None) -> BootStepResult | None` — gains the same keyword.
- `def inert_capture(profile_policy, profile, *, gdbstub_live: bool = False) -> list[str]` — the
  default keeps every existing two-argument caller and test unchanged.
- `async def _expected_crash_capture(conn, system_id, profile_policy, *, connector, panicked)
  -> tuple[list[str], list[str]]` — returns `(available_capture, inert_capture)`.
- `async def _expected_crash_profile(conn, system_id) -> ProvisioningProfile | None`.

**Verification inventory.**

- Contract: the `(available_capture, inert_capture)` pair across all four gate combinations —
  reachable stub, unreachable stub, unprovisioned stub, no generic panic. Mode: focused-test.
  Test: `tests/jobs/handlers/test_runs_boot.py`, four `test_record_expected_crash_*` cases.
  Expected red: `TypeError: record_expected_crash() got an unexpected keyword argument
  'connector'`. Green: `uv run python -m pytest tests/jobs/handlers/test_runs_boot.py -q`.
- Contract: `gdbstub_reachable` is not called when the console shows no generic panic, and not
  called when `gdbstub` is unprovisioned. Mode: focused-test. Test: same file, a connector fake
  recording every `open_transport` call, asserted empty. Expected red: the same `TypeError`.
  Green: the same command.
- Contract: `inert_capture(policy, profile)` called with no keyword still returns today's list.
  Mode: focused-test. Test: the existing `test_inert_capture_*` cases, which must stay green
  unmodified. Green: the same command.

**Steps.**

1. In `tests/jobs/handlers/test_runs_boot.py`, add a connector fake beside the existing
   fixtures, importing `CategorizedError` and `ErrorCategory` from `kdive.domain.errors` if the
   module does not already:

   ```python
   class _RecordingConnector:
       """Records open_transport calls; answers or refuses the gdbstub probe on demand."""

       def __init__(self, *, reachable: bool) -> None:
           self.reachable = reachable
           self.opened: list[tuple[str, str]] = []

       def open_transport(self, handle: object, kind: str) -> object:
           self.opened.append((str(handle), kind))
           if not self.reachable:
               raise CategorizedError(
                   "stub did not answer", category=ErrorCategory.DEBUG_ATTACH_FAILURE
               )
           return object()
   ```

2. Add the four contract cases. Each seeds a System whose profile provisions what the case
   needs, builds a `ConsoleArtifact` whose `data` does or does not contain
   `b"Kernel panic - not syncing"`, and calls `record_expected_crash` with
   `connector=_RecordingConnector(reachable=...)`. Assert: reachable →
   `available_capture == ["console", "gdbstub"]`, `"gdbstub" not in inert_capture`;
   unreachable → `available_capture == ["console"]`, `"gdbstub" in inert_capture`,
   `len(conn_fake.opened) == 1`; unprovisioned → `available_capture == ["console"]`,
   `conn_fake.opened == []`; no panic → `available_capture == ["console"]`,
   `"gdbstub" in inert_capture`, `conn_fake.opened == []`.
3. Run `uv run python -m pytest tests/jobs/handlers/test_runs_boot.py -q`. Expect the four new
   cases to fail with the `TypeError` above and every pre-existing case to pass.
4. In `boot_evidence.py`, give `inert_capture` a keyword-only `gdbstub_live: bool = False`
   parameter (after a `*`) and change its first condition to
   `if profile_policy.gdbstub_provisioned(profile) and not gdbstub_live:`. Leave the host_dump
   and `KDUMP_FAMILY` arms untouched. Add to its docstring: `` `gdbstub_live` `` is the ADR-0628
   probe result — a stub that answered on the halted guest is not inert, so it is reported in
   `available_capture` instead.
5. Split `_expected_crash_inert_capture` into `_expected_crash_profile` and
   `_expected_crash_capture`. `_expected_crash_profile` is the removed function's body verbatim —
   the `SYSTEMS.get` miss, the `ProvisioningProfile.parse` call, and the `except
   CategorizedError` branch with its unchanged `_log.warning` — returning the parsed profile, or
   `None` on either failure, instead of calling `inert_capture`. Then:

   ```python
   async def _expected_crash_capture(
       conn: AsyncConnection,
       system_id: UUID,
       profile_policy: ProfilePolicy,
       *,
       connector: Connector,
       panicked: bool,
   ) -> tuple[list[str], list[str]]:
       """Return ``(available_capture, inert_capture)`` for an expected-crash boot (ADR-0628).

       The gdbstub probe runs only on a provisioned stub whose console also shows a generic
       kernel panic. ADR-0233 established why the panic signature gates it: an RSP connect stops
       the vCPU, and a declared expectation is a caller-supplied literal that can match on a
       guest which is still running (ADR-0383's post-ready downgrade).
       """
       available = [CaptureMethod.CONSOLE.value]
       profile = await _expected_crash_profile(conn, system_id)
       if profile is None:
           return available, []
       gdbstub_live = (
           panicked
           and profile_policy.gdbstub_provisioned(profile)
           and await asyncio.to_thread(gdbstub_reachable, connector, system_id)
       )
       if gdbstub_live:
           available.append(CaptureMethod.GDBSTUB.value)
       return available, inert_capture(profile_policy, profile, gdbstub_live=gdbstub_live)
   ```

6. Add `connector: Connector` to `record_expected_crash`'s keyword-only parameters. Replace its
   `inert = await _expected_crash_inert_capture(...)` line with

   ```python
       available, inert = await _expected_crash_capture(
           conn,
           system_id,
           profile_policy,
           connector=connector,
           panicked=generic_panic_matches(artifact.data),
       )
   ```

   and change the returned dict's `"available_capture": [CaptureMethod.CONSOLE.value]` to
   `"available_capture": available`. Every other key in that dict is unchanged. Retitle its
   docstring to `"""Record ``expected_crash_observed`` with console evidence and the probed
   capture sets (ADR-0628)."""`.
7. Add `connector: Connector` to `evaluate_expected_failure_after_ready`'s keyword-only
   parameters and pass `connector=connector` in its `record_expected_crash` call.
8. In `src/kdive/jobs/handlers/runs/boot.py`, add `connector=connector,` to the
   `boot_evidence.record_expected_crash(...)` call in the `except CategorizedError` branch and
   to the `boot_evidence.evaluate_expected_failure_after_ready(...)` call on the ready path.
   Both already hold `connector` as a parameter of `_run_boot_and_capture_outcome`.
9. Update the five existing cases that name the removed helper or call the changed signature —
   `test_record_expected_crash_threads_args_and_pins_result`,
   `test_expected_crash_inert_capture_threads_args`,
   `test_expected_crash_inert_capture_omits_invalid_profile`,
   `test_record_expected_crash_degrades_when_system_gone`,
   `test_record_expected_crash_degrades_when_profile_unparseable` — to the new names and the
   `connector` keyword, keeping each case's assertion intent. The two degradation cases still
   assert `inert_capture == []`, and now also `available_capture == ["console"]`.
10. Run `uv run python -m pytest tests/jobs/handlers/test_runs_boot.py -q`. Expect every case in
    the file to pass. Then `just format`, `just lint`, `just type`: `All checks passed!` from
    each.

**Acceptance criteria.** `record_expected_crash` probes exactly once on a provisioned stub with a
panicking console and never otherwise; `available_capture` gains `gdbstub` only when the probe
answered, and `inert_capture` loses it in the same case; every pre-existing case in
`tests/jobs/handlers/test_runs_boot.py` still passes.

## Task 2 — Admit the gdbstub transport, with its own refusal

The MCP gate that reads Task 1's recorded evidence.

**Interfaces.** Consumed, confirmed in `src/kdive/mcp/tools/debug/sessions/lifecycle.py` on this
branch: `_GDBSTUB = "gdbstub"` (line 86), `_DRGN_LIVE = "drgn-live"` (line 87),
`CONSOLE_CRASH_GUIDANCE` (imported line 53), `BOOT_OUTCOME_EXPECTED_CRASH_OBSERVED`,
`parse_boot_outcome`, and `async def _succeeded_boot_result(conn: AsyncConnection, run_id: UUID)
-> dict[str, Any] | None` (line 206), whose non-`None` result is the local `boot_result` in
`_attach_preconditions`. Consumed from Task 1: the `available_capture` list recorded by
`record_expected_crash`. Provides nothing to Task 3.

**Verification inventory.**

- Contract: a gdbstub attach on `expected_crash_observed` with `gdbstub` in `available_capture`
  falls through to the System-ready/occupied checks and inserts a session. Mode: focused-test.
  Test: `tests/mcp/debug/test_debug_tools.py::
  test_start_session_admits_gdbstub_for_expected_crash_with_live_stub`. Expected red:
  `resp.status == "error"` with `reason == "expected_crash_not_live_debuggable"`. Green:
  `uv run python -m pytest tests/mcp/debug/test_debug_tools.py -q`.
- Contract: a gdbstub attach on `expected_crash_observed` without `gdbstub` in
  `available_capture` is refused with the new detail. Mode: focused-test. Test: the existing
  `test_start_session_rejects_expected_crash_run`, retargeted. Expected red: the detail
  assertion fails because the detail is still `CONSOLE_CRASH_GUIDANCE`. Green: the same command.
- Contract: a `drgn-live` attach on `expected_crash_observed` is refused with
  `CONSOLE_CRASH_GUIDANCE`, whatever `available_capture` holds. Mode: focused-test. Test:
  `tests/mcp/debug/test_debug_tools.py::test_start_session_rejects_drgn_live_on_expected_crash`.
  This contract is preserved rather than changed, so its red comes from a controlled fault: after
  step 7, temporarily drop the `if transport == _GDBSTUB` arm so the branch always uses
  `_EXPECTED_CRASH_GDBSTUB_DETAIL`, confirm the case fails on the detail assertion, revert the
  fault. Green: the same command.
- Contract: the `debug.start_session` wrapper docstring names the admission and its precondition.
  Mode: task-test-not-applicable. The changed surface is the prose FastMCP serializes into the
  tool schema; the only executable observation would search for or snapshot that wording, which
  this plan forbids.

**Steps.**

1. Retarget `test_start_session_rejects_expected_crash_run` so its seeded `boot_result` is
   `{"boot_outcome": "expected_crash_observed", "available_capture": ["console"]}`, and change
   its detail assertion from `CONSOLE_CRASH_GUIDANCE` to `_EXPECTED_CRASH_GDBSTUB_DETAIL`,
   imported from `kdive.mcp.tools.debug.sessions.lifecycle`. Keep its other assertions
   (`reason`, `suggested_next_actions`, session count 0, `conn_fake.opened == []`) as they are.
2. Add `test_start_session_rejects_drgn_live_on_expected_crash`, seeding
   `{"boot_outcome": "expected_crash_observed", "available_capture": ["console", "gdbstub"]}`
   and calling with `transport="drgn-live"`. Assert `resp.detail == CONSOLE_CRASH_GUIDANCE` and
   `resp.data["reason"] == "expected_crash_not_live_debuggable"`.
3. Add `test_start_session_admits_gdbstub_for_expected_crash_with_live_stub`, modelled on the
   existing `test_start_session_admits_gdbstub_for_crashed_halted_live` in the same file, with
   `boot_result={"boot_outcome": "expected_crash_observed",
   "available_capture": ["console", "gdbstub"]}`. Assert `resp.status == "success"` and that one
   session row exists.
4. Run `uv run python -m pytest tests/mcp/debug/test_debug_tools.py -q`. Expect step 1's case to
   fail on the detail assertion and step 3's to fail with
   `reason == "expected_crash_not_live_debuggable"`.
5. In `lifecycle.py`, add the detail constant beside `_CRASHED_HALTED_LIVE_DRGN_DETAIL` (near
   line 99):

   ```python
   # An expected console_crash whose provisioned gdbstub did not answer at boot (ADR-0628). The
   # vmcore-worded CONSOLE_CRASH_GUIDANCE would be wrong here: a gdbstub attach needs no capture
   # kernel and produces no vmcore, so the refusal is about the stub, not about kdump.
   _EXPECTED_CRASH_GDBSTUB_DETAIL = (
       "this run declared an early-boot console_crash and its gdbstub did not answer when the "
       "boot recorded the crash, so there is no halted stub to attach to. Read the console "
       "artifact instead — fetch its reference with runs.get."
   )
   ```

6. Add a reader above `_attach_preconditions`. The `isinstance` guard is what makes it total
   against a boot step recorded before this change, whose result carries
   `available_capture: ["console"]` or no such key:

   ```python
   def _gdbstub_recorded_available(boot_result: dict[str, Any]) -> bool:
       """True iff the boot step recorded a gdbstub that answered its probe (ADR-0628)."""
       available = boot_result.get("available_capture")
       return isinstance(available, list) and _GDBSTUB in available
   ```

7. Replace the `expected_crash_observed` branch (currently lines 613–624) with:

   ```python
       if boot_outcome == BOOT_OUTCOME_EXPECTED_CRASH_OBSERVED and not (
           transport == _GDBSTUB and _gdbstub_recorded_available(boot_result)
       ):
           # A declared expected crash leaves the System READY, so vmcore.fetch always rejects and
           # postmortem.crash only self-corrects back to the console (#759): the non-gdbstub
           # refusal reuses postmortem.crash's shared CONSOLE_CRASH_GUIDANCE so the two surfaces
           # cannot drift. A gdbstub attach is refused only when the boot's probe found no stub
           # (ADR-0628), and says so in gdbstub terms rather than kdump ones.
           return ToolResponse.failure(
               str(run.id),
               ErrorCategory.CONFIGURATION_ERROR,
               detail=(
                   _EXPECTED_CRASH_GDBSTUB_DETAIL
                   if transport == _GDBSTUB
                   else CONSOLE_CRASH_GUIDANCE
               ),
               suggested_next_actions=["runs.get", "artifacts.list"],
               data={"reason": "expected_crash_not_live_debuggable"},
           )
   ```

8. In `src/kdive/mcp/tools/debug/sessions/registrar.py`, add one paragraph to the
   `debug_start_session` wrapper docstring, after the existing multiarch one:

   ```
       A run that declared an `expected_boot_failure` is attachable over `gdbstub` when its
       boot probed the provisioned stub and found it answering — `runs.get` reports that as
       `gdbstub` in `available_capture` rather than in `inert_capture` (ADR-0628). Check that
       field before attaching: when the stub did not answer, or when the console matched the
       expectation without a kernel panic, no probe ran and the attach is refused. `drgn-live`
       is never admitted against a crashed guest, which has no running sshd.
   ```

9. Run the controlled-fault check named in the third inventory entry, then
   `uv run python -m pytest tests/mcp/debug/test_debug_tools.py -q`. Expect every case to pass.
   Then `just format`, `just lint`, `just type`, and `just adr-status-check` bare: `All checks
   passed!` from the first three, exit 0 from the last.

**Acceptance criteria.** gdbstub is admitted on `expected_crash_observed` exactly when the boot
recorded it in `available_capture`; the gdbstub refusal carries `_EXPECTED_CRASH_GDBSTUB_DETAIL`
and the non-gdbstub refusal still carries `CONSOLE_CRASH_GUIDANCE`; the wrapper docstring names
the precondition.

## Task 3 — Stop pairing a kexec reason with an empty inert list

The `runs.get` render path, whose `inert_capture` list Task 1 can now empty.

**Interfaces.** Consumed: `StepProgress` (`src/kdive/services/runs/steps.py:170`) with fields
`boot_outcome`, `available_capture`, `inert_capture`; `CONSOLE_CRASH_GUIDANCE`;
`BOOT_OUTCOME_EXPECTED_CRASH_OBSERVED`. Provides nothing.

**Verification inventory.** Contract: `runs.get` emits `inert_capture_reason` only alongside a
non-empty `inert_capture`. Mode: focused-test. Test: `tests/mcp/lifecycle/test_runs_tools.py`, a
case asserting `"inert_capture_reason" not in resp.data` for an `expected_crash_observed` step
whose `inert_capture` is `[]`. Expected red: the key is present, because the current guard is
`is not None`. Green: `uv run python -m pytest tests/mcp/lifecycle/test_runs_tools.py -q`.

**Steps.**

1. In `tests/mcp/lifecycle/test_runs_tools.py`, add a case beside the existing one at line 1560
   seeding an `expected_crash_observed` boot step with
   `available_capture: ["console", "gdbstub"]` and `inert_capture: []`, asserting
   `resp.data["inert_capture"] == []` and `"inert_capture_reason" not in resp.data`.
2. Run `uv run python -m pytest tests/mcp/lifecycle/test_runs_tools.py -q`. Expect the new case
   to fail on the second assertion.
3. In `src/kdive/mcp/tools/lifecycle/runs/common.py`, change the reason guard inside
   `_capture_data` from `if step_progress.boot_outcome == BOOT_OUTCOME_EXPECTED_CRASH_OBSERVED:`
   to `if step_progress.inert_capture and step_progress.boot_outcome ==
   BOOT_OUTCOME_EXPECTED_CRASH_OBSERVED:`, and extend the existing comment with: `A probed-live
   gdbstub can empty the list (ADR-0628), and a reason for nothing is not a reason.`
4. Run `uv run python -m pytest tests/mcp/lifecycle/test_runs_tools.py -q`. Expect every case in
   the file to pass. Then `just format`, `just lint`, `just type`.
5. Run the full gate bare: `just ci > /tmp/ci-2303.log 2>&1 < /dev/null`, and read the log.
   Expect exit 0.

**Acceptance criteria.** `inert_capture_reason` never accompanies an empty `inert_capture`; the
existing case at line 1560 still passes unchanged.

**Rollback.** Reverting the five source files and their three test files restores the previous
behavior. No schema, migration, or persisted state is touched: `boot_outcome` and both capture
lists ride in the schemaless `run_steps.result`, so a revert simply stops writing the probed
`available_capture` and the gate stops reading it.
