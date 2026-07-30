# Restore-limbo gets its own failure category (#1560)

Design record for [ADR-0513](../adr/0513-restore-incomplete-failure-category.md), which carries
the decision and the rejected alternatives. This file carries the requirement, the surfaces
touched, and how each claim is verified.

## Requirement

A System that `repair_stalled_restoring_systems` drives to `failed` must report a category that
says its restore was abandoned part-way, and must not report `retryable: true`.

The issue as filed asked for a `LATERAL` join on the `systems.list` query so the list path could
resolve each row's failing job. That premise is dead — ADR-0492 put `failure_category` on the
`systems` row `list_systems` already selects, and the list path reports it with no lookup. The
one failure path still landing on the flattened default is the reconciler's, and it is the one a
job join could never resolve: it leaves no failed job to attribute. See ADR-0513 §Context.

## Acceptance criteria

1. `repair_stalled_restoring_systems` records `restore_incomplete` on a System it resolves whose
   restore job explains nothing — absent, no category, or `lease_expired` — in the transaction
   that already holds the advisory lock and performs the `FAILED` transition.
1a. It records **nothing** when the newest terminal restore job carries a real category, so the
   generic verdict never displaces a specific one (ADR-0513 §1a). The System is still resolved.
2. A System it left alone (an active restore job) has no category written.
3. `systems.list` reports `restore_incomplete` and `retryable: false` for such a System.
4. `systems.get` reports the same after the real `repair_abandoned_jobs` →
   `repair_stalled_restoring_systems` sequence, superseding the job's `lease_expired`.
5. `restore_incomplete` is admitted by all four `ErrorCategory` CHECK constraints.
6. The served errors guide documents the category and its recovery.

## Surfaces

| Surface | Change |
|---|---|
| `domain/errors.py` | `RESTORE_INCOMPLETE` member; `RETRYABLE_BY_CATEGORY[...] = False`. |
| `db/schema/0086_restore_incomplete_category.sql` | Drop + re-add the four named CHECKs (ADR-0513 §3). |
| `db/repositories.py` | `record_system_failure_category` — the column's single writer, moved out of the job handler so both callers share its safety argument (ADR-0513 §4). |
| `jobs/handlers/systems.py` | Calls the shared writer; its private `_record_failure_category` is deleted. |
| `reconciler/repairs/systems.py` | Stamps the category beside `update_state`, gated on `_restore_limbo_category` (ADR-0513 §1a). |
| `mcp/tools/lifecycle/systems/view.py` | `_resolve_failure_verdict` docstring: three NULL-category paths become two. Behaviour unchanged. |
| `docs/guide/errors.md` + packaged snapshot | Category row and recovery pattern. Regenerated with `just resources-docs`. |

No tool schema, argument, RBAC rule or exposure entry changes. `restore_incomplete` is
nevertheless an agent-visible wire string; ADR-0513 §Consequences states the compatibility effect.

## Verification

| Criterion | Test |
|---|---|
| 1 | `tests/reconciler/test_snapshot_repairs.py::test_recovers_restoring_with_no_active_restore_job`, `::test_recovers_multiple_restoring_systems_all_counted`, `::test_stamps_over_a_lease_expired_restore_job`, `::test_stamps_when_no_restore_job_row_survives` |
| 1a | `tests/reconciler/test_snapshot_repairs.py::test_does_not_overwrite_a_restore_jobs_own_category` |
| 2 | `tests/reconciler/test_snapshot_repairs.py::test_leaves_restoring_with_active_restore_job` |
| 3 | `tests/mcp/lifecycle/test_systems_failure_category.py::test_systems_list_reports_restore_incomplete_after_the_reconciler_sweep` |
| 4 | `tests/mcp/lifecycle/test_systems_failure_category.py::test_get_system_after_the_real_abandoned_job_sweep` (re-baselined) |
| 5 | `tests/db/test_migrate.py::test_check_constraint_covers_every_enum_value` (parametrized over all four constraints) |
| retryable | `tests/mcp/core/test_responses.py::test_restore_incomplete_is_not_retryable`, `::test_every_category_has_an_explicit_expected_bool` |
| taxonomy | `tests/domain/test_errors.py::test_taxonomy_is_exactly_the_m0_set` |
| 6 | `tests/mcp/resources/test_doc_resources.py::test_packaged_snapshot_matches_canonical_source` |

Mutation-verified in both directions: deleting the stamp fails the criterion-1, -3 and -4 tests,
and disabling the `_restore_limbo_category` guard fails the criterion-1a test. Both pass again on
the restored tree.

## Not done

- No backfill. ADR-0513 §Consequences and §Considered & rejected: the repair's evidence is the
  *absence* of an active job, so a migration could only guess, and would relabel restores that
  failed inside their handler for a real provider reason.
- No `LATERAL` join, no partial index on `jobs`. Moot, per §Requirement.
- Migration 0085 is skipped — reserved by another in-flight change in this campaign. The runner
  sorts by version string and requires no contiguity; the schema already has a 0073 gap.
