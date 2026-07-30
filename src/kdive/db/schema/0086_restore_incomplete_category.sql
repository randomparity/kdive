-- 0086_restore_incomplete_category.sql — snapshot-restore limbo failure category (#1560, ADR-0513).
-- Additive to 0085 (forward-only, ADR-0015). Widens all four ErrorCategory CHECKs —
-- runs.failure_category, jobs.error_category, allocations.failure_category and
-- systems.failure_category — to admit `restore_incomplete`, the category
-- repair_stalled_restoring_systems now stamps when it drives a stalled `restoring` System to
-- `failed`.
--
-- Only `systems` can receive the value today: it is written by the reconciler straight onto the
-- systems row, never raised as a CategorizedError, so no job/run/allocation failure path can
-- carry it. All four widen anyway because test_migrate.py CHECK_ENUMS ties each of these
-- constraints to the whole ErrorCategory enum, not to the subset its table can observe — the
-- same reason 0059 widened three constraints for a value only the debug op could produce.
-- Drop-and-recreate keeps the constraint names stable. Mirrors ErrorCategory in domain/errors.py.
ALTER TABLE runs DROP CONSTRAINT runs_failure_category_check;
ALTER TABLE runs ADD CONSTRAINT runs_failure_category_check
    CHECK (failure_category IN (
        'configuration_error', 'missing_dependency',
        'build_failure', 'boot_timeout', 'readiness_failure',
        'debug_attach_failure', 'symbol_not_found', 'infrastructure_failure',
        'stale_handle', 'transport_conflict', 'not_implemented',
        'not_found', 'conflict', 'restore_incomplete',
        'allocation_denied', 'quota_exceeded', 'lease_expired', 'queue_timeout',
        'provisioning_failure', 'install_failure',
        'transport_failure', 'control_failure',
        'authorization_denied', 'capacity_exhausted'));

ALTER TABLE jobs DROP CONSTRAINT jobs_error_category_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_error_category_check
    CHECK (error_category IN (
        'configuration_error', 'missing_dependency',
        'build_failure', 'boot_timeout', 'readiness_failure',
        'debug_attach_failure', 'symbol_not_found', 'infrastructure_failure',
        'stale_handle', 'transport_conflict', 'not_implemented',
        'not_found', 'conflict', 'restore_incomplete',
        'allocation_denied', 'quota_exceeded', 'lease_expired', 'queue_timeout',
        'provisioning_failure', 'install_failure',
        'transport_failure', 'control_failure',
        'authorization_denied', 'capacity_exhausted'));

ALTER TABLE allocations DROP CONSTRAINT allocations_failure_category_check;
ALTER TABLE allocations ADD CONSTRAINT allocations_failure_category_check
    CHECK (failure_category IN (
        'configuration_error', 'missing_dependency',
        'build_failure', 'boot_timeout', 'readiness_failure',
        'debug_attach_failure', 'symbol_not_found', 'infrastructure_failure',
        'stale_handle', 'transport_conflict', 'not_implemented',
        'not_found', 'conflict', 'restore_incomplete',
        'allocation_denied', 'quota_exceeded', 'lease_expired', 'queue_timeout',
        'provisioning_failure', 'install_failure',
        'transport_failure', 'control_failure',
        'authorization_denied', 'capacity_exhausted'));

ALTER TABLE systems DROP CONSTRAINT systems_failure_category_check;
ALTER TABLE systems ADD CONSTRAINT systems_failure_category_check
    CHECK (failure_category IN (
        'configuration_error', 'missing_dependency',
        'build_failure', 'boot_timeout', 'readiness_failure',
        'debug_attach_failure', 'symbol_not_found', 'infrastructure_failure',
        'stale_handle', 'transport_conflict', 'not_implemented',
        'not_found', 'conflict', 'restore_incomplete',
        'allocation_denied', 'quota_exceeded', 'lease_expired', 'queue_timeout',
        'provisioning_failure', 'install_failure',
        'transport_failure', 'control_failure',
        'authorization_denied', 'capacity_exhausted'));
