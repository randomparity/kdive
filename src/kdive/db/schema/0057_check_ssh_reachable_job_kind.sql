-- 0057_check_ssh_reachable_job_kind.sql — SSH-reachability probe job kind (#972).
-- Additive to 0052/0055 (forward-only, ADR-0015). Widens the jobs.kind CHECK to admit the
-- `check_ssh_reachable` job kind (systems.check_ssh_reachable enqueues one job whose handler opens
-- a bounded, retried TCP connect to the recorded loopback endpoint and reads the SSH banner to
-- report a per-System reachability verdict, ADR-0298). Drop-and-recreate keeps the constraint name
-- stable for the SQL<->enum tie.
ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check
    CHECK (kind IN ('provision', 'reprovision', 'teardown', 'build', 'install',
                    'boot', 'force_crash', 'power', 'capture_vmcore', 'image_build',
                    'diagnostics_worker_check', 'build_install_boot', 'authorize_ssh_key',
                    'console_rotate', 'diagnostic_sysrq', 'check_ssh_reachable'));
