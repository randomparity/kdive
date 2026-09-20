# Empty-control-group systemd observation — implementation plan

**Goal:** Accept the reproduced empty-cgroup retained-unit state, make unsupported-state conflicts
diagnostic, and prove status, stop, recover, and partial-start rollback behavior.

**Architecture:** Keep systemd state validation in `SystemdRuntime.observe`. Broaden only the
existing `active/exited` retained state after exact property and invocation validation. Existing
lifecycle outcome and identity code consumes the resulting `UnitObservation` unchanged.

**Tech stack:** Python 3.14, pytest, systemd, `uv`, `just`.

Expected implementation size: 260–300 changed lines (M) — corrected after implementation measured
275 changed source/test lines. The original range undercounted the accepted/refused matrix rewrite,
the three explicit lifecycle-consumer proofs, and fail-safe live-host rollback assertions; all remain
inside the reviewed four-file surface and fixed M denominator.

## Global constraints

- Base branch: `main`; branch: `feat/observe-empty-cgroup-terminal-2596`; sibling worktree only.
- Host is x86_64; declared native targets are x86_64 and ppc64le.
- Preserve ADR-0657 identity binding, #2533 residual cleanup ownership, every protocol model,
  schema, migration, and termination/fence contract.
- Change only the frozen source/test surface and these dated design artifacts.
- Public artifacts use `sys-R1` and `<INVOCATION-R1>`; they never publish private host or user
  identifiers.
- Iterate with focused `just test-verbose` commands, `just test-changed`, `just lint`, and `just
  type`; run exact-head `just ci > <private-log> 2>&1 < /dev/null` before push.

## Task 1: Correct the empty-cgroup state matrix

**Files:** modify `src/kdive/processes/lifecycle/systemd/systemd_worker_runtime.py` and
`tests/processes/lifecycle/systemd/test_systemd_worker_runtime.py`.

**Interfaces**

- Rename private `SystemdRuntime._require_released_terminal_identity(properties)` to
  `_require_released_terminal_state(properties) -> None` and update its one caller.
- `SystemdRuntime.observe(unit, deadline) -> UnitObservation | BootObservation` remains unchanged.
- Task 2 consumes only the unchanged `UnitObservation` fields.

**Verification**

- **Accepted matrix — Mode: focused-test.** Parametrize failed/failed nonsuccess, active/exited
  success/0, and active/exited success/15. The new status-15 case initially raises
  `SystemdConflict`; all cases pass with `just test-verbose
  tests/processes/lifecycle/systemd/test_systemd_worker_runtime.py`.
- **Refused matrix and message — Mode: focused-test.** Parametrize active/exited exit-code,
  active/running success with empty cgroup, and failed/failed success. Assert the conflict contains
  the exact four state fields, excludes `partial unit identity`, and excludes the invocation ID.
  Use the same focused command.

**Steps**

1. Replace the two narrow runtime tests with a table that constructs complete `systemctl show`
   output from `(active_state, sub_state, result, status)` and observes an empty cgroup with one
   valid invocation ID. Run the focused command and retain the expected status-15 red result.
2. Rename the helper and implement the matrix without a status predicate on the
   `active/exited/success` arm:

   ```python
   failed = active == "failed" and sub == "failed" and result != "success"
   retained_exit = active == "active" and sub == "exited" and result == "success"
   if not failed and not retained_exit:
       raise SystemdConflict(
           "systemd released cgroup in unsupported state "
           f"ActiveState={active} SubState={sub} Result={result} "
           f"ExecMainStatus={properties['ExecMainStatus']}"
       )
   ```

   Continue relying on `observe`'s preceding complete-property, invocation-ID, and numeric-status
   validation; do not duplicate or relax those checks.
3. Run the focused file, `just lint`, and `just type`; commit the runtime and matrix tests.

Acceptance: the reproduced tuple produces a same-invocation empty `UnitObservation`; unsupported
tuples name state rather than identity; nonempty, inactive, malformed, and unreadable paths are
unchanged.

