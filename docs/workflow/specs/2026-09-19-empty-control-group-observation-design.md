# Empty-control-group systemd observation

Issue #2596.

## Problem

`SystemdRuntime.observe` accepts an empty `ControlGroup` only for failed units or for an
`active/exited` retained unit whose `Result` is `success` and whose `ExecMainStatus` is zero. The
second rule does not match systemd's observed rollback behavior. On a provisioned host, a first
worker terminated during rollback after a forced second-slot activation failure produced:

```text
ActiveState=active
SubState=exited
Result=success
ExecMainStatus=15
ControlGroup=
InvocationID=<INVOCATION-R1>
```

The invocation ID was present and valid. Observation nevertheless raised the misleading
`partial unit identity` conflict before status, stop, or recover could classify the dead process.

## Scope

The systemd runtime remains the sole owner of property-shape validation. Rename its private
empty-cgroup validator from identity-oriented wording to state-oriented wording. Accept these two
closed shapes after the existing property, invocation-ID, and numeric-status validation:

1. `ActiveState=failed`, `SubState=failed`, and `Result != success`;
2. `ActiveState=active`, `SubState=exited`, and `Result=success`, for every already-validated
   `ExecMainStatus` in `0..255`.

The second shape reflects the observed `RemainAfterExit=yes` contract: an empty cgroup proves no
process remains, while `ExecMainStatus` records how the process exited. The lifecycle layer keeps
owning the outcome mapping. Existing `_outcome` behavior therefore records `success/0` as
`succeeded` and `success/nonzero` as `failed`; this change neither fabricates evidence nor borrows
another invocation's result.

Every other empty-cgroup combination is refused with a message containing only the validated
`ActiveState`, `SubState`, `Result`, and `ExecMainStatus` values. The message does not call the
identity partial and does not expose the invocation ID. A nonempty cgroup keeps its exact path and
membership checks unchanged. Inactive units with empty invocation identity remain
`BootObservation`; unreadable or malformed identity remains refused under ADR-0657.

No ownership transition is needed. `SystemdRuntime.observe` already owns systemd parsing and is
shared by start, status, stop, and recover. Those callers consume the corrected observation
without new branches or compatibility paths.

### Failure model

- **Actors and deployments:** a local operator drives the root lifecycle witness on fixed-slot
  systemd hosts; unprivileged callers cannot select arbitrary units or properties.
- **Assets and invariants:** a valid invocation ID remains mandatory; only an empty cgroup proves
  the observed invocation has no live process; another invocation's result is never attributed to
  retained state.
- **Accepted failures:** unrecognized state tuples remain operator-recovery conflicts, now with
  the four bounded state values needed to diagnose them.
- **Covered elsewhere:** #2533 owns residual slot/fence recovery, and ADR-0657 owns unreadable
  identity, termination-evidence, and cross-invocation rules.

## Success

1. Runtime matrix tests accept the reproduced `active/exited`, `success`, status-15 tuple and the
   existing failed and successful retained shapes.
2. Matrix tests refuse unsupported empty-cgroup combinations and include all four state values in
   the conflict while omitting `partial identity` and the invocation ID.
3. Lifecycle tests prove status, stop, and recover consume the reproduced observation without
   weakening live-process or identity refusal.
4. Repeating the second-slot activation failure on a provisioned host cleans the first slot rather
   than leaving it unobservable, and leaves no active local fence or slot files after cleanup.
5. Focused tests and `just ci` pass without protocol, schema, migration, or ADR changes.

## Validation

- **Runtime accepted/refused state matrix — Mode: focused-test.** Add parametrized cases in
  `tests/processes/lifecycle/systemd/test_systemd_worker_runtime.py`. The reproduced tuple fails
  before the implementation change with `systemctl show returned a partial unit identity`; the
  focused file passes afterward with `just test-verbose
  tests/processes/lifecycle/systemd/test_systemd_worker_runtime.py`.
- **Status, stop, and recover consumption — Mode: focused-test.** Add focused coordinator cases in
  `tests/processes/lifecycle/systemd/test_systemd_worker_lifecycle.py` using the reproduced
  `UnitObservation`. Before the runtime change, an end-to-end runtime observation cannot reach
  these paths; afterward each path applies its existing outcome and cleanup contract. Run `just
  test-verbose tests/processes/lifecycle/systemd/test_systemd_worker_lifecycle.py`.
- **Provisioned-host regression — Mode: focused-test.** Extend
  `tests/live_vm/test_systemd_worker_lifecycle.py` with a bounded second-slot failure arm and
  exact removal of its uniquely named runtime-only drop-in. Run it only under the quest
  operator's outer disposable-host controller: the controller preserves the test exit status,
  always removes that exact drop-in, and uses reboot plus documented different-boot recovery if
  ordinary cleanup or any postcondition fails. Before the change the start leaves slot 1 in the
  reproduced unobservable state; afterward rollback clears both units and ordinary cleanup leaves
  no slot files or active matching rows. The controller must additionally verify both units
  inactive with empty identity, no proof drop-in, and stopped backends before releasing the host.
  Run the named test under the documented `KDIVE_RUN_SYSTEMD_WORKER_PROOF=1` live environment.
