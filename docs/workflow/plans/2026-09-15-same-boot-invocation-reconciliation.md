# Same-boot invocation reconciliation — implementation plan

Derived from
[the design](../specs/2026-09-15-same-boot-invocation-reconciliation-design.md) and
[ADR-0657](../../adr/0657-a-successor-invocation-is-terminal-evidence.md). Issue #2485.

**Goal.** Make a retained worker slot whose unit was restarted on the same host boot reconcilable
through the shipped `start` / `status` / `stop` contract instead of failing with
`LifecycleConflict`.

**Architecture.** `SystemdWorkerLifecycle` is a request-scoped coordinator over eight fixed systemd
slots. Every operation reads one `UnitObservation` or `BootObservation` per slot and asks the
module-level `_terminal_observation(state, observation)` whether the retained slot has terminal
evidence; it returns a `TerminationOutcome`, `None` (still running), or raises. Only that function
changes. Stack: Python 3.14, `uv`, pytest, `ruff`, `ty`, `just`.

## Global Constraints

- Ruff line length 100, lint set `E,F,I,UP,B,SIM`; `ty` runs whole-tree (src + tests), do not narrow
  it. Doc style: "Milestone" never "Sprint"; plain factual prose; avoid critical, robust,
  comprehensive, essential, significant, elegant.
- Branch `feat/reconcile-same-boot-invocation-2485`; `BASE_BRANCH` is `main`. Guardrails:
  `just lint`, `just type`, `just test-changed` and `just test-lf` while iterating,
  `just ci > /tmp/ci-2485.log 2>&1 < /dev/null` as the pre-push gate run bare, and
  `git fetch origin main` then `just records` for the decision records.
- Before `git commit`: `just format` for Python-only changes. For a commit also touching Markdown,
  `git diff --cached --name-only` first, then `prek run`, then re-add exactly those paths — never
  `git add -A` or `git add -u`.
- `adr-status-check` (`justfile:472-473`, in `just ci`) fails a `Proposed` ADR cited from `src/` or
  `tests/`, so ADR-0657 is `Accepted` in this PR. `.github/scripts/check-records.sh:349` reports
  `E-BANNER-COUNT` for a second resolution banner and ADR-0574 already carries one, so its change
  here is an appended amendment block, never a banner. No ADR index row exists (ADR-0504).
- Every focused green command below has the same form — write `<case>` and run it verbatim:
  `uv run python -m pytest tests/processes/lifecycle/systemd/test_systemd_worker_lifecycle.py::<case> -q`,
  expecting `1 passed`. `<module>` below means that same file.
- Owned by sibling issues, not to be edited: `deploy/systemd/**`, `systemd_worker_contract.py`,
  `systemd_worker_control.py`, `systemd_worker_state.py`, `systemd_diagnostics.py`,
  `scripts/live-stack/**`, `deploy/ansible/**`, `tests/integration/**`, `tests/scripts/**`,
  `docs/operating/**`.

Expected implementation size: 120–190 changed lines (M) — derived from the file map and task list
below: about 10 lines in the coordinator, about 130 in the test module, about 14 in ADR-0574;
ADR-0657 and these design artifacts are excluded.

## File map

- `src/kdive/processes/lifecycle/systemd/systemd_worker_lifecycle.py` — owns the terminal-evidence
  rules for a retained slot; keeps them, with the same-boot invocation mismatch returning `killed`
  for the retained binding.
- `tests/processes/lifecycle/systemd/test_systemd_worker_lifecycle.py` — owns the coordinator's
  replay and evidence-ordering tests; gains the changed rule and each unchanged rule pinned.
- `docs/adr/0657-a-successor-invocation-is-terminal-evidence.md` — new; owns the fencing decision.
- `docs/adr/0574-systemd-supervises-host-worker-incarnations.md` — owns the retained-incarnation
  decision; gains one appended amendment naming ADR-0657.

No file is created, moved, or removed beyond the new ADR. No caller migrates: the function keeps
its name and signature, and the raise it drops is not observed outside the module.

## Task 1 — a same-boot successor invocation is terminal evidence for the retained one

**Where this fits.** The whole behavioural change. Everything else in this plan records it.

