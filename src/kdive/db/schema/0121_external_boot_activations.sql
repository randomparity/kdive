-- 0121_external_boot_activations.sql — durable external-boot activation truth (ADR-0583/0584).

CREATE UNIQUE INDEX runs_id_system_id_key ON runs (id, system_id);

CREATE TABLE external_boot_activations (
    id                            uuid PRIMARY KEY,
    system_id                     uuid NOT NULL REFERENCES systems (id) ON DELETE CASCADE,
    run_id                        uuid NOT NULL,
    plan_identity                 text NOT NULL CONSTRAINT external_boot_activation_plan_digest
                                      CHECK (plan_identity ~ '^sha256:[0-9a-f]{64}$'),
    operation_owner_id            uuid NOT NULL,
    authority_generation          bigint NOT NULL CONSTRAINT external_boot_activation_generation
                                      CHECK (authority_generation > 0),
    state                         text NOT NULL CONSTRAINT external_boot_activation_state
                                      CHECK (state IN ('preparing', 'prepared', 'activating',
                                                       'active', 'recovering', 'recovered',
                                                       'recovery_conflict', 'recovery_failed',
                                                       'abandoned')),
    cleanup_complete              boolean NOT NULL DEFAULT false,
    activation_readiness_deadline timestamptz,
    materialization               jsonb,
    recovery_point                jsonb,
    pre_recovery_evidence         jsonb,
    terminal_evidence             jsonb,
    teardown_evidence             jsonb,
    cleanup_evidence              jsonb,
    current_attempt_id            uuid,
    created_at                    timestamptz NOT NULL DEFAULT now(),
    updated_at                    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT external_boot_activation_run_system_fk
        FOREIGN KEY (run_id, system_id) REFERENCES runs (id, system_id) ON DELETE CASCADE,
    CONSTRAINT external_boot_activation_cleanup_state
        CHECK (NOT cleanup_complete OR state IN ('recovered', 'abandoned',
                                                  'recovery_conflict', 'recovery_failed')),
    CONSTRAINT external_boot_activation_deadline
        CHECK (state NOT IN ('activating', 'active')
               OR activation_readiness_deadline IS NOT NULL),
    CONSTRAINT external_boot_activation_state_evidence CHECK (
        state = 'preparing'
        OR (state = 'prepared'
            AND materialization IS NOT NULL AND recovery_point IS NOT NULL)
        OR (state = 'activating'
            AND materialization IS NOT NULL AND recovery_point IS NOT NULL)
        OR (state = 'active'
            AND materialization IS NOT NULL AND recovery_point IS NOT NULL
            AND terminal_evidence ->> 'outcome' IS NOT DISTINCT FROM 'active')
        OR (state IN ('recovering', 'recovery_conflict', 'recovery_failed', 'recovered')
            AND materialization IS NOT NULL AND current_attempt_id IS NOT NULL
            AND (recovery_point IS NOT NULL OR pre_recovery_evidence IS NOT NULL))
        OR (state = 'abandoned'
            AND terminal_evidence ->> 'outcome' IS NOT DISTINCT FROM 'abandoned')
    ),
    CONSTRAINT external_boot_activation_evidence_schema CHECK (
        (materialization IS NULL OR materialization ->> 'schema'
            IS NOT DISTINCT FROM 'external-boot-materialization-v1')
        AND (recovery_point IS NULL OR recovery_point ->> 'schema'
            IS NOT DISTINCT FROM 'external-boot-recovery-v1')
        AND (pre_recovery_evidence IS NULL OR pre_recovery_evidence ->> 'schema'
            IS NOT DISTINCT FROM 'external-boot-pre-recovery-evidence-v1')
        AND (terminal_evidence IS NULL OR terminal_evidence ->> 'schema'
            IS NOT DISTINCT FROM 'external-boot-terminal-evidence-v1')
        AND (teardown_evidence IS NULL OR teardown_evidence ->> 'schema'
            IS NOT DISTINCT FROM 'external-boot-teardown-evidence-v1')
        AND (cleanup_evidence IS NULL OR cleanup_evidence ->> 'schema'
            IS NOT DISTINCT FROM 'external-boot-cleanup-evidence-v1')
    ),
    CONSTRAINT external_boot_activation_cleanup_evidence CHECK (
        NOT cleanup_complete
        OR (
            cleanup_evidence IS NOT NULL
            AND (
                (state IN ('recovered', 'abandoned')
                 AND cleanup_evidence ->> 'mode' IS NOT DISTINCT FROM 'ordinary'
                 AND teardown_evidence IS NULL)
                OR
                (state IN ('recovery_conflict', 'recovery_failed')
                 AND cleanup_evidence ->> 'mode' IS NOT DISTINCT FROM 'system_teardown'
                 AND teardown_evidence IS NOT NULL)
            )
        )
    ),
    CONSTRAINT external_boot_activation_evidence_size CHECK (
        (materialization IS NULL OR pg_column_size(materialization) <= 65536)
        AND (recovery_point IS NULL OR pg_column_size(recovery_point) <= 65536)
        AND (pre_recovery_evidence IS NULL OR pg_column_size(pre_recovery_evidence) <= 65536)
        AND (terminal_evidence IS NULL OR pg_column_size(terminal_evidence) <= 65536)
        AND (teardown_evidence IS NULL OR pg_column_size(teardown_evidence) <= 65536)
        AND (cleanup_evidence IS NULL OR pg_column_size(cleanup_evidence) <= 65536)
    ),
    CONSTRAINT external_boot_activations_retry_key
        UNIQUE (system_id, run_id, plan_identity)
);

