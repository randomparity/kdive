# Plan: diagnostics names why it withheld, and `operator_recovery` is documented

**Goal.** Give every `diagnostics` withhold path a fixed-form reason an operator can act on, and
make `operator_recovery` and the wedged-slot recovery procedure resolve in shipped documentation.

**Architecture.** `SystemdDiagnostics`
(`src/kdive/processes/lifecycle/systemd/systemd_diagnostics.py`) walks the eight fixed worker
slots, acquires bounded systemd properties and journal text per slot, redacts it, and appends the
result to a `_DiagnosticCapture` enforcing the aggregate emission budget. Four places in that walk
give up on a slot, each recording the slot and nothing else. This change adds a closed
`WithholdReason` enum, threads it through the private `_UnsafeDiagnosticText` exception, splits
the deterministic `StateConflict` preconditions out of the generic `except` arm, relabels the
refusal `_sanitize_diagnostics` raises so `acquisition_failures` cannot absorb it, and funnels all
four sites through one `_DiagnosticCapture.withhold` method that emits the marker and builds the
`SlotResult` together. Two operator documents then define the `RetryAction` vocabulary and the
recovery procedure.

**Tech stack.** Python 3.14, `uv`, pydantic v2, pytest. Markdown under `docs/` and `deploy/`.

Design: [`docs/workflow/specs/2026-09-16-diagnostics-withhold-reason-design.md`](../specs/2026-09-16-diagnostics-withhold-reason-design.md).

Expected implementation size: 230–380 changed lines (M) — from the file map below: about 45 lines
in the diagnostics module, about 130 in its tests (five new cases, seven existing cases updated),
about 45 in `deploy/systemd/README.md`, about 65 in the runbook. Task 4 changes no file.

The built diff is larger than the original 230–300 estimate, and the range above was widened to
the measured figure rather than the code cut to fit it. As shipped it measures 377 changed lines
(`git diff --shortstat e363c265...HEAD -- src/ tests/ deploy/ docs/operating/`): 330 after the
three task commits, then a further round from the security pass, which relabelled the redaction
refusal `_sanitize_diagnostics` raises, corrected three operator-document rows, restored an
exact-byte emission assertion, and added the case covering the new arm. The overrun is in the
diagnostics module and its tests — the `withhold` funnel and the two precondition arms are more
code than "about 45 lines" allowed, and each updated case gained a reason assertion. Every line
traces to a completion criterion and the reviewed design; no unrequested work is present. The
frozen `M` denominator of 250 is unchanged — the estimate is informational and never replaces it.

## Global Constraints

- **No schema change.** `Operation`, `LifecycleRequest`, `LifecycleResponse`, and `SlotResult` are
  untouched; `lifecycle_protocol_identity()` stays byte-identical to its value at base `e363c265`,
  or `scripts/live-stack/worker-lifecycle.sh` fails closed on every provisioned host. Populating
  the existing `SlotResult.phase` field is not a schema change.
- **Closed vocabulary.** Every reason value is a literal member of `WithholdReason`; none is
  derived from an exception string, a captured value, or any part of the withheld report.
- **Bounds do not move.** 320 KiB per-slot and 1.25 MiB aggregate acquisition; 256 KiB per-slot
  and 1 MiB aggregate emission. Reason bytes are accounted through `_DiagnosticCapture.append`. An
  emitted marker holding a value in the capture's known forbidden set is suppressed to `""`.
- Ruff line length 100, lint set `E,F,I,UP,B,SIM`; `ty` runs whole-tree via `just type`. Doc
  style: **Milestone**, never "Sprint"; avoid "critical", "robust", "comprehensive", "elegant".
- Iterate with `just lint`, `just type`, `just test-changed`; rerun failures with `just test-lf`.
  Before committing: stage, run `prek run`, re-add exactly the recorded staged paths. Full gate:
  `just ci > <file> 2>&1 < /dev/null`.

## File map

| Path | Now | After |
|---|---|---|
| `src/kdive/processes/lifecycle/systemd/systemd_diagnostics.py` | four anonymous withhold sites | owns `WithholdReason`, one `withhold` funnel, and a `StateConflict` arm; a withheld result carries `phase` |
| `tests/processes/lifecycle/systemd/test_systemd_worker_lifecycle.py` | seven diagnostics cases the change touches | each reason covered, plus the leak and suppression proofs |
| `deploy/systemd/README.md` | installs the fixed live-worker contract | also defines the six `RetryAction` values |
| `docs/operating/runbooks/live-stack.md` | bring-up, budgets, teardown | also the wedged-slot recovery procedure |

