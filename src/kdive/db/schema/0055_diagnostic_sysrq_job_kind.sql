-- 0055_diagnostic_sysrq_job_kind.sql — diagnostic SysRq capture job kind (#925).
-- Additive to 0051/0052/0053 (forward-only, ADR-0015). Widens the jobs.kind CHECK to admit the
-- `diagnostic_sysrq` job kind (inject one allowlisted magic-SysRq keystroke into a ready
-- local-libvirt guest and capture the console dump, ADR-0285). Drop-and-recreate keeps the
-- constraint name stable for the SQL<->enum tie.
ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check
    CHECK (kind IN ('provision', 'reprovision', 'teardown', 'build', 'install',
                    'boot', 'force_crash', 'power', 'capture_vmcore', 'image_build',
                    'diagnostics_worker_check', 'build_install_boot', 'authorize_ssh_key',
                    'console_rotate', 'diagnostic_sysrq'));