CREATE UNIQUE INDEX external_boot_activations_one_live_per_system
    ON external_boot_activations (system_id)
    WHERE state NOT IN ('recovered', 'abandoned') OR NOT cleanup_complete;

CREATE TRIGGER external_boot_activations_set_updated_at
    BEFORE UPDATE ON external_boot_activations
    FOR EACH ROW WHEN (OLD.* IS DISTINCT FROM NEW.*) EXECUTE FUNCTION set_updated_at();

CREATE TABLE external_boot_reservations (
    activation_id uuid PRIMARY KEY REFERENCES external_boot_activations (id) ON DELETE CASCADE,
    store_identity text NOT NULL,
    owner_key       text NOT NULL,
    reserved_bytes  bigint NOT NULL CONSTRAINT external_boot_reservation_bytes
                        CHECK (reserved_bytes > 0),
    state           text NOT NULL CONSTRAINT external_boot_reservation_state
                        CHECK (state IN ('pending', 'ready')),
    ready_at        timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT external_boot_reservation_ready_at
        CHECK ((state = 'ready') = (ready_at IS NOT NULL)),
    CONSTRAINT external_boot_reservation_owner_key UNIQUE (store_identity, owner_key)
);

CREATE TRIGGER external_boot_reservations_set_updated_at
    BEFORE UPDATE ON external_boot_reservations
    FOR EACH ROW WHEN (OLD.* IS DISTINCT FROM NEW.*) EXECUTE FUNCTION set_updated_at();

CREATE FUNCTION reject_external_boot_reservation_identity_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.activation_id, NEW.store_identity, NEW.owner_key, NEW.reserved_bytes)
       IS DISTINCT FROM
       (OLD.activation_id, OLD.store_identity, OLD.owner_key, OLD.reserved_bytes) THEN
        RAISE EXCEPTION 'external_boot_reservation identities are immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER external_boot_reservations_identity_immutable
    BEFORE UPDATE ON external_boot_reservations
    FOR EACH ROW EXECUTE FUNCTION reject_external_boot_reservation_identity_change();

CREATE TABLE external_boot_reservation_releases (
    activation_id     uuid PRIMARY KEY REFERENCES external_boot_activations (id) ON DELETE CASCADE,
    store_identity    text NOT NULL,
    owner_key          text NOT NULL,
    reserved_bytes     bigint NOT NULL CONSTRAINT external_boot_release_bytes
                           CHECK (reserved_bytes > 0),
    release_identity   text NOT NULL CONSTRAINT external_boot_release_digest
                           CHECK (release_identity ~ '^sha256:[0-9a-f]{64}$'),
    release_evidence   jsonb NOT NULL CONSTRAINT external_boot_release_evidence_size
                           CHECK (pg_column_size(release_evidence) <= 65536),
    teardown_evidence  jsonb CONSTRAINT external_boot_release_teardown_size
                           CHECK (teardown_evidence IS NULL
                                  OR pg_column_size(teardown_evidence) <= 65536),
    released_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT external_boot_release_evidence_schema CHECK (
        release_evidence ->> 'schema'
            IS NOT DISTINCT FROM 'external-boot-release-evidence-v1'
        AND (teardown_evidence IS NULL OR teardown_evidence ->> 'schema'
            IS NOT DISTINCT FROM 'external-boot-teardown-evidence-v1')
    )
);

CREATE FUNCTION reject_external_boot_release_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'external_boot_reservation_release is immutable';
END;
$$;

CREATE TRIGGER external_boot_reservation_releases_immutable
    BEFORE UPDATE OR DELETE ON external_boot_reservation_releases
    FOR EACH ROW EXECUTE FUNCTION reject_external_boot_release_mutation();

