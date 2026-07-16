-- 0069_watch_for_crash_job_kind.sql — out-of-band crash-signature console watch job kind (#984).
-- Additive to 0057 (forward-only, ADR-0015). Widens the jobs.kind CHECK to admit the
-- `watch_for_crash` job kind (control.watch_for_crash enqueues one job whose handler polls a ready
-- local-libvirt System's serial console for the boot-readiness crash matcher until a clamped
-- wall-clock deadline, returning the first hit — the reproducer loop stays the agent's own root
-- SSH, ADR-0367). Drop-and-recreate keeps the constraint name stable for the SQL<->enum tie.
ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check
    CHECK (kind IN ('provision', 'reprovision', 'teardown', 'build', 'install',
                    'boot', 'force_crash', 'power', 'capture_vmcore', 'image_build',
                    'diagnostics_worker_check', 'build_install_boot', 'authorize_ssh_key',
                    'console_rotate', 'diagnostic_sysrq', 'check_ssh_reachable',
                    'watch_for_crash'));
