# Plan: diagnostics names why it withheld, and `operator_recovery` is documented

**Goal.** Give every `diagnostics` withhold path a fixed-form reason an operator can act on, and
make `operator_recovery` and the wedged-slot recovery procedure resolve in shipped documentation.

**Architecture.** `SystemdDiagnostics`
(`src/kdive/processes/lifecycle/systemd/systemd_diagnostics.py`) walks the eight fixed worker
slots, acquires bounded systemd properties and journal text per slot, redacts it, and appends the
result to a `_DiagnosticCapture` enforcing the aggregate emission budget. Four places in that walk
give up on a slot, each recording the slot and nothing else. This change adds a closed
`WithholdReason` enum, threads it through the private `_UnsafeDiagnosticText` exception, and
funnels all four sites through one `_DiagnosticCapture.withhold` method that emits the marker and
builds the `SlotResult` together. Two operator documents then define the `RetryAction` vocabulary
and the recovery procedure.

**Tech stack.** Python 3.14, `uv`, pydantic v2, pytest. Markdown under `docs/` and `deploy/`.

Design: [`docs/workflow/specs/2026-09-16-diagnostics-withhold-reason-design.md`](../specs/2026-09-16-diagnostics-withhold-reason-design.md).

Expected implementation size: 240–320 changed lines (M) — from the file map below: about 60 lines
in the diagnostics module, about 150 in its tests (five new cases, six existing assertions
updated), about 40 in `deploy/systemd/README.md`, about 60 in the runbook.

## Global Constraints

- **No schema change.** `Operation`, `LifecycleRequest`, `LifecycleResponse`, and `SlotResult` are
  untouched; `lifecycle_protocol_identity()` stays byte-identical to its value at base `e363c265`,
  or `scripts/live-stack/worker-lifecycle.sh` fails closed on every provisioned host.
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
| `src/kdive/processes/lifecycle/systemd/systemd_diagnostics.py` | four anonymous withhold sites | owns `WithholdReason` and one `withhold` funnel |
| `tests/processes/lifecycle/systemd/test_systemd_worker_lifecycle.py` | three withhold cases asserting a slot-only marker | one case per reason plus the leak and suppression proofs |
| `deploy/systemd/README.md` | installs the fixed live-worker contract | also defines the six `RetryAction` values |
| `docs/operating/runbooks/live-stack.md` | bring-up, budgets, teardown | also the wedged-slot recovery procedure |

No caller migration and no obsolete path: `WithholdReason` is new surface inside the module that
already owns withholding, and the only removal is the now-unreachable `code` keyword on the
module-private `_result`. `systemd_worker_contract.py`, `systemd_worker_lifecycle.py`, and
`scripts/live-stack/worker-lifecycle.sh` are read, not changed.

## Task 1 — the reason vocabulary and the four withhold sites

Modifies `src/kdive/processes/lifecycle/systemd/systemd_diagnostics.py`; tests in
`tests/processes/lifecycle/systemd/test_systemd_worker_lifecycle.py`.

**Interfaces.** Consumes `SlotPhase` and `SlotResult` from
`kdive.processes.lifecycle.systemd.systemd_worker_contract`. Provides:

```text
class WithholdReason(StrEnum)
    STATE_UNREADABLE = "state_unreadable"
    ACQUISITION_FAILED = "acquisition_failed"
    REDACTION_REFUSED = "redaction_refused"
    PEER_REDACTION_REFUSED = "peer_redaction_refused"
    INTERNAL_ERROR = "internal_error"

_DiagnosticCapture.withhold(
    self, slot: int, unit: str, reason: WithholdReason, *, phase: SlotPhase | None = None
) -> SlotResult

_UnsafeDiagnosticText.__init__(
    self, forbidden: tuple[str, ...], *, reason: WithholdReason,
    used: int | None = None, aggregate_truncated: bool = False
) -> None

_result(state: SlotState) -> SlotResult   # the `code` keyword is removed
```

