-- Bounded recovery-object quarantine and administrative disposition (#2204).

ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check
    CHECK (kind IN ('provision', 'reprovision', 'teardown', 'build', 'install',
                    'boot', 'force_crash', 'power', 'capture_vmcore', 'image_build',
                    'diagnostics_worker_check', 'build_install_boot', 'authorize_ssh_key',
                    'console_rotate', 'diagnostic_sysrq', 'check_ssh_reachable',
                    'watch_for_crash', 'snapshot', 'restore', 'delete_snapshot',
                    'capture_traffic', 'reclaim_investigation_rootfs',
                    'remote_module_volume_reap', 'resolve_recovery_orphan'));

CREATE TABLE external_boot_recovery_quarantine (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    object_identity     text NOT NULL UNIQUE,
    resource_id         uuid NOT NULL REFERENCES resources (id) ON DELETE RESTRICT,
    system_id           uuid NOT NULL REFERENCES systems (id) ON DELETE RESTRICT,
    activation_id       uuid NOT NULL REFERENCES external_boot_activations (id)
                            ON DELETE RESTRICT,
    provider_kind       text NOT NULL,
    authority_instance  text NOT NULL,
    object_kind         text NOT NULL,
    object_reference    text NOT NULL,
    ownership_digest    text NOT NULL,
    observed_digest     text NOT NULL,
    reserved_bytes      bigint NOT NULL,
    status              text NOT NULL DEFAULT 'quarantined',
    disposition_job_id  uuid REFERENCES jobs (id) ON DELETE RESTRICT,
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    resolved_at         timestamptz,
    CONSTRAINT external_boot_recovery_quarantine_identity
        CHECK (octet_length(object_identity) BETWEEN 1 AND 1024),
    CONSTRAINT external_boot_recovery_quarantine_provider
        CHECK (provider_kind IN ('local-libvirt', 'remote-libvirt')),
    CONSTRAINT external_boot_recovery_quarantine_authority
        CHECK (octet_length(authority_instance) BETWEEN 1 AND 255),
    CONSTRAINT external_boot_recovery_quarantine_kind
        CHECK (object_kind IN ('kernel', 'initrd', 'modules', 'recovery-record')),
    CONSTRAINT external_boot_recovery_quarantine_reference
        CHECK (octet_length(object_reference) BETWEEN 1 AND 1024),
    CONSTRAINT external_boot_recovery_quarantine_ownership_digest
        CHECK (ownership_digest ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT external_boot_recovery_quarantine_observed_digest
        CHECK (observed_digest ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT external_boot_recovery_quarantine_reserved_bytes CHECK (reserved_bytes >= 0),
    CONSTRAINT external_boot_recovery_quarantine_status
        CHECK (status IN ('quarantined', 'adopted', 'deleted')),
    CONSTRAINT external_boot_recovery_quarantine_resolution CHECK (
        (status = 'quarantined' AND resolved_at IS NULL)
        OR (status IN ('adopted', 'deleted') AND resolved_at IS NOT NULL)
    )
);

CREATE INDEX external_boot_recovery_quarantine_system_status_idx
    ON external_boot_recovery_quarantine (system_id, status, id);

CREATE TABLE external_boot_recovery_orphan_requests (
    id              uuid PRIMARY KEY,
    system_id       uuid NOT NULL REFERENCES systems (id) ON DELETE RESTRICT,
    disposition     text NOT NULL CHECK (disposition IN ('delete', 'adopt')),
    binding_digest  text NOT NULL CHECK (binding_digest ~ '^sha256:[0-9a-f]{64}$'),
    object_ids      uuid[] NOT NULL CHECK (
        cardinality(object_ids) BETWEEN 1 AND 64
        AND array_position(object_ids, NULL) IS NULL
    ),
    job_id          uuid NOT NULL UNIQUE REFERENCES jobs (id) ON DELETE RESTRICT,
    created_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at    timestamptz
);

REVOKE ALL ON external_boot_recovery_quarantine FROM PUBLIC;
REVOKE ALL ON external_boot_recovery_orphan_requests FROM PUBLIC;
GRANT SELECT ON external_boot_recovery_quarantine TO kdive_server;
GRANT SELECT, INSERT, UPDATE ON external_boot_recovery_quarantine TO kdive_worker;
GRANT SELECT, INSERT ON external_boot_recovery_orphan_requests TO kdive_server;
GRANT SELECT, UPDATE ON external_boot_recovery_orphan_requests TO kdive_worker;
