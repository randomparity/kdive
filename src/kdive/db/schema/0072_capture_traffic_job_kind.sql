-- 0072_capture_traffic_job_kind.sql — host-side network traffic capture job kind (#1258, ADR-0384).
-- Forward-only (ADR-0015), additive. Widens jobs.kind to admit the `capture_traffic` job kind:
-- control.capture_traffic enqueues one job whose handler runs a QEMU filter-dump on a ready
-- local-libvirt guest's netdev for a bounded window, storing a Run-owned SENSITIVE pcap.
-- Drop-and-recreate keeps the constraint name stable for the SQL<->enum tie (test_migrate.py).
ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check
    CHECK (kind IN ('provision', 'reprovision', 'teardown', 'build', 'install',
                    'boot', 'force_crash', 'power', 'capture_vmcore', 'image_build',
                    'diagnostics_worker_check', 'build_install_boot', 'authorize_ssh_key',
                    'console_rotate', 'diagnostic_sysrq', 'check_ssh_reachable',
                    'watch_for_crash', 'snapshot', 'restore', 'delete_snapshot',
                    'capture_traffic'));