Tasks 2 and 3 rely on the five reason strings above as prose, nothing more.

### Verification inventory

All focused commands below run in the worktree root. `PYTEST_FILE` stands for
`tests/processes/lifecycle/systemd/test_systemd_worker_lifecycle.py`.

- **Each withhold cause yields its own reason on the `SlotResult` message and in the emitted
  marker.** Mode: `focused-test`. Observable: `response.slots[i].message` and
  `response.diagnostics` after `SystemdWorkerLifecycle.diagnostics`. Cases
  `test_diagnostics_names_state_unreadable_reason`,
  `test_diagnostics_names_acquisition_failed_reason`,
  `test_diagnostics_names_redaction_refused_reason`,
  `test_diagnostics_names_peer_redaction_refused_reason`, and
  `test_diagnostics_names_internal_error_reason` in `PYTEST_FILE`. Expected red: `AssertionError`
  comparing `'withheld'` or a bare phase value against `'withheld: <reason>; phase=started'`.
  Green: `uv run python -m pytest PYTEST_FILE -k "names_ and reason" -q`.
- **No reason carries report-derived text.** Mode: `focused-test`. Case
  `test_diagnostics_reason_carries_no_withheld_material` in `PYTEST_FILE`: a redaction source and
  a journal body both holding the sentinel `LEAK-SENTINEL`, asserting it is absent from
  `response.model_dump_json()`. Expected red: the case does not exist yet. Green:
  `uv run python -m pytest PYTEST_FILE -k reason_carries_no_withheld_material -q`.
- **The marker is suppressed when it collides with a known forbidden value.** Mode:
  `focused-test`. Case `test_diagnostics_withheld_marker_respects_known_forbidden_values` in
  `PYTEST_FILE`: redaction source `"withheld"`, asserting `response.diagnostics == ""` while
  `response.slots[0].message` still names the reason. Expected red: `AssertionError` on the
  message, which is `'started'` today. Green:
  `uv run python -m pytest PYTEST_FILE -k withheld_marker_respects -q`.
- **`lifecycle_protocol_identity()` does not move.** Mode: `focused-test`. The existing pin in
  `tests/processes/lifecycle/systemd/test_systemd_worker_contract.py` must stay green:
  `uv run python -m pytest tests/processes/lifecycle/systemd/test_systemd_worker_contract.py -q`.

### Steps

1. Read `src/kdive/processes/lifecycle/systemd/systemd_diagnostics.py` end to end. The four
   withhold sites are in `_capture_diagnostics` and `_capture_diagnostic_slot`.

2. Write the seven cases from the verification inventory into
   `tests/processes/lifecycle/systemd/test_systemd_worker_lifecycle.py`, beside the existing
   `test_diagnostics_withholds_*` group, using the module's `_state`, `_fleet`, `_coordinator`,
   `_deadline`, and `_run` helpers. Reach each cause the way the existing cases already do:

   - `state_unreadable`: `stores[0].load_failure = ValueError("state path and credential detail")`.
   - `acquisition_failed`: `runtime.journal_failure = SystemdUnavailable("journal unavailable")`
     on a single-slot fleet.
   - `redaction_refused`: a journal body of `"x" * (320 * 1024)` with
     `redaction_sources={1: ("truncated",)}`, so the truncation marker the renderer appends
     collides with the forbidden value and `_diagnose_trusted_slot` refuses its own report.
   - `peer_redaction_refused`: a two-slot fleet where slot 1 registers a value that also appears
     in slot 2's journal body, so slot 2's report is clean against its own forbidden set and
     refused against the accumulated one.
   - `internal_error`: a `load_redaction_values` callable raising `PermissionError`, which escapes
     `_diagnose_slot` outside its own `try`.

