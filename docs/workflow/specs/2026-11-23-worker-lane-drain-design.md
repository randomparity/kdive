# Observe worker lane failures after graceful drain

## Problem

Worker supervision waits for pending lanes during graceful stop, but discards
those outcomes and inspects only the first completed set. A later lane failure
can therefore become a successful worker exit. A deterministic two-lane probe
at ff0667c9 reproduces this with the real Worker.run path and an injected
run_once boundary: the normal lane finishes after stop, then its sibling fails.

## Scope

Repair Worker._run_lane_loops in src/kdive/jobs/worker.py under the existing
per-lane supervision contract (ADR-0550). Capture the stop decision when the
first completion is observed. Keep the existing wait for pending lanes. On
graceful stop, inspect results from the started lane set after it has drained.
On early lane death, retain sibling cancellation and inspection of the original
completed set, so expected sibling cancellation cannot mask that failure.

Preserve lane dispatch, heartbeat cleanup, job handling, leases and public
configuration. This is a correction of the existing supervision decision;
no new task supervisor, exception group or persisted state is needed.

### Failure model

- Prevent: a failure from a pending lane becoming a successful graceful exit.
- Detect: cancellation replacing the original early-death failure, and returning
  before a finite in-flight lane finishes, through existing supervision tests.
- Recover: propagate the original lane exception after the drain; the existing
  process supervisor owns restart. Revert this local correction if necessary.
- Accepted: selection among simultaneous lane failures remains unspecified, as
  before. Parent-task cancellation retains its existing cleanup path; this
  change covers the stop-event and early-lane-death branches.

## Success

1. With either default lane failing after the other exits on stop, Worker.run
   raises that same exception object after the pending lane finishes.
2. Early lane death still cancels its sibling and propagates the original error.
3. The existing real-database proof still finishes a running job during stop;
   the ordinary shared-stop proof still terminates both entered lanes.

## Validation

- Mode: focused-test. Add parameterized late-failure cases to
  tests/jobs/test_worker_lanes.py. Gate both entries, observe the real first
  completion, then release the failing lane. Expected red: Worker.run returns
  successfully. A test-local asyncio-module copy isolates the scheduler observer.
- Mode: focused-test. Run the existing early-death, shared-stop and real-job-drain
  cases unchanged with the new cases: KDIVE_REQUIRE_DOCKER=1 just test-verbose
  tests/jobs/test_worker_lanes.py. Expect twelve passing cases.
- Mutate result inspection back to the first set, inspect cancelled siblings,
  and cancel pending lanes on graceful stop; require focused failures and restore.
- Run just lint, whole-tree just type, documentation checks and installed hooks.
