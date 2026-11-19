-- 0200_system_devices.sql — hot-plugged device child ledger + plug/unplug job kinds (#3697, ADR-0823).
-- Forward-only (ADR-0015), additive. Two changes:
--  1. A `system_devices` child ledger (one row per hot-plugged NIC or scratch disk of a System).
--     `ON DELETE CASCADE` keeps a device from outliving its System. The per-System device limit is
--     enforced at admission, not here. `kind` is text; the provider port owns the typed enum.
--  2. Widen jobs.kind to admit the `plug_device` and `unplug_device` job kinds.
-- No role grants: runtime authority arrives with the handlers that use the table (ADR-0533).

CREATE TABLE system_devices (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    system_id       uuid NOT NULL REFERENCES systems (id) ON DELETE CASCADE,
    kind            text NOT NULL CONSTRAINT system_devices_kind_check
                        CHECK (kind IN ('nic', 'disk')),
    state           text NOT NULL CONSTRAINT system_devices_state_check
                        CHECK (state IN ('plugging', 'attached', 'unplugging', 'failed', 'lost')),
    network         text,
    size_gb         integer,
    provider_handle text,
    identity        jsonb NOT NULL DEFAULT '{}',
    detail          text,
    settle_deadline timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX system_devices_system_id_idx ON system_devices (system_id);
CREATE TRIGGER system_devices_set_updated_at BEFORE UPDATE ON system_devices
    FOR EACH ROW WHEN (OLD.* IS DISTINCT FROM NEW.*) EXECUTE FUNCTION set_updated_at();

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
                    'observe_stranded_activation', 'plug_device', 'unplug_device'));