3. Run `uv run python -m pytest tests/processes/lifecycle/systemd/test_systemd_worker_lifecycle.py -k "names_ and reason" -q`.
   Expect failures: the messages are still `'withheld'` or a bare phase value.

4. Add `from enum import StrEnum` to the imports and `SlotPhase` to the existing
   `systemd_worker_contract` import, then add the enum after the module constants:

   ```python
   class WithholdReason(StrEnum):
       """The closed vocabulary naming why one slot's diagnostics were withheld.

       Every member is a literal: no value is derived from an exception, a captured value, or
       the withheld report, because two of these causes fire precisely because that material
       held a redaction-forbidden value.
       """

       STATE_UNREADABLE = "state_unreadable"
       ACQUISITION_FAILED = "acquisition_failed"
       REDACTION_REFUSED = "redaction_refused"
       PEER_REDACTION_REFUSED = "peer_redaction_refused"
       INTERNAL_ERROR = "internal_error"
   ```

5. Change the template to carry the reason:

   ```python
   _WITHHELD_TEMPLATE = "[diagnostics withheld for slot {slot}: {reason}]\n"
   ```

6. Give `_UnsafeDiagnosticText` a required keyword `reason: WithholdReason` in its signature after
   `*`, and store it as `self.reason = reason` beside the existing three attributes.

7. Add the funnel to `_DiagnosticCapture`, after `append`:

   ```python
   def withhold(
       self, slot: int, unit: str, reason: WithholdReason, *, phase: SlotPhase | None = None
   ) -> SlotResult:
       """Record one withheld slot, emit its marker, and name its fixed-form cause."""
       self.withheld_slots.add(slot)
       marker = _WITHHELD_TEMPLATE.format(slot=slot, reason=reason.value)
       self.append("" if _contains_forbidden(marker, tuple(self.forbidden_values)) else marker)
       detail = "" if phase is None else f"; phase={phase.value}"
       return SlotResult(
           slot=slot,
           unit=unit,
           code="diagnostics_withheld",
           message=f"withheld: {reason.value}{detail}",
       )
   ```

8. Replace the unsafe-state branch in `_capture_diagnostics` with one call:

   ```python
   if unsafe_state:
       capture.results.append(
           capture.withhold(store.slot, store.unit, WithholdReason.STATE_UNREADABLE)
       )
   ```

9. Replace the two `except` blocks in `_capture_diagnostic_slot`. Moving `aggregate_truncated`
   ahead of the emission is behaviour-preserving, because `append` reads only `emitted`:

   ```python
   except _UnsafeDiagnosticText as exc:
       if exc.used is not None:
           capture.acquired -= reservation - exc.used
       capture.forbidden_values.update(exc.forbidden)
       capture.aggregate_truncated = exc.aggregate_truncated
       return capture.withhold(store.slot, store.unit, exc.reason, phase=state.phase)
   except Exception as exc:
       _log.error(
           "unexpected systemd diagnostic capture failure slot=%s cause=%s",
           store.slot,
           type(exc).__name__,
       )
       capture.acquired -= reservation
       return capture.withhold(
           store.slot, store.unit, WithholdReason.INTERNAL_ERROR, phase=state.phase
       )
   ```

10. Replace the tail of `_capture_diagnostic_slot`:

    ```python
    capture.acquired -= reservation - used
    capture.forbidden_values.update(forbidden)
    capture.aggregate_truncated = aggregate_truncated
    if _contains_forbidden(report, tuple(capture.forbidden_values)):
        return capture.withhold(
            store.slot, store.unit, WithholdReason.PEER_REDACTION_REFUSED, phase=state.phase
        )
    capture.append(report)
    return _result(state)
    ```

11. Name the reason at each raise. Both `except` arms in `_diagnose_slot` pass
    `reason=WithholdReason.ACQUISITION_FAILED` to `_UnsafeDiagnosticText(secret_values, ...)`; the
    raise in `_diagnose_trusted_slot` passes `reason=WithholdReason.REDACTION_REFUSED` alongside
    its existing `used` and `aggregate_truncated` keywords.

