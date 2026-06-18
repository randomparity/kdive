-- 0040_diagnostics_worker_check_job_kind.sql — diagnostics worker-vantage dispatch (ADR-0164, #514).
-- Additive to 0003/0024 (forward-only, ADR-0015). Widens the jobs.kind CHECK to admit the
-- `diagnostics_worker_check` op (ops.diagnostics enqueues it to run provider_tls/gdbstub_acl on the
-- worker); mirrors JobKind in domain/models.py. Drop-and-recreate keeps the constraint name stable
-- for the SQL<->enum tie (tested in test_migrate.py).
ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check
    CHECK (kind IN ('provision', 'reprovision', 'teardown', 'build', 'install',
                    'boot', 'force_crash', 'power', 'capture_vmcore', 'image_build',
                    'diagnostics_worker_check'));