**Interfaces.** Consumes, unchanged and already present in the repository:
`_terminal_observation(state: SlotState, observation: UnitObservation | BootObservation) -> TerminationOutcome | None`
in the coordinator module; `UnitObservation(unit, boot_id, invocation_id, active_state, sub_state,
result, exec_main_status, control_group, membership)` and `BootObservation(unit, boot_id)`, frozen
dataclasses in `src/kdive/processes/lifecycle/systemd/systemd_worker_runtime.py`;
`type TerminationOutcome = Literal["succeeded", "failed", "killed"]` in
`src/kdive/worker_lifecycle/contracts.py`; and these helpers already in `<module>`:
`_state(slot, phase, *, generation=None, credential_hash="c"*64, boot_id=_BOOT_ID, invocation_id=None, outcome=None)`,
`_observation(slot, membership, *, boot_id=_BOOT_ID, invocation_id=None, result="success", status=0)`,
`_boot_observation(slot, *, boot_id=_BOOT_ID)`,
`_fleet(*, states=None, releases=None) -> (stores, runtime, authority, clock, events)`,
`_coordinator(stores, runtime, authority, clock)`, `_run(coroutine)`, `_deadline(clock)`,
`_request(worker_count=1)`. Nothing later relies on a new name from this task.

**Verification** — all `Mode: focused-test`, in `<module>`, green by the command form above.

- *A same-boot successor invocation is terminal evidence for the retained invocation and reconciles
  through `stop`.* Case `test_same_boot_successor_invocation_retires_the_retained_incarnation`,
  replacing `test_stale_same_boot_invocation_is_refused_without_signaling_or_cleanup`. Red before
  the source edit: `assert response.ok` fails because `response.code` is `"conflict"`.
- *The same mismatch reconciles through `start`.* Case
  `test_start_reconciles_a_restarted_unit_and_replaces_the_slot`. Red before the source edit:
  `assert response.ok` fails with `response.code == "conflict"`.
- *The successor's exit facts are not attributed to the retained incarnation.* Case
  `test_successor_invocation_exit_facts_are_not_attributed_to_the_retained_one`. Red before the
  source edit on `response.code == "conflict"`, and red against a wrong fix returning
  `_outcome(observation)`, which publishes `"failed"` instead of `"killed"`.

**Steps**

1. In `tests/processes/lifecycle/systemd/test_systemd_worker_lifecycle.py`, replace
   `test_stale_same_boot_invocation_is_refused_without_signaling_or_cleanup` with
   `test_same_boot_successor_invocation_retires_the_retained_incarnation`: build
   `started = _state(1, SlotPhase.STARTED)`, `_fleet(states={1: started})`, set
   `runtime.current[started.unit] = _observation(1, "populated", invocation_id="f" * 32)`, run
   `stop`, then assert `response.ok`, `authority.terminations == [(started.incarnation, "killed")]`,
   `stores[0].state is None`, `not stores[0].environment and not stores[0].credential and not stores[0].release`,
   and `runtime.stopped == [started.unit]`. Assert `runtime.signaled == []`: the successor is not
   signalled, because the retained invocation already has terminal evidence.
2. Add `test_start_reconciles_a_restarted_unit_and_replaces_the_slot` in the same module: same
   setup, call `start(_request(), _deadline(clock))`, assert `response.ok`,
   `authority.terminations[0] == (started.incarnation, "killed")`, and that
   `stores[0].state is not None and stores[0].state.phase is SlotPhase.STARTED` with
   `stores[0].state.generation != started.generation`.
3. Add `test_successor_invocation_exit_facts_are_not_attributed_to_the_retained_one`: set
   `runtime.current[started.unit] = _observation(1, "empty", invocation_id="f" * 32, result="exit-code", status=2)`,
   run `status`, and assert `authority.terminations == [(started.incarnation, "killed")]` —
   `"failed"` would mean the successor's `result` reached the retained incarnation.
4. Run
   `uv run python -m pytest tests/processes/lifecycle/systemd/test_systemd_worker_lifecycle.py -q -k "successor or restarted_unit"`.
   Expect 3 failed, each on an assertion about `response.ok` or `authority.terminations`.