12. Drop the now-unreachable keyword from the module-private helper:

    ```python
    def _result(state: SlotState) -> SlotResult:
        return SlotResult(slot=state.slot, unit=state.unit, message=state.phase.value)
    ```

13. Update the six existing assertions the new markers change, in `PYTEST_FILE`:

    | Case | New expectation |
    |---|---|
    | `test_diagnostics_reserves_aggregate_acquisition_for_failed_journals` | the four reached slots each emit `[diagnostics withheld for slot N: acquisition_failed]\n` ahead of `[aggregate diagnostics truncated]\n` |
    | `test_failed_journal_aggregate_marker_respects_known_forbidden_values` | the four withheld markers are emitted; only the aggregate marker is suppressed by the forbidden value `"aggregate"` |
    | `test_diagnostics_withholds_unsafe_source_without_reading_its_journal` | `[diagnostics withheld for slot 1: internal_error]\n` |
    | `test_diagnostics_withholds_unsafe_state_without_exposing_error_detail` | `[diagnostics withheld for slot 1: state_unreadable]\n` |
    | `test_diagnostics_withholds_oversized_redaction_value` | `[diagnostics withheld for slot 1: internal_error]\n` |
    | `test_diagnostics_emits_no_fallback_when_truncation_text_collides` | the contract is that no acquired material is emitted, not that nothing is: the emitted text is `""` or exactly `[diagnostics withheld for slot 1: redaction_refused]\n`, the secret is absent from it, and `response.slots[0].message` names `redaction_refused` |

14. Run `uv run python -m pytest tests/processes/lifecycle/systemd/ -q`. Expect every case green,
    the protocol-identity pin included. Then `just lint` and `just type`, expecting exit 0 each.

15. Confirm the identity is unmoved. The contract module is not in this task's file map, so prove
    that directly rather than by re-running the hash:
    `git diff --stat origin/main...HEAD -- src/kdive/processes/lifecycle/systemd/systemd_worker_contract.py`.
    Expect no output; the pinned case in step 14 is the second half of the proof.

16. Stage, run `prek run`, re-add exactly the recorded staged paths, and commit
    `fix(lifecycle): name the cause of every diagnostics withholding`.

**Acceptance.** Five distinct reasons, one per cause; every reason a literal enum member; the
marker suppressed on a forbidden collision; `lifecycle_protocol_identity()` unchanged; lint, type,
and the module's tests green.

## Task 2 — define the `RetryAction` vocabulary

Modifies `deploy/systemd/README.md`.

**Interfaces.** Consumes the five reason strings from Task 1 and the `RetryAction` literals in
`src/kdive/processes/lifecycle/systemd/systemd_worker_contract.py`. Task 3 links to the
`## Lifecycle retry actions` section this task adds.

### Verification inventory

- **`deploy/systemd/README.md` defines all six `RetryAction` values.** Mode:
  `task-test-not-applicable`. The changed surface is operator-readable prose with no executable
  consumer; its machine-checkable properties are link targets and referenced repository paths,
  which `docs-links`, `docs-paths`, `served-doc-links`, and `docs-check` already validate in
  `just ci`. A test over the prose would pin wording, not behaviour.

### Steps

1. Read `deploy/systemd/README.md` and `_map_failure` in
   `src/kdive/processes/lifecycle/systemd/systemd_worker_lifecycle.py`, which chooses five of the
   six values. The sixth, `none`, comes from `_ok_response` and from a successful `diagnostics`
   capture.

