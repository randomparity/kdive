-- 0083_systems_failure_category.sql — give a failed System its own durable home for
-- the reason it failed (ADR-0492, #1562).
--
-- Before this column the category had exactly one durable home, jobs.error_category,
-- written by queue.fail on a *different* connection from the one the handler used to
-- commit systems.state = 'failed'. Nothing spanned the two, so a worker that died — or a
-- queue.fail that raised — in that window left the category permanently unrecoverable:
-- the job either got 'lease_expired' stamped over it by repair_abandoned_jobs, or was
-- re-dispatched, re-entered a terminal System, and ended 'succeeded'.
--
-- _record_system_failure now writes this column inside the same transaction as the
-- systems.state transition, so the System's reason lands atomically with its failure.
--
-- Additive, forward-only (ADR-0015). NULL for every existing failed row, for the
-- reconciler's job-less repair_stalled_restoring_systems path, and for any System that
-- fails without a categorized error; the envelope then falls back to the ADR-0454
-- failing-job lookup and finally to infrastructure_failure, exactly as today.
--
-- The named CHECK mirrors ErrorCategory (like runs_failure_category_check /
-- allocations_failure_category_check / jobs_error_category_check), so an out-of-enum
-- string can never reach the column and break the System.failure_category coercion on
-- read. Registered in test_migrate.py CHECK_ENUMS, which fails if it drifts from the enum.
ALTER TABLE systems
    ADD COLUMN failure_category text
        CONSTRAINT systems_failure_category_check
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
