-- 0053_console_rotate_job_kind.sql — internal console-rotation job kind (#892).
-- Additive to 0051/0052 (forward-only, ADR-0015). Widens the jobs.kind CHECK to admit the
-- internal `console_rotate` job kind (rotating per-System console-part artifacts after a
-- ready system's console log exceeds its size threshold). Drop-and-recreate keeps the
-- constraint name stable for the SQL<->enum tie.
ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check
    CHECK (kind IN ('provision', 'reprovision', 'teardown', 'build', 'install',
                    'boot', 'force_crash', 'power', 'capture_vmcore', 'image_build',
                    'diagnostics_worker_check', 'build_install_boot', 'authorize_ssh_key',
                    'console_rotate'));