2. Append a `## Lifecycle retry actions` section after `## Fixed live-worker lifecycle contract`.
   Introduce it as the `retry_action` field of every `scripts/live-stack/worker-lifecycle.sh`
   response, then give one table row per value with what it means and the action it asks for, each
   grounded in the condition that produces it: `none` from a successful response;
   `correct_request` from an invalid start request; `retry_same_operation` from a deadline or
   rejected termination evidence; `restore_systemd` from `SystemdUnavailable`; `restore_database`
   from an unavailable database authority; `operator_recovery` from every `conflict`, from an
   unmapped internal error, and from a `diagnostics` capture that withheld a slot.

3. Under the table, state that `operator_recovery` means no retry of the same request will clear
   the condition: the operator inspects `status` and `diagnostics`, then runs
   `scripts/live-stack/worker-lifecycle.sh recover`. Link the procedure Task 3 adds at
   `../../docs/operating/runbooks/live-stack.md#recovering-a-wedged-worker-slot`.

4. Run `just docs-links` and `just docs-paths`, expecting exit 0 each. Stage, run `prek run`,
   re-add exactly the recorded staged paths, and commit
   `docs(systemd): define the lifecycle retry actions`.

**Acceptance.** All six values defined against the condition that emits them; `operator_recovery`
resolves to a definition and to the recovery procedure; doc guards green.

## Task 3 — the wedged-slot recovery procedure

Modifies `docs/operating/runbooks/live-stack.md`.

**Interfaces.** Consumes the five reason strings from Task 1 and the `#lifecycle-retry-actions`
anchor from Task 2. Nothing later depends on this task.

### Verification inventory

- **The runbook carries a wedged-slot procedure matching the shipped `recover`.** Mode:
  `task-test-not-applicable`. Same reason as Task 2: operator prose with no executable consumer,
  whose links and referenced paths are covered by the four doc guards in `just ci`.

### Steps

1. Read section 4 of `docs/operating/runbooks/live-stack.md`, `recover` and `_recover_slot` in
   `src/kdive/processes/lifecycle/systemd/systemd_worker_lifecycle.py`, and the `recover`
   paragraphs of `docs/adr/0657-a-successor-invocation-is-terminal-evidence.md`. Describe what the
   code does now, not what it could do.

2. Add a `### Recovering a wedged worker slot` subsection to section 4, after the paragraph
   stating the request deadlines and diagnostic budgets, covering in order:

   - **The symptom.** `stack-services.sh` fails, or a lifecycle request returns
     `retry_action=operator_recovery` — most often `code=conflict` after a worker unit was
     restarted outside the lifecycle contract, leaving it `failed` with its `InvocationID`
     retained so the next `start` is refused.
   - **Read the cause first.** `scripts/live-stack/worker-lifecycle.sh diagnostics`, then the
     per-slot `message`: a withheld slot reads `withheld: <reason>; phase=<phase>`, or
     `withheld: state_unreadable` when no state could be loaded. Table the five reasons with
     meaning and action — `state_unreadable`: slot files unreadable or malformed, check ownership
     under `/var/lib/kdive/live-workers`; `acquisition_failed`: systemd or the journal did not
     answer, check `systemctl status` and `journalctl` for the unit; `redaction_refused` and
     `peer_redaction_refused`: the report held a value the redactor may not emit, read the unit's
     journal on the host directly; `internal_error`: unexpected, the witness log names the
     exception type.
   - **Recover.** `scripts/live-stack/worker-lifecycle.sh recover`. Per slot it observes the unit
     and refuses one whose cgroup still holds live processes, returning `code=conflict`,
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

3. Link the new subsection to the retry-action table at
   `../../../deploy/systemd/README.md#lifecycle-retry-actions`.

4. Run `just docs-links`, `just docs-paths`, and `just served-doc-links`, expecting exit 0 each.
   Stage, run `prek run`, re-add exactly the recorded staged paths, and commit
   `docs(runbook): add the wedged worker slot recovery procedure`.

**Acceptance.** An operator can go from an `operator_recovery` response to a running stack with
only the shipped contract; every reason maps to an action; the residual cases are named as out of
reach with their owning issue; doc guards green.

## Deferrals

None.
