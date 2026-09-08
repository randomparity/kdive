# Historical verification — Restore-limbo gets its own failure category (#1560)

Recorded 2026-07-30 for the then-current checkout. These are historical results; they do not
establish a passing result or supported procedure for the current release.

The verification covered the reconciler's `restore_incomplete` stamp, its visibility through
System list/get, and preservation of a failed restore job's own category. Removing the stamp
failed the stamp/list/get tests. Disabling `_restore_limbo_category` failed
`test_does_not_overwrite_a_restore_jobs_own_category`. Both directions passed again after
restoring the implementation.

Decision and executable owners:

- [0492-system-records-its-own-failure-category.md](../adr/0492-system-records-its-own-failure-category.md)
- [0513-restore-incomplete-failure-category.md](../adr/0513-restore-incomplete-failure-category.md)
- [test_snapshot_repairs.py](../../tests/reconciler/test_snapshot_repairs.py)
- [test_systems_failure_category.py](../../tests/mcp/lifecycle/test_systems_failure_category.py)
