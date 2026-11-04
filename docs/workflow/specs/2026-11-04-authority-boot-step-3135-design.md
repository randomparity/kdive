# Design — an authority activation records the boot step (#3135)

## Problem

The fixed-worker `boot` handler writes the `boot` run step. The external-boot authority commit is a
database function that never touches `run_steps`, so an authority boot leaves none.
`debug.start_session` and `introspect.script` refuse such a Run with `boot_first`, and the x86_64
live spine cannot attach.

## Scope

- The worker, after `queue.complete_external_boot` returns a job in `succeeded` for a result whose
  operation is `activate`, claims and completes the `boot` step with `{system_id, boot_outcome:
  ready}`, the shape the fixed handler writes. The attach precondition is unchanged.
- Rejected: writing it inside the commit function (needs a migration, out of scope); writing it
  in the activate handler before the commit (a refused commit would leave a phantom step).
- No ownership change is needed: `Worker._commit_external_result` already owns the commit.

### Failure model

- Activation fails, is superseded or requeued, or the result is not `activate`: no step.
- The step write fails after the commit: the error is logged and the job stays succeeded; attach
  keeps refusing with `boot_first`. A step already present is left alone (`claim_run_step`).

## Success

- A committed activation yields a succeeded `boot` step that `_attach_preconditions` accepts.
- Failed, queued and `deadline` results yield none, and the fixed-worker path is untouched.
- The x86_64 spine passes through the installed authority.

## Validation

- focused-test: `tests/jobs/test_worker_authority_boot_step.py` covers the success row and the
  failed, queued and `deadline` negatives against a real database.
- task-test-not-applicable: the live spine proof is an operator run on the lab host, not a unit test.