No caller migration and no obsolete path: `WithholdReason` is new surface inside the module that
already owns withholding, and the only removal is the now-unreachable `code` keyword on the
module-private `_result`. `systemd_worker_contract.py`, `systemd_worker_lifecycle.py`,
`systemd_worker_control.py`, and `scripts/live-stack/worker-lifecycle.sh` are read, not changed.

## Task 1 — the reason vocabulary and the four withhold sites

Modifies `src/kdive/processes/lifecycle/systemd/systemd_diagnostics.py`; tests in
`tests/processes/lifecycle/systemd/test_systemd_worker_lifecycle.py` (`PYTEST_FILE` below).

**Interfaces.** Consumes `SlotPhase` and `SlotResult` from
`kdive.processes.lifecycle.systemd.systemd_worker_contract`, and `StateConflict`, already
imported from `...systemd_worker_state`. Provides:

```text
class WithholdReason(StrEnum)
    STATE_UNREADABLE = "state_unreadable"
    SLOT_UNUSABLE = "slot_unusable"
    ACQUISITION_FAILED = "acquisition_failed"
    REDACTION_REFUSED = "redaction_refused"
    PEER_REDACTION_REFUSED = "peer_redaction_refused"
    INTERNAL_ERROR = "internal_error"

_DiagnosticCapture.withhold(
    self, slot: int, unit: str, reason: WithholdReason, *, phase: SlotPhase | None = None
) -> SlotResult
    # adds slot to withheld_slots; appends the marker, or "" when the marker contains a value
    # in self.forbidden_values; returns SlotResult(slot, unit, phase=phase,
    # code="diagnostics_withheld", message=f"withheld: {reason.value}")

_UnsafeDiagnosticText.__init__(
    self, forbidden: tuple[str, ...], *, reason: WithholdReason,
    used: int | None = None, aggregate_truncated: bool = False
) -> None

_result(state: SlotState) -> SlotResult
    # SlotResult(slot=state.slot, unit=state.unit, message=state.phase.value) -- body unchanged.
    # Only the `code` keyword goes, and only because step 11 leaves every remaining caller on the
    # default. A successful capture's result is not otherwise reshaped: no criterion asks for it.

_WITHHELD_TEMPLATE = "[diagnostics withheld for slot {slot}: {reason}]\n"
```

Tasks 2 and 3 rely on the six reason strings above as prose, nothing more.

### Verification inventory

- **Each withhold cause yields its own reason on the `SlotResult` and in the emitted marker.**
  Mode: `focused-test`. Observable: `response.slots[i].message`, `response.slots[i].phase`, and
  `response.diagnostics` after `SystemdWorkerLifecycle.diagnostics`. New cases
  `test_diagnostics_names_acquisition_failed_reason` and
  `test_diagnostics_names_peer_redaction_refused_reason`; the other four causes are asserted in
  the existing cases step 13 updates. Expected red: `AssertionError` comparing `'withheld'` or a
  bare phase value against `'withheld: <reason>'`. Green:
  `uv run python -m pytest PYTEST_FILE -k "diagnostics and reason" -q`.
- **No reason carries report-derived text.** Mode: `focused-test`. New case
  `test_diagnostics_reason_carries_no_withheld_material`: a redaction source and a journal body
  both holding the sentinel `LEAK-SENTINEL`, asserting it is absent from
  `response.model_dump_json()`. Expected red: the case does not exist yet. Green:
  `uv run python -m pytest PYTEST_FILE -k reason_carries_no_withheld_material -q`.
- **The marker is suppressed when it collides with a known forbidden value.** Mode:
  `focused-test`. New case `test_diagnostics_withheld_marker_respects_known_forbidden_values`:
  a single-slot fleet with `runtime.journal_failure = SystemdUnavailable("journal unavailable")`
  and `redaction_sources={1: ("withheld",)}`, so the `acquisition_failed` marker collides.
  Asserts `response.diagnostics == ""` and
  `response.slots[0].message == "withheld: acquisition_failed"`. Expected red: today the emitted
  text is `""` but the message is `'started'`, so the message assertion fails. Green:
  `uv run python -m pytest PYTEST_FILE -k withheld_marker_respects -q`.
