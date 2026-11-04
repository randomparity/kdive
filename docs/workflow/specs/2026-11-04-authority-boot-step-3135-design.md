# Design — an authority activation records the boot step (#3135)

## Problem

The fixed-worker `boot` handler writes the `boot` run step. The external-boot authority commit is a
database function that never touches `run_steps`, so an authority boot leaves none.
`debug.start_session` and `introspect.script` refuse such a Run with `boot_first`, and the x86_64
live spine cannot attach.

## Scope

- The worker, after `queue.complete_external_boot` returns a job in `succeeded` for an `activate`
  result, inserts the succeeded `boot` step `{system_id, boot_outcome: ready}` (the fixed
  handler's shape) under the System and Run locks, only if the result's activation is still the
  Run's `active` one. The attach precondition is unchanged.
- Rejected: writing it inside the commit function (needs a migration, out of scope); writing it
  in the activate handler before the commit (a refused commit would leave a phantom step).
- No ownership change is needed: `Worker._commit_external_result` already owns the commit.

### Failure model

- Activation fails, is superseded or requeued, or the result is not `activate`: no step.
- The step write fails after the commit: the write is retried three times; if all fail the error is logged and the job stays succeeded, and attach keeps refusing
  with `boot_first`. A worker crash in that window is not repaired (a reconciler backfill is a
  larger surface, left unbuilt). A step already present is left alone.

## Success

- A committed activation yields a succeeded `boot` step that `_attach_preconditions` accepts.
- Failed, queued and `deadline` results yield none, and the fixed-worker path is untouched.
- The x86_64 spine passes through the installed authority.

## Validation

- focused-test: `tests/jobs/test_worker_authority_boot_step.py` covers the success row and the
  failed, queued and `deadline` negatives against a real database.
- task-test-not-applicable: the live spine proof is an operator run on the lab host, not a unit test.