Rollback: revert this task commit; no persisted state changes.

## Task 2: Pin lifecycle consumers and live rollback

**Files:** modify `tests/processes/lifecycle/systemd/test_systemd_worker_lifecycle.py` and
`tests/live_vm/test_systemd_worker_lifecycle.py`.

**Interfaces**

- Reuse existing `_observation(..., result="success", status=15, membership="empty")` in the
  coordinator test double.
- Add one live test that installs a uniquely named runtime-only slot-2 `ExecStart=/bin/false`
  drop-in, invokes `_lifecycle_result("start", 2)`, and removes only that drop-in in `finally`
  before fleet cleanup.
- Production lifecycle interfaces remain unchanged.

**Verification**

- **Status/stop/recover outcomes — Mode: focused-test.** Add named coordinator cases proving the
  reproduced observation reaches `_outcome`, records `failed` for `success/15`, and lets the
  existing stop and recover paths clear a dead retained slot. Before Task 1 an actual
  `SystemdRuntime` cannot construct this observation; the focused lifecycle file passes after it.
  Run `just test-verbose tests/processes/lifecycle/systemd/test_systemd_worker_lifecycle.py`.
- **Second-slot rollback — Mode: focused-test.** The live test asserts start returns the expected
  conflict, then both units become inactive with empty identities and no slot artifacts remain.
  Before Task 1 the first slot remains `active/exited` and cleanup cannot observe it. Run only its
  node ID with the documented provisioned-host gate and environment, inside the outer cleanup
  controller described below.

**Steps**

1. Add status, stop, and recover tests using a same-invocation `success/15` empty observation.
   Assert the authority receives `(incarnation, "failed")`; stop/recover clear the existing slot
   state through their ordinary paths; a populated variant remains refused.
2. Add the live regression with direct argument-array subprocess calls. Feed this exact override
   to `sudo systemctl edit --runtime --drop-in=kdive-live-proof-2596.conf --stdin
   kdive-live-worker@2.service`, after verifying the exact target path under `/run/systemd/system`
   does not already exist:

   ```ini
   [Service]
   ExecStart=
   ExecStart=/bin/false
   ```

   In `finally`, remove only
   `/run/systemd/system/kdive-live-worker@2.service.d/kdive-live-proof-2596.conf`, then run
   daemon-reload and the existing `_reset_fleet`. Never call unit-wide `systemctl revert`, which
   can remove operator-owned persistent and runtime drop-ins. Assert cleanup even when the start
   assertion fails; never retain a host-specific identifier in test output. This in-test cleanup
   is the ordinary path, not the fail-safe owner: a rejected candidate can make `_reset_fleet`
   fail through the same classifier.
3. Run both focused unit files, `just test-changed`, `just lint`, and `just type`. Stage the exact
   four implementation/test paths and two design artifacts, run `prek run`, re-add only those
   paths if rewritten, and commit review-driven formatting separately if needed.
4. Run `just ci > .agent/sdd/ci-2596.log 2>&1 < /dev/null` as the final local gate. On the
   provisioned host, install the exact candidate and run the live node ID from an outer quest
   operator controller that records the test exit status separately from cleanup. In an outer
   `finally`, remove only the named proof drop-in and attempt ordinary fleet cleanup. If that
   cleanup or a postcondition fails, reboot the disposable host, wait for it to return, start only
   the required backends, and run the documented different-boot recovery. Before releasing the
   host, verify both units inactive with empty identities, zero slot files, zero active local rows,
   no proof drop-in, and stopped backend containers. Report the original test result and any
   cleanup failure independently so successful recovery cannot turn a red test green.

Acceptance: all three lifecycle consumers retain their existing contracts, the live partial-start
rollback no longer wedges slot 1, and the host returns to its pre-proof stopped state.

Rollback: the outer controller removes the exact proof drop-in and uses the existing different-boot
recovery procedure if a pre-fix wedge is observed; revert the branch commits for source rollback.