- **`lifecycle_protocol_identity()` does not move.** Mode: `task-test-not-applicable`. The
  identity is a pure function of `systemd_worker_contract.py`'s two schemas, and the contract is
  the absence of a change to them. No task-specific test can fail meaningfully: a pinned-hash
  assertion is the shape this repository already rejected, recorded in
  `tests/processes/lifecycle/systemd/test_systemd_worker_contract.py`'s own docstring — "an
  inequality against a frozen hash passes for any schema change at all". The structural
  observation that does bite is step 15's `git diff --exit-code`, which is exact and repeatable.

### Steps

1. Read `src/kdive/processes/lifecycle/systemd/systemd_diagnostics.py` end to end. The four
   withhold sites are in `_capture_diagnostics` and `_capture_diagnostic_slot`.

2. Write the four new cases from the verification inventory into `PYTEST_FILE`, beside the
   existing `test_diagnostics_withholds_*` group, using the module's `_state`, `_fleet`,
   `_coordinator`, `_deadline`, and `_run` helpers. Reach each cause the way the existing cases
   already do:

   - `state_unreadable`: `stores[0].load_failure = ValueError("state path and credential detail")`.
   - `slot_unusable`: `redaction_sources={1: ("s" * 4097,)}`, so `_validated_redaction_values`
     raises `StateConflict` before `_diagnose_slot`'s own `try`.
   - `acquisition_failed`: `runtime.journal_failure = SystemdUnavailable("journal unavailable")`
     on a single-slot fleet.
   - `redaction_refused`: a journal body of `"x" * (320 * 1024)` with
     `redaction_sources={1: ("truncated",)}`, so the truncation marker the renderer appends
     collides with the forbidden value and `_diagnose_trusted_slot` refuses its own report.
   - `peer_redaction_refused`: a two-slot fleet where slot 1 registers a value that also appears
     in slot 2's journal body, so slot 2's report is clean against its own forbidden set and
     refused against the accumulated one.
   - `internal_error`: a `load_redaction_values` callable raising `PermissionError`, which is not
     a `StateConflict` and so falls through to the generic arm.

3. Run `uv run python -m pytest PYTEST_FILE -k "diagnostics and reason" -q`. Expect failures: the
   messages are still `'withheld'` or a bare phase value.

4. Add `from enum import StrEnum` to the imports and `SlotPhase` to the existing
   `systemd_worker_contract` import. Add `WithholdReason` after the module constants with exactly
   the six members in the Interfaces block, and a class docstring stating that every member is a
   literal — no value is derived from an exception, a captured value, or the withheld report,
   because two of these causes fire precisely because that material held a forbidden value.

5. Change `_WITHHELD_TEMPLATE` to the value in the Interfaces block, and give
   `_UnsafeDiagnosticText` the required keyword `reason: WithholdReason` after `*`, stored as
   `self.reason` beside the existing three attributes.

6. Add `withhold` to `_DiagnosticCapture`, directly after `append`, with the signature and
   behaviour in the Interfaces block:

   ```python
   def withhold(
       self, slot: int, unit: str, reason: WithholdReason, *, phase: SlotPhase | None = None
   ) -> SlotResult:
       """Record one withheld slot, emit its marker, and name its fixed-form cause."""
       self.withheld_slots.add(slot)
       marker = _WITHHELD_TEMPLATE.format(slot=slot, reason=reason.value)
       self.append("" if _contains_forbidden(marker, tuple(self.forbidden_values)) else marker)
       return SlotResult(
           slot=slot,
           unit=unit,
           phase=phase,
           code="diagnostics_withheld",
           message=f"withheld: {reason.value}",
       )
   ```

7. In `_capture_diagnostics`, replace the unsafe-state branch's three statements
   (`withheld_slots.add`, `append(...)`, `results.append(SlotResult(...))`) with
   `capture.results.append(capture.withhold(store.slot, store.unit,
   WithholdReason.STATE_UNREADABLE))`. That site has no loaded state, so it passes no `phase`.

