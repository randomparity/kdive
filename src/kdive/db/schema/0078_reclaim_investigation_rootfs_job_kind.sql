-- 0078_reclaim_investigation_rootfs_job_kind.sql — investigation-rootfs reclaim job kind
-- (#1522, ADR-0442). Forward-only (ADR-0015), additive. Widens jobs.kind to admit the internal
-- `reclaim_investigation_rootfs` kind: the reconciler's two rootfs sweeps become DB-only worklist
-- scans that enqueue one job per investigation, and the worker — which created the staging tree
-- and may be running as a different user than the reconciler — performs the staged-base unlink,
-- object delete, and `artifacts` row delete. Drop-and-recreate keeps the constraint name stable
-- for the SQL<->enum tie (test_migrate.py).
ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check
    CHECK (kind IN ('provision', 'reprovision', 'teardown', 'build', 'install',
                    'boot', 'force_crash', 'power', 'capture_vmcore', 'image_build',
                    'diagnostics_worker_check', 'build_install_boot', 'authorize_ssh_key',
                    'console_rotate', 'diagnostic_sysrq', 'check_ssh_reachable',
                    'watch_for_crash', 'snapshot', 'restore', 'delete_snapshot',
                    'capture_traffic', 'reclaim_investigation_rootfs'));
