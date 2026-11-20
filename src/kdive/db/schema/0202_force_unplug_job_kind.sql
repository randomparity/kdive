-- 0202_force_unplug_job_kind.sql — admit the `force_unplug_device` job kind (#3761, ADR-0828).
-- Forward-only (ADR-0015), additive. Widens jobs.kind; no table, column or grant changes.

-- Drop-and-recreate keeps the constraint name stable for the SQL<->enum tie (test_migrate.py).
ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check
    CHECK (kind IN ('provision', 'reprovision', 'teardown', 'build', 'install',
                    'boot', 'force_crash', 'power', 'capture_vmcore', 'image_build',
                    'diagnostics_worker_check', 'build_install_boot', 'authorize_ssh_key',
                    'console_rotate', 'diagnostic_sysrq', 'check_ssh_reachable',
                    'watch_for_crash', 'snapshot', 'restore', 'delete_snapshot',
                    'capture_traffic', 'reclaim_investigation_rootfs',
                    'remote_module_volume_reap', 'resolve_recovery_orphan',
                    'observe_stranded_activation', 'plug_device', 'unplug_device',
                    'force_unplug_device'));