8. In `_capture_diagnostic_slot`, rewrite the `except _UnsafeDiagnosticText` arm to keep its
   `used` refund and `forbidden_values` update, then set `capture.aggregate_truncated =
   exc.aggregate_truncated` **before** returning `capture.withhold(store.slot, store.unit,
   exc.reason, phase=state.phase)`. Moving the assignment ahead of the emission is
   behaviour-preserving: `append` reads only `emitted`.

9. Insert a new `except StateConflict as exc:` arm between it and the generic one. It is reached
   only by `_require_diagnostic_budget` and `_validated_redaction_values`, which run before
   `_diagnose_slot`'s own `try`; a `StateConflict` raised inside that `try` is converted to
   `_UnsafeDiagnosticText` by `self._acquisition_failures`, which lists it. Log at `warning` with
   the existing `slot=%s cause=%s` shape and `type(exc).__name__`, refund the full reservation
   with `capture.acquired -= reservation` as the generic arm does, and return
   `capture.withhold(store.slot, store.unit, WithholdReason.SLOT_UNUSABLE, phase=state.phase)`.

10. Leave the generic `except Exception` arm's log and full refund as they are, and replace its
    two-statement withhold with
    `capture.withhold(store.slot, store.unit, WithholdReason.INTERNAL_ERROR, phase=state.phase)`.

11. Rewrite the tail of `_capture_diagnostic_slot`: keep the `acquired` refund and
    `forbidden_values.update(forbidden)`, hoist `capture.aggregate_truncated =
    aggregate_truncated` above the forbidden check, and where `_contains_forbidden(report,
    tuple(capture.forbidden_values))` holds, return `capture.withhold(store.slot, store.unit,
    WithholdReason.PEER_REDACTION_REFUSED, phase=state.phase)` instead of blanking `report`.
    Otherwise `capture.append(report)` and `return _result(state)`. The trailing `code = ... if
    store.slot in capture.withheld_slots` line goes with it.

12. Name the reason at each raise, and give `_diagnose_slot` three arms rather than two. Insert
    `except StateConflict` AHEAD of `except self._acquisition_failures`, raising
    `_UnsafeDiagnosticText(secret_values, reason=WithholdReason.REDACTION_REFUSED)`: that tuple
    lists `StateConflict`, so without this arm `_sanitize_diagnostics`'s sentinel refusal would be
    relabelled `acquisition_failed` and the runbook would send the operator to re-run a request
    that deterministically fails the same way. The arm shadows the tuple's `StateConflict` entry,
    which is retained only as a backstop. The remaining two arms keep
    `reason=WithholdReason.ACQUISITION_FAILED`, and the raise in `_diagnose_trusted_slot` passes
    `reason=WithholdReason.REDACTION_REFUSED` alongside its existing `used` and
    `aggregate_truncated` keywords. Cover the new arm with a case that fails without it.

13. Drop the unused `code` keyword from the module-private `_result`, leaving its body as it is,
    and update the seven existing cases in `PYTEST_FILE` the change touches:

    | Case | New expectation |
    |---|---|
    | `test_diagnostics_reserves_aggregate_acquisition_for_failed_journals` | the four reached slots each emit `[diagnostics withheld for slot N: acquisition_failed]\n` ahead of `[aggregate diagnostics truncated]\n` |
    | `test_failed_journal_aggregate_marker_respects_known_forbidden_values` | the four withheld markers are emitted; only the aggregate marker is suppressed by the forbidden value `"aggregate"` |
    | `test_diagnostics_withholds_unsafe_source_without_reading_its_journal` | `[diagnostics withheld for slot 1: internal_error]\n`, message `withheld: internal_error`, `phase` is `SlotPhase.STARTED` |
    | `test_diagnostics_withholds_unsafe_state_without_exposing_error_detail` | `[diagnostics withheld for slot 1: state_unreadable]\n`, message `withheld: state_unreadable`, `phase` is `None` |
    | `test_diagnostics_withholds_oversized_redaction_value` | `[diagnostics withheld for slot 1: slot_unusable]\n`, message `withheld: slot_unusable` |
    | `test_diagnostics_emits_no_fallback_when_truncation_text_collides` | parametrize the expected emission alongside the secret — `("diagnostics", "")` because the marker contains `"diagnostics"` and is suppressed, and `("truncated", "[diagnostics withheld for slot 1: redaction_refused]\n")` because it does not. Keep `secret not in response.diagnostics` and add `response.slots[0].message == "withheld: redaction_refused"` for both. No disjunction: each parameter has one determinate outcome |
    | `test_diagnostics_emits_no_fallback_when_aggregate_marker_collides` | slot 4's site now emits `[diagnostics withheld for slot 4: redaction_refused]\n`, which holds no `"aggregate"` and so is not suppressed. Keep the assertion exact — `== 3 * 256 * 1024 + len(marker)` — because `LifecycleResponse` already rejects anything over 1 MiB, so a `<=` bound could not fail and an under-emitting regression would pass it silently. Add the marker's presence and keep `"aggregate" not in response.model_dump_json()` |

    Successful-capture results are untouched, so no case asserting a non-withheld slot's `message`
    changes. If one asserts a *withheld* slot's message as a bare phase value, it moves to
    `withheld: <reason>` with the phase read from `slot.phase`.

