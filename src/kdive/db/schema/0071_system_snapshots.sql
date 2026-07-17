-- 0071_system_snapshots.sql — System snapshot child ledger + restore/pause states (#1254, ADR-0378).
-- Forward-only (ADR-0015), additive. Three changes:
--  1. A `snapshots` child ledger (one row per named checkpoint of a System). Postgres is the
--     index-of-record for list/audit/teardown; libvirt holds the RAM+disk data inside the qcow2.
--     `ON DELETE CASCADE` enforces the durable-objects invariant (a snapshot never outlives its
--     System); `UNIQUE (system_id, name)` makes a name a durable checkpoint identity.
--  2. Widen jobs.kind to admit the async snapshot/restore/delete_snapshot job kinds.
--  3. Widen systems.state to admit `restoring` (the revert fence) and `paused` (a start_paused
--     restore's suspended-guest resting state). No existing row carries the new states/kinds.

CREATE TABLE snapshots (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    system_id      uuid NOT NULL REFERENCES systems (id) ON DELETE CASCADE,
    name           text NOT NULL,
    include_memory boolean NOT NULL,
    state          text NOT NULL CONSTRAINT snapshots_state_check
                       CHECK (state IN ('creating', 'available', 'failed')),
    principal      text NOT NULL,
    agent_session  text,
    project        text NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT snapshots_system_id_name_key UNIQUE (system_id, name)
);
CREATE TRIGGER snapshots_set_updated_at BEFORE UPDATE ON snapshots
    FOR EACH ROW WHEN (OLD.* IS DISTINCT FROM NEW.*) EXECUTE FUNCTION set_updated_at();

-- Drop-and-recreate keeps the constraint name stable for the SQL<->enum tie (test_migrate.py).
ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check
    CHECK (kind IN ('provision', 'reprovision', 'teardown', 'build', 'install',
                    'boot', 'force_crash', 'power', 'capture_vmcore', 'image_build',
                    'diagnostics_worker_check', 'build_install_boot', 'authorize_ssh_key',
                    'console_rotate', 'diagnostic_sysrq', 'check_ssh_reachable',
                    'watch_for_crash', 'snapshot', 'restore', 'delete_snapshot'));

ALTER TABLE systems DROP CONSTRAINT systems_state_check;
ALTER TABLE systems ADD CONSTRAINT systems_state_check
    CHECK (state IN ('defined', 'provisioning', 'ready', 'reprovisioning',
                     'restoring', 'paused', 'crashing', 'crashed', 'torn_down', 'failed'));