5. In `src/kdive/processes/lifecycle/systemd/systemd_worker_lifecycle.py`, replace the body line

   ```python
       if observation.invocation_id != state.invocation_id:
           raise LifecycleConflict("systemd invocation does not match retained state")
   ```

   with

   ```python
       if observation.invocation_id != state.invocation_id:
           # A unit carries one invocation at a time and is assigned a new INVOCATION_ID only
           # when it leaves an inactive state, so a successor identity on the retained boot
           # proves the retained invocation ended. Its own exit facts are gone with it, and the
           # observed result and membership belong to the successor, so they are not mapped here
           # (ADR-0657, amending ADR-0574).
           return "killed"
   ```

6. Extend the module docstring at line 1 of the same file to cite the record, so
   `adr-status-check` sees ADR-0657 cited from `src/`:

   ```python
   """Replay-safe coordination for retained systemd worker incarnations (ADR-0574, ADR-0657)."""
   ```

7. Re-run step 4's command. Expect `3 passed`.
8. Run `uv run python -m pytest tests/processes/lifecycle/systemd/ -q`. Expect every test in the
   directory to pass.

**Acceptance criteria.** `_terminal_observation` returns `"killed"` for a same-boot successor
invocation and raises nothing; the three tests above pass; no other rule in the function is edited;
the module docstring cites ADR-0657.

## Task 2 — pin the rules that must not change

**Where this fits.** Issue #2485 acceptance criterion 2: each unchanged rule keeps a test, so a
later edit to the function cannot move one silently. `rg` finds no test holding the foreign-unit
rule or the bound-phase membership-`unknown` rule today.

**Interfaces.** Consumes the same helpers listed in Task 1. Defines no new name.

**Verification** — all `Mode: focused-test`, in `<module>`, green by the command form above. Each
red is produced by the named controlled fault in `_terminal_observation`, reverted afterwards.

- *A foreign unit observation raises `LifecycleConflict`.* Case
  `test_foreign_unit_observation_is_refused_without_evidence`. Fault: delete the
  `observation.unit != state.unit` check; the run then reaches the invocation rule and returns `ok`
  instead of `conflict`.
- *A differing boot ID publishes `killed`, for an absent unit and for a live-looking one.* Case
  `test_reboot_maps_exact_retained_binding_to_killed`, extended. Fault: delete the boot-ID rule; the
  `BootObservation` case then raises `SystemdUnavailable` and the added `populated` case returns
  `ok` with no termination, because the retained invocation matches and `populated` is not terminal.
- *A `BootObservation` on the retained boot raises `SystemdUnavailable`.* Case
  `test_same_boot_inactive_unit_is_not_terminal_evidence`, unchanged. Fault: delete that branch; the
  case then fails on the `BootObservation` having no `invocation_id` attribute.
- *`membership == "unknown"` on the retained invocation raises `SystemdUnavailable`.* Case
  `test_unknown_membership_on_the_retained_invocation_is_not_terminal_evidence`. Fault: delete the
  `membership == "unknown"` line; `_outcome` then runs and the response is `ok`.

**Steps**

1. Add `test_foreign_unit_observation_is_refused_without_evidence`: build
   `started = _state(1, SlotPhase.STARTED)` and `_fleet(states={1: started})`, then set
   `runtime.current[started.unit] = _observation(2, "populated")` — an observation naming
   `kdive-live-worker@2.service` returned for slot 1's unit. Run `status` and assert
   `(response.code, response.retry_action) == ("conflict", "operator_recovery")`,
   `authority.terminations == []`, and `stores[0].state == started`.
2. Extend `test_reboot_maps_exact_retained_binding_to_killed`: after the existing
   `BootObservation` case, add a second case on a fresh fleet using
   `_observation(1, "populated", boot_id=_NEXT_BOOT_ID)` — the retained invocation identity, a
   differing boot, and a cgroup that still looks live — and assert
   `authority.terminations == [(started.incarnation, "killed")]`. The prior boot's cgroup cannot
   survive a reboot, which is why the boot-ID rule decides this case before membership is read.
3. Add `test_unknown_membership_on_the_retained_invocation_is_not_terminal_evidence`: build
   `started = _state(1, SlotPhase.STARTED)`, set
   `runtime.current[started.unit] = _observation(1, "unknown")` — the retained invocation, so the
   changed rule does not apply — run `status`, and assert
   `(response.code, response.retry_action) == ("dependency_unavailable", "restore_systemd")`,
   `authority.terminations == []`, and `stores[0].state == started`.