14. Run `uv run python -m pytest tests/processes/lifecycle/systemd/ -q`. Expect every case green.
    Then `just lint` and `just type`, expecting exit 0 each.

15. Prove the protocol identity did not move — a computed comparison, because no frozen-hash pin
    exists and the repository deliberately rejected that shape:

    ```bash
    probe='from kdive.processes.lifecycle.systemd.systemd_worker_contract import lifecycle_protocol_identity as i; print(i())'
    git -C . show e363c265:src/kdive/processes/lifecycle/systemd/systemd_worker_contract.py > /tmp/base_contract.py
    diff /tmp/base_contract.py src/kdive/processes/lifecycle/systemd/systemd_worker_contract.py
    uv run python -c "$probe"
    ```

    Expect `diff` to print nothing and exit 0, which makes the two computed identities equal by
    construction; record the printed identity in the commit body.

16. Stage, run `prek run`, re-add exactly the recorded staged paths, and commit
    `fix(lifecycle): name the cause of every diagnostics withholding`.

**Acceptance.** Six distinct reasons, one per cause; every reason a literal enum member; the
marker suppressed on a forbidden collision; `SlotResult.phase` populated; the computed identity
unchanged; lint, type, and the module's tests green.

## Task 2 — define the `RetryAction` vocabulary

Modifies `deploy/systemd/README.md`.

**Interfaces.** Consumes the six reason strings from Task 1 and the `RetryAction` literals in
`src/kdive/processes/lifecycle/systemd/systemd_worker_contract.py`. Task 3 links to the
`## Lifecycle retry actions` section this task adds.

### Verification inventory

- **`deploy/systemd/README.md` defines all six `RetryAction` values.** Mode:
  `task-test-not-applicable`. Prose with no executable consumer. `docs-links` resolves a link's
  file and discards its `#fragment` (`scripts/check-doc-links.sh`, `target="${target%%#*}"`), so
  step 4 greps the heading that produces the anchor instead; `docs-paths` covers referenced
  `docs/<path>` strings. `docs-check` is the tool-reference generator diff and `served-doc-links`
  applies only to served docs, so neither touches this file. A test over the prose would pin
  wording, not behaviour.

### Steps

1. Read `deploy/systemd/README.md`, `_map_failure` and `_invalid_start_response` in
   `src/kdive/processes/lifecycle/systemd/systemd_worker_lifecycle.py`, and
   `src/kdive/processes/lifecycle/systemd/systemd_worker_control.py`, which is the socket server
   every `worker-lifecycle.sh` invocation goes through. `_map_failure` returns four of the six
   values — `retry_same_operation`, `operator_recovery`, `restore_database`, `restore_systemd`.
   `correct_request` comes from `_invalid_start_response` and from the control module's malformed
   request response; `none` comes from `_ok_response` and from a successful `diagnostics` capture.

