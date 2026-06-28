-- 0051_build_install_boot_job_kind.sql — composite build->install->boot job (ADR-0268, #866).
-- Additive to 0003/0024/0040 (forward-only, ADR-0015). Widens the jobs.kind CHECK to admit the
-- `build_install_boot` composite op (runs.build_install_boot enqueues one job whose handler runs
-- the three phases). Drop-and-recreate keeps the constraint name stable for the SQL<->enum tie.
ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check
    CHECK (kind IN ('provision', 'reprovision', 'teardown', 'build', 'install',
                    'boot', 'force_crash', 'power', 'capture_vmcore', 'image_build',
                    'diagnostics_worker_check', 'build_install_boot'));