CREATE TABLE external_boot_recovery_attempts (
    activation_id                 uuid NOT NULL REFERENCES external_boot_activations (id)
                                      ON DELETE CASCADE,
    attempt_number                integer NOT NULL CONSTRAINT external_boot_attempt_number
                                      CHECK (attempt_number > 0),
    attempt_id                    uuid NOT NULL,
    authority_generation          bigint NOT NULL CONSTRAINT external_boot_attempt_generation
                                      CHECK (authority_generation > 0),
    recovery_basis                text NOT NULL CONSTRAINT external_boot_attempt_basis
                                      CHECK (recovery_basis IN ('recovery_point', 'pre_recovery')),
    resolution_operation          text CONSTRAINT external_boot_attempt_resolution_operation
                                      CHECK (resolution_operation IS NULL
                                             OR char_length(resolution_operation) BETWEEN 1 AND 255),
    resolution_identity           text CONSTRAINT external_boot_attempt_resolution_digest
                                      CHECK (resolution_identity IS NULL
                                             OR resolution_identity ~ '^sha256:[0-9a-f]{64}$'),
    acknowledged_composite_state  text CONSTRAINT external_boot_attempt_ack_digest
                                      CHECK (acknowledged_composite_state IS NULL
                                             OR acknowledged_composite_state
                                                ~ '^sha256:[0-9a-f]{64}$'),
    recovery_readiness_deadline   timestamptz,
    state                         text NOT NULL CONSTRAINT external_boot_attempt_state
                                      CHECK (state IN ('recovering', 'conflict',
                                                       'failed', 'recovered')),
    conflict_evidence             jsonb,
    terminal_evidence             jsonb,
    created_at                    timestamptz NOT NULL DEFAULT now(),
    updated_at                    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT external_boot_recovery_attempts_pkey
        PRIMARY KEY (activation_id, attempt_number),
    CONSTRAINT external_boot_recovery_attempt_id_key UNIQUE (attempt_id),
    CONSTRAINT external_boot_recovery_activation_attempt_key UNIQUE (activation_id, attempt_id),
    CONSTRAINT external_boot_attempt_deadline
        CHECK (state <> 'recovering' OR recovery_readiness_deadline IS NOT NULL),
    CONSTRAINT external_boot_attempt_resolution_group CHECK (
        (resolution_operation IS NULL AND resolution_identity IS NULL
         AND acknowledged_composite_state IS NULL)
        OR
        (resolution_operation IS NOT NULL AND resolution_identity IS NOT NULL
         AND acknowledged_composite_state IS NOT NULL)
    ),
    CONSTRAINT external_boot_attempt_evidence CHECK (
        (state <> 'conflict' OR conflict_evidence IS NOT NULL)
        AND (state NOT IN ('failed', 'recovered') OR terminal_evidence IS NOT NULL)
        AND (conflict_evidence IS NULL OR pg_column_size(conflict_evidence) <= 65536)
        AND (terminal_evidence IS NULL OR pg_column_size(terminal_evidence) <= 65536)
    ),
    CONSTRAINT external_boot_attempt_evidence_schema CHECK (
        (conflict_evidence IS NULL OR conflict_evidence ->> 'schema'
            IS NOT DISTINCT FROM 'external-boot-conflict-evidence-v1')
        AND (terminal_evidence IS NULL OR terminal_evidence ->> 'schema'
            IS NOT DISTINCT FROM 'external-boot-terminal-evidence-v1')
    )
);

CREATE TRIGGER external_boot_recovery_attempts_set_updated_at
    BEFORE UPDATE ON external_boot_recovery_attempts
    FOR EACH ROW WHEN (OLD.* IS DISTINCT FROM NEW.*) EXECUTE FUNCTION set_updated_at();

ALTER TABLE external_boot_activations
    ADD CONSTRAINT external_boot_activation_current_attempt_fk
    FOREIGN KEY (id, current_attempt_id)
    REFERENCES external_boot_recovery_attempts (activation_id, attempt_id)
    DEFERRABLE INITIALLY DEFERRED;

GRANT SELECT, INSERT, UPDATE ON TABLE external_boot_activations TO kdive_server;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE external_boot_reservations TO kdive_server;
GRANT SELECT, INSERT, UPDATE ON TABLE external_boot_recovery_attempts TO kdive_server;
GRANT SELECT, INSERT ON TABLE external_boot_reservation_releases TO kdive_server;

REVOKE ALL ON TABLE external_boot_activations, external_boot_reservations,
    external_boot_recovery_attempts, external_boot_reservation_releases
    FROM kdive_worker, kdive_reconciler;
GRANT SELECT ON TABLE external_boot_activations, external_boot_reservations,
    external_boot_recovery_attempts, external_boot_reservation_releases
    TO kdive_worker, kdive_reconciler;
