# Run failure eligibility follows the state machine

## Problem

The shared failure policy admits `CREATED`, but the Run state machine forbids
`CREATED → FAILED`. Dead-lettering an abandoned job with an unstarted Run then
raises `IllegalTransition` and rolls back the job update. The ordinary suite and
an isolated earlier checkout reproduce this failure.

## Scope

Change only the eligible source states in `domain/operations/jobs.py` and its
behavioral tests. Default eligibility is `RUNNING`; `INSTALL` also admits
`SUCCEEDED`. Worker and reconciler already consume this shared function. Keep
the repository's row lock, transition guard, atomic metadata update, and caller
transactions. Keep the state machine and applied SQL unchanged. The existing
ADR-0230 boot-versus-install distinction remains authoritative.

The abandoned-job test currently seeds a `CREATED` Run while expecting failure.
Make its running-Run premise explicit, and add an unstarted-Run case that proves
job dead-lettering with the entire Run record unchanged. Add a bounded policy
law over the declared `JobKind` members and retain install/boot distinctions.
No production caller moves, compatibility aliases, new dependency or migration.
Other twelve baseline failures remain with the branch-gate-repairs owner.

## Success

1. Each source returned for the declared `JobKind` members has a legal edge to
   `FAILED`; default eligibility is exactly `RUNNING` and install additionally
   admits `SUCCEEDED`.
2. The real reconciler dead-letters an exhausted job naming an unstarted Run
   without changing that Run or raising an illegal transition.
3. A running Run and its abandoned job fail together with existing failure
   category and job attribution. Repository illegal-edge rejection stays strict.

## Failure model

- Actors and deployments: KDIVE workers/reconciler on supported Linux x86_64
  and ppc64le deployments; local proof runs on real x86_64 with disposable DB.
- Invariants/assets: legal Run transitions, unchanged unstarted/terminal Runs,
  atomic job compensation and Run failure metadata.
- Accepted classes: malformed payloads and missing Runs retain existing bounded
  compensation behavior; this change does not reinterpret their contents.
- Covered elsewhere: caller transaction/lease fences and repository row locking
  remain governed by their existing worker, reconciler and repository tests.

## Validation

- `focused-test`: policy law and install/boot controls in
  `tests/domain/test_run_failure_policy.py`; RED includes illegal CREATED source.
- `focused-test`: running and unstarted abandoned-job cases in
  `tests/adversarial/test_reconciler_atomicity.py`; RED unstarted case raises
  `IllegalTransition`; GREEN both preserve their stated transaction outcomes.
- Existing `tests/db/test_repositories.py` illegal-source case must still pass;
  run worker failure tests selected from actual callers as regression proof.
- Use `just test-verbose` for these paths, `just lint`, whole-tree `just type`,
  configured commit hooks; restore a controlled policy fault and observe RED.
- No real ppc64le or live VM claim; changed behavior is exercised through the
  actual reconciler and real PostgreSQL, not an operational host stack.
