# Shared test helpers have non-collected owners

## Problem

The ordinary suite's dependency guard finds nine imports of collected test
modules. Shared helpers consequently import unrelated cases and their imports.
The guard already requires non-collected support modules and stays unchanged.

## Scope

Move `_Fixed` and `_Slow` from diagnostics `test_service.py` into
`tests/diagnostics/checks_support.py`; move `wrap_validation_error` from binding
provenance tests to `tests/mcp/middleware/binding_support.py`. Move teardown,
completed-activation and reprovision seed/query helpers from three reconciler
test files to `tests/reconciler/recovery_support.py`. Update defining tests and
consumers together, retaining aliases where useful. Keep the two teardown
seed functions distinct: their attempt, age and failure-context premises differ.

The sole teardown-replay consumer of the worker test module will instead use
existing `tests.support.worker_fence.register_worker` and construct its Worker
with that returned credential and an explicit SecretRegistry. The original
worker tests and their helpers remain intact. No new registration abstraction.
Production code, assertions, fixture scopes and database cleanup stay unchanged.
Other branch gates and console fixture cleanup retain their existing owners.

### Failure model

- Actors/deployments: repository test runner on supported Linux targets;
  actual local proof uses x86_64 and disposable PostgreSQL.
- Invariants/assets: exact test premises, worker credential binding, cleanup,
  and detection of collected-test dependencies.
- Accepted classes: existing optional/live prerequisites retain their markers;
  the focused ordinary selection must report its actual skips.
- Covered elsewhere: production persistence/auth and final whole-suite proof
  remain with existing guards and campaign completion requirements.

## Success

The unchanged dependency guard passes without exceptions. Moved helper ASTs
match their originals except deliberate names/import aliases. Affected tests
retain their assertions and pass; replay tests still use registered credentials.

## Validation

- `focused-test`: unchanged `tests/test_test_module_dependencies.py` is already
  RED on nine edges; GREEN after migration. Verify moved ASTs mechanically.
- `focused-test`: run all moved-helper defining and importing test files with
  `KDIVE_REQUIRE_DOCKER=1 just test-verbose` and actual PostgreSQL.
- `focused-test`: existing teardown replay tests execute the real Worker claim
  path with the returned credential; no new behavior test mirrors the move.
- Run `just lint`, whole-tree `just type`, hooks and independent code review.
