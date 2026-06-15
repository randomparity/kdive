-- 0033_allocation_failure_category.sql — record the terminal cause of a failed
-- allocation so a waiting agent (allocations.wait, #430 / ADR-0118) can tell a
-- queue_timeout (retryable) from a budget terminate (allocation_denied, terminal).
-- Additive, forward-only (ADR-0015). NULL for every existing failed row and for any
-- failed path that does not yet set it; the response envelope falls back to
-- infrastructure_failure when NULL.
--
-- The named CHECK mirrors ErrorCategory (like runs_failure_category_check /
-- jobs_error_category_check), so an out-of-enum string can never reach the column and
-- break the Allocation.failure_category coercion on read. Registered in
-- test_migrate.py CHECK_ENUMS, which fails if it drifts from the enum.
ALTER TABLE allocations
    ADD COLUMN failure_category text
        CONSTRAINT allocations_failure_category_check
        CHECK (failure_category IN (
            'configuration_error', 'missing_dependency',
            'build_failure', 'boot_timeout', 'readiness_failure',
            'debug_attach_failure', 'infrastructure_failure',
            'stale_handle', 'transport_conflict', 'not_implemented',
            'not_found', 'conflict',
            'allocation_denied', 'quota_exceeded', 'lease_expired', 'queue_timeout',
            'provisioning_failure', 'install_failure',
            'transport_failure', 'control_failure',
            'authorization_denied', 'capacity_exhausted'));