2. Append a `## Lifecycle retry actions` section after `## Fixed live-worker lifecycle contract`.
   Introduce it as the `retry_action` field of every `scripts/live-stack/worker-lifecycle.sh`
   response, then give one table row per value with what it means and the action it asks for, each
   grounded in the condition that produces it:

   - `none` — a successful response.
   - `correct_request` — a start request missing `worker_count`/`settings`, or a request frame the
     control module rejects as malformed.
   - `retry_same_operation` — a deadline, rejected termination evidence, a `busy` refusal while
     another lifecycle request holds the control lock, or a `diagnostics` operation that failed
     before any slot was captured.
   - `restore_systemd` — `SystemdUnavailable`: systemd could not answer for the retained unit.
   - `restore_database` — the database authority is unavailable.
   - `operator_recovery` — every `conflict` response, an unmapped internal error, and a
     `diagnostics` capture that withheld at least one slot.

3. Under the table, state that `operator_recovery` means no retry of the same request will clear
   the condition: the operator inspects `status` and `diagnostics`, then runs
   `scripts/live-stack/worker-lifecycle.sh recover`. Add one clause noting that
   `code=diagnostics_withheld` carries `operator_recovery` for a per-slot withholding but
   `retry_same_operation` when the whole capture failed, so the retry action rather than the code
   selects the response. Link the procedure Task 3 adds at
   `../../docs/operating/runbooks/live-stack.md#recovering-a-wedged-worker-slot`.

4. Run `just docs-links` and `just docs-paths`, expecting exit 0 each, and require a hit from
   `rg -n '^## Lifecycle retry actions' deploy/systemd/README.md`. Stage, run `prek run`, re-add
   exactly the recorded staged paths, and commit
   `docs(systemd): define the lifecycle retry actions`.

**Acceptance.** All six values defined against the condition that emits them, across both modules
that emit them; `operator_recovery` resolves to a definition and to the recovery procedure; the
anchor the runbook links exists; doc guards green.

## Task 3 — the wedged-slot recovery procedure

Modifies `docs/operating/runbooks/live-stack.md`.

**Interfaces.** Consumes the six reason strings from Task 1 and the `#lifecycle-retry-actions`
anchor from Task 2. Nothing later depends on this task.

### Verification inventory

- **The runbook carries a wedged-slot procedure matching the shipped `recover`.** Mode:
  `task-test-not-applicable`. Same reasoning as Task 2: prose with no executable consumer, whose
  file-level links `docs-links` resolves and whose anchor step 4 greps directly. This file is not
  under `src/kdive/mcp/resources/_content/`, so it is not a served doc and needs no
  `resources-docs-check` snapshot.

### Steps

1. Read section 4 of `docs/operating/runbooks/live-stack.md`, `recover` and `_recover_slot` in
   `src/kdive/processes/lifecycle/systemd/systemd_worker_lifecycle.py`, the `request` function in
   `scripts/live-stack/worker-lifecycle.sh`, and the `recover` paragraphs of
   `docs/adr/0657-a-successor-invocation-is-terminal-evidence.md`. Describe what the code does
   now, not what it could do.

