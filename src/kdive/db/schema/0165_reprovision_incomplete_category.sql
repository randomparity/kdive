-- 0165_reprovision_incomplete_category.sql — reprovision limbo failure category (#2980, ADR-0435).
-- Additive to 0164 (forward-only, ADR-0015). Widens all four ErrorCategory CHECKs —
-- runs.failure_category, jobs.error_category, allocations.failure_category and
-- systems.failure_category — to admit `reprovision_incomplete`, the category
-- repair_stalled_reprovisioning_systems stamps when it drives a stalled `reprovisioning` System
-- to `failed`.
--
-- Only `systems` can receive the value: the reconciler writes it straight onto the systems row
-- and never raises it as a CategorizedError. All four widen anyway because test_migrate.py
-- CHECK_ENUMS ties each constraint to the whole ErrorCategory enum, as 0086 did for
-- `restore_incomplete`. Drop-and-recreate keeps the constraint names stable. Mirrors
-- ErrorCategory in domain/errors.py.
ALTER TABLE runs DROP CONSTRAINT runs_failure_category_check;
ALTER TABLE runs ADD CONSTRAINT runs_failure_category_check
    CHECK (failure_category IN (
        'configuration_error', 'missing_dependency',
        'build_failure', 'boot_timeout', 'readiness_failure',
        'debug_attach_failure', 'symbol_not_found', 'infrastructure_failure',
        'stale_handle', 'transport_conflict', 'not_implemented',
        'not_found', 'conflict', 'restore_incomplete', 'reprovision_incomplete',
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
        'not_found', 'conflict', 'restore_incomplete', 'reprovision_incomplete',
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
        'not_found', 'conflict', 'restore_incomplete', 'reprovision_incomplete',
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
        'not_found', 'conflict', 'restore_incomplete', 'reprovision_incomplete',
        'allocation_denied', 'quota_exceeded', 'lease_expired', 'queue_timeout',
        'provisioning_failure', 'install_failure',
        'transport_failure', 'control_failure',
        'authorization_denied', 'capacity_exhausted'));