4. Run
   `uv run python -m pytest tests/processes/lifecycle/systemd/test_systemd_worker_lifecycle.py -q -k "foreign_unit or reboot_maps or inactive_unit_is_not_terminal or unknown_membership"`.
   Expect `4 passed`.
5. Verify each new test bites. One at a time, make the controlled fault named in that test's
   Verification entry in `_terminal_observation`, run the same command, observe the named test red,
   then `git checkout -- src/kdive/processes/lifecycle/systemd/systemd_worker_lifecycle.py` and
   re-run to observe green. Record which fault produced which red.

**Acceptance criteria.** Four tests cover the foreign-unit, boot-ID, absent-boot and
membership-`unknown` rules; each was observed red under a controlled fault and green after revert;
no source file is left modified by step 5.

## Task 3 — record the amendment in ADR-0574 and clear the guardrails

**Where this fits.** Issue #2485 acceptance criteria 3 and 5, and epic #2484 R6. ADR-0657 is
written with the design; this task is the edit to the record it amends, plus the gate.

**Interfaces.** None.

**Verification** — both `Mode: focused-test`.

- *ADR-0657 is `Accepted` while `src/` cites it, and ADR-0574's amendment satisfies the records
  gate.* Green: `just adr-status-check` → exit 0, and `git fetch origin main` then `just records` →
  exit 0. Red: leaving ADR-0657 `Proposed` makes `adr-status-check` report it as
  cited-but-unaccepted; writing the ADR-0574 edit as a second `> **Superseded by …**` banner makes
  `just records` report `E-BANNER-COUNT`.
- *The branch satisfies the repository's PR gate.* Green:
  `just ci > /tmp/ci-2485.log 2>&1 < /dev/null` → exit 0. Red: any lint, type, or test failure
  introduced by Tasks 1-3.

**Steps**

1. In `docs/adr/0574-systemd-supervises-host-worker-incarnations.md`, append the block below to the
   `## Decision` section, immediately after the paragraph ending "It never applies this rule when
   the boot ID is unreadable or unchanged." Its heading is the form `docs/adr/README.md` requires,
   and its link target is a sibling filename because ADR-0574 sits in the same directory.

```markdown
### Amendment (2026-09-15): a successor invocation is terminal evidence (#2485)

This amendment qualifies only the preceding claim that absence within the same host boot is never
termination evidence. [ADR-0657](0657-a-successor-invocation-is-terminal-evidence.md) decides that
a *different* invocation identity reported for the retained unit on the retained boot is the
presence of a successor rather than absence, and therefore proves the retained invocation ended:
the witness records the retained incarnation as `killed`. Absence itself is unchanged — an
observation carrying no invocation identity on the retained boot still yields no termination
evidence — and ADR-0574's gate marker binding, credential, witness, and cleanup decisions remain
in force.
```

2. Change nothing else in ADR-0574: no status-line edit, no banner, no rewrite of existing prose.
   `E-REWRITE` reports a changed line in a merged record.
3. Confirm ADR-0657's `## Status` section reads `Accepted (2026-09-15)`.
4. Run `just adr-status-check`. Expect exit 0 and no report naming 0657 or 0574.
5. Run `git fetch origin main` then `just records`. Expect exit 0.
6. Run `just format`, then `just lint`, then `just type`. Expect exit 0 from each.
7. Stage the change. Run `git diff --cached --name-only`, then `prek run`, then re-add exactly
   those paths, then commit.
8. Run `just ci > /tmp/ci-2485.log 2>&1 < /dev/null` bare, as the last command in its invocation.
   Expect exit 0.

**Acceptance criteria.** ADR-0574 gains exactly one amendment block and no other change; ADR-0657
is `Accepted`; `just adr-status-check`, `just records`, `just lint`, `just type` and `just ci` each
exit 0, with the `just ci` run captured to a file rather than piped.

## Deferrals and what this plan does not prove

No deferral is recorded at plan time; one accepted during review is added here with its owning
record path or tracker issue before the build resumes.

Acceptance criterion 4 — proof on a provisioned systemd host with the installed contract — cannot
be reached from a workstation, because unit tests cannot exercise a `daemon-reexec` or an
out-of-band restart. The run implementing this plan states in the pull-request body which arms it
ran and that the provisioned-host proof is outstanding.