2. Add a `### Recovering a wedged worker slot` subsection immediately **before** the existing
   `### The app tier does not hot-reload — re-run `stack-services.sh` after editing source`
   subsection, so the new heading opens at a heading boundary and section 4's flowing prose stays
   in section 4. Cover, in order:

   - **The symptom.** `stack-services.sh` fails, or a lifecycle request returns
     `retry_action=operator_recovery` — most often `code=conflict` after a worker unit was
     restarted outside the lifecycle contract, leaving it `failed` with its `InvocationID`
     retained so the next `start` is refused. Link the retry-action table at
     `../../../deploy/systemd/README.md#lifecycle-retry-actions`.
   - **Read the cause first.** The client prints one JSON line, so give a command rather than a
     noun: `scripts/live-stack/worker-lifecycle.sh diagnostics | python3 -m json.tool`, noting
     that the command exits 4 when a slot was withheld, so the pipe is what keeps the output
     readable in a `set -e` shell. A withheld slot reads `"code": "diagnostics_withheld"` with
     `"message": "withheld: <reason>"` and its `"phase"`. Table the six reasons with meaning and
     action — `state_unreadable`: the slot's state file could not be read, check ownership under
     `/var/lib/kdive/live-workers`; `slot_unusable`: the slot holds no usable diagnostic state
     (no exact invocation, no safe budget, or redaction sources rejected as unsafe), run
     `recover`, or check the redaction sources under the same directory; `acquisition_failed`:
     systemd, the journal, or the request deadline did not answer in time — check
     `systemctl status` and `journalctl` for the unit, then re-run `diagnostics`;
     `redaction_refused` and `peer_redaction_refused`: the report held a value the redactor may
     not emit, read the unit's journal on the host directly and do not re-run; `internal_error`:
     the redaction-source file could not be read, or something unexpected — check ownership and
     mode under `/var/lib/kdive/live-workers`, and the witness log names the exception type.
   - **A silent slot is truncation, not withholding.** One sentence: a slot whose result carries
     no reason and whose text is absent after `[aggregate diagnostics truncated]` fell outside the
     1.25 MiB acquisition or 1 MiB emission budget and was never captured; read that unit's
     journal on the host directly.
   - **Recover.** `scripts/live-stack/worker-lifecycle.sh recover`. Unlike `diagnostics`, this
     operation first requires the installed lifecycle venv to match the checkout; if it prints
     `installed lifecycle protocol does not match this checkout; reprovision the runner` or
     `installed lifecycle protocol is unavailable; reprovision the runner`, reprovision the host
     per this runbook's Prerequisites before retrying. Per slot it then observes the unit and
     refuses one whose cgroup still holds live processes, returning `code=conflict`,
     `retry_action=operator_recovery`, and a per-slot `recovery_refused` — stop that work rather
     than forcing past it. For a slot proven dead it publishes terminal evidence derived from that
     observation, clears the on-disk slot facts, releases the `worker_incarnations` fence, and
     runs `reset-failed` on a unit systemd still accounts for; it never fabricates a termination
     outcome (ADR-0657, at `../../adr/0657-a-successor-invocation-is-terminal-evidence.md`).
   - **Bring the stack back up.** Re-run `scripts/live-stack/stack-services.sh`; it is idempotent.
   - **What `recover` does not reach.** A slot with absent or malformed `state.json`, a drifted
     binding, rejected evidence, or an unreadable boot ID stays wedged; that is issue #2533. Say
     so rather than describing a manual edit.

   State that the reason bytes are accounted inside the emission budgets given above, so those
   numbers are unchanged.

3. Run `just docs-links`, `just docs-paths`, and `just served-doc-links`, expecting exit 0 each,
   and require a hit from
   `rg -n '^### Recovering a wedged worker slot' docs/operating/runbooks/live-stack.md`. Stage,
   run `prek run`, re-add exactly the recorded staged paths, and commit
   `docs(runbook): add the wedged worker slot recovery procedure`.

**Acceptance.** An operator can go from an `operator_recovery` response to a running stack with
only the shipped contract; every reason maps to an action; `recover`'s compatibility prerequisite
and its refusal case are named; the residual cases are named as out of reach with their owning
issue; doc guards green.

## Task 4 — the full gate

Changes no file. It owns completion criterion 7, which no earlier task's checks reach: Tasks 1–3
run lint, type, one test directory, and three doc guards, while `just ci` additionally runs
`lock-check`, the shell/workflow/Ansible linters, `adr-status-check`, every generated-artifact
check, and the whole suite.

### Verification inventory

- **Criterion 7: `just ci` green.** Mode: `focused-test`. The observable is the recipe's own exit
  status. No expected red: this task runs after Tasks 1–3 are green, and a failure here is a
  defect in one of them rather than a planned red. Green: the command in step 2, exit 0.

### Steps

1. `git fetch origin main && just records`. Expect exit 0 — no decision record is added or
   changed by this work, so the gate has nothing to compare.

2. Run the full gate in the foreground, as the last command in its invocation, with a raised
   timeout:

   ```bash
   just ci > /tmp/ci-2489.log 2>&1 < /dev/null
   ```

   Expect exit 0. `< /dev/null` is required: `lint-ansible` aborts on non-blocking stdin. Judge by
   the exit status, never by grepping the log — `test-ansible` deliberately exercises negative
   paths, so the log contains `[ERROR]` and `failed:` strings from an intentional
   checksum-mismatch proof. Never pipe the recipe through `tail` or `head`, and never append
   `; echo $?`: both replace the recipe's status with the trailing command's.

3. On a failure, read `/tmp/ci-2489.log` for the first failing recipe, fix it in the task that
   owns the file, and re-run this step rather than the whole plan.

**Acceptance.** `just ci` exits 0, and `just records` exits 0.

## Deferrals

None.
