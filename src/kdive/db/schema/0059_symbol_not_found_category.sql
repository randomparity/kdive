-- 0059_symbol_not_found_category.sql — resolver symbol-miss failure category (#1013, ADR-0307).
-- Additive to 0058 (forward-only, ADR-0015). Widens the runs.failure_category,
-- jobs.error_category and allocations.failure_category CHECKs to admit `symbol_not_found`, the new
-- ErrorCategory value `debug.resolve_symbol` returns for an inlined / optimized-away symbol or an
-- addressless enum/macro constant. It is raised synchronously at the debug-op boundary (so it is
-- not currently persisted on a Run/Job/Allocation), but the SQL↔enum tie (tested in test_migrate.py
-- CHECK_ENUMS) requires every ErrorCategory value be admitted by these constraints.
-- Drop-and-recreate keeps the constraint names stable. Mirrors ErrorCategory in domain/errors.py.
ALTER TABLE runs DROP CONSTRAINT runs_failure_category_check;
ALTER TABLE runs ADD CONSTRAINT runs_failure_category_check
    CHECK (failure_category IN (
        'configuration_error', 'missing_dependency',
        'build_failure', 'boot_timeout', 'readiness_failure',
        'debug_attach_failure', 'symbol_not_found', 'infrastructure_failure',
        'stale_handle', 'transport_conflict', 'not_implemented',
        'not_found', 'conflict',
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
        'not_found', 'conflict',
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
        'not_found', 'conflict',
        'allocation_denied', 'quota_exceeded', 'lease_expired', 'queue_timeout',
        'provisioning_failure', 'install_failure',
        'transport_failure', 'control_failure',
        'authorization_denied', 'capacity_exhausted'));
