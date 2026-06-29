-- 0052_authorize_ssh_key_job_kind.sql — direct-SSH key authorization job (ADR-0271, #782).
-- Additive to 0003/0024/0040/0051 (forward-only, ADR-0015). Widens the jobs.kind CHECK to admit
-- the `authorize_ssh_key` op (systems.authorize_ssh_key enqueues one job whose handler appends the
-- agent public key to the guest root authorized_keys). Drop-and-recreate keeps the constraint name
-- stable for the SQL<->enum tie.
ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check
    CHECK (kind IN ('provision', 'reprovision', 'teardown', 'build', 'install',
                    'boot', 'force_crash', 'power', 'capture_vmcore', 'image_build',
                    'diagnostics_worker_check', 'build_install_boot', 'authorize_ssh_key'));
