-- 0004_quota_exceeded_category.sql — M1 admission-gate failure category (ADR-0007 §4).
-- Additive to 0003 (forward-only, ADR-0015). Widens the runs.failure_category and
-- jobs.error_category CHECKs to admit `quota_exceeded`, the per-project concurrency-cap
-- denial first emitted by the budget/quota admission gate (allocations.request,
-- systems.provision). Mirrors ErrorCategory in domain/errors.py. Drop-and-recreate keeps
-- the constraint names stable for the SQL↔enum tie (tested in test_migrate.py).
ALTER TABLE runs DROP CONSTRAINT runs_failure_category_check;
ALTER TABLE runs ADD CONSTRAINT runs_failure_category_check
    CHECK (failure_category IN (
        'configuration_error', 'missing_dependency',
        'build_failure', 'boot_timeout', 'readiness_failure',
        'debug_attach_failure', 'infrastructure_failure',
        'stale_handle', 'transport_conflict', 'not_implemented',
        'allocation_denied', 'quota_exceeded', 'lease_expired',
        'provisioning_failure', 'install_failure',
        'transport_failure', 'control_failure',
        'authorization_denied'));

ALTER TABLE jobs DROP CONSTRAINT jobs_error_category_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_error_category_check
    CHECK (error_category IN (
        'configuration_error', 'missing_dependency',
        'build_failure', 'boot_timeout', 'readiness_failure',
        'debug_attach_failure', 'infrastructure_failure',
        'stale_handle', 'transport_conflict', 'not_implemented',
        'allocation_denied', 'quota_exceeded', 'lease_expired',
        'provisioning_failure', 'install_failure',
        'transport_failure', 'control_failure',
        'authorization_denied'));
