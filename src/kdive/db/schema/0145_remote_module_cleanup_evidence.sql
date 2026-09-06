-- 0145_remote_module_cleanup_evidence.sql — authority read of worker-owned reap evidence

ALTER TABLE remote_module_attempt_obligations
    ADD COLUMN restored_operation jsonb,
    ADD COLUMN restored_operation_identity text,
    ADD COLUMN restored_result jsonb,
    ADD COLUMN restored_result_identity text,
    ADD CONSTRAINT remote_module_attempt_restored_evidence_group CHECK (
        num_nonnulls(restored_operation, restored_operation_identity,
                     restored_result, restored_result_identity) IN (0, 4)
    ),
    ADD CONSTRAINT remote_module_attempt_restored_evidence_digests CHECK (
        (restored_operation_identity IS NULL
            OR restored_operation_identity ~ '^sha256:[0-9a-f]{64}$')
        AND (restored_result_identity IS NULL
            OR restored_result_identity ~ '^sha256:[0-9a-f]{64}$')
    ),
    ADD CONSTRAINT remote_module_attempt_restored_evidence_schema CHECK (
        (restored_operation IS NULL OR restored_operation ->> 'protocol'
            IS NOT DISTINCT FROM 'remote-module-operation-v1')
        AND (restored_result IS NULL OR restored_result ->> 'protocol'
            IS NOT DISTINCT FROM 'remote-module-result-v1')
    ),
    ADD CONSTRAINT remote_module_attempt_restored_evidence_ownership CHECK (
        restored_operation IS NULL OR (
            restored_operation ->> 'system_id' IS NOT DISTINCT FROM system_id::text
            AND restored_operation ->> 'run_id' IS NOT DISTINCT FROM run_id::text
            AND restored_operation ->> 'operation_nonce'
                IS NOT DISTINCT FROM operation_nonce
            AND restored_result ->> 'system_id' IS NOT DISTINCT FROM system_id::text
            AND restored_result ->> 'run_id' IS NOT DISTINCT FROM run_id::text
            AND restored_result ->> 'operation_nonce' IS NOT DISTINCT FROM operation_nonce)
    ),
    ADD CONSTRAINT remote_module_attempt_restored_evidence_size CHECK (
        (restored_operation IS NULL OR pg_column_size(restored_operation) <= 65536)
        AND (restored_result IS NULL OR pg_column_size(restored_result) <= 65536)
    ),
    ADD CONSTRAINT remote_module_attempt_restored_evidence_baseline CHECK (
        restored_operation IS NULL OR (
            terminal_operation IS NOT NULL
            AND reap_opened_at IS NOT NULL
            AND restored_operation ->> 'operation' = 'restore'
            AND restored_result ->> 'status' = 'success'
            AND restored_result ->> 'phase' = 'restored'
            AND restored_operation ->> 'plan_identity'
                IS NOT DISTINCT FROM terminal_operation ->> 'plan_identity'
            AND restored_operation ->> 'release'
                IS NOT DISTINCT FROM terminal_operation ->> 'release'
            AND restored_operation -> 'root_volume'
                IS NOT DISTINCT FROM terminal_operation -> 'root_volume'
            AND restored_operation ->> 'source_manifest'
                IS NOT DISTINCT FROM terminal_operation ->> 'source_manifest'
            AND restored_operation ->> 'appliance_image_digest'
                IS NOT DISTINCT FROM terminal_operation ->> 'appliance_image_digest'
            AND restored_operation ->> 'installed_manifest'
                IS NOT DISTINCT FROM terminal_result ->> 'installed_manifest'
            AND restored_operation -> 'capture_manifest'
                IS NOT DISTINCT FROM terminal_result -> 'capture_manifest'
            AND restored_operation -> 'capture_absent'
                IS NOT DISTINCT FROM terminal_result -> 'capture_absent'
            AND restored_result ->> 'plan_identity'
                IS NOT DISTINCT FROM restored_operation ->> 'plan_identity'
            AND restored_result ->> 'release'
                IS NOT DISTINCT FROM restored_operation ->> 'release'
            AND restored_result ->> 'root_volume_key'
                IS NOT DISTINCT FROM restored_operation -> 'root_volume' ->> 'key'
            AND restored_result ->> 'root_volume_identity'
                IS NOT DISTINCT FROM restored_operation -> 'root_volume' ->> 'identity'
            AND restored_result ->> 'source_manifest'
                IS NOT DISTINCT FROM restored_operation ->> 'source_manifest'
            AND restored_result ->> 'installed_manifest'
                IS NOT DISTINCT FROM restored_operation ->> 'installed_manifest'
            AND restored_result ->> 'appliance_image_digest'
                IS NOT DISTINCT FROM restored_operation ->> 'appliance_image_digest'
            AND restored_result -> 'capture_manifest'
                IS NOT DISTINCT FROM restored_operation -> 'capture_manifest'
            AND restored_result -> 'capture_absent'
                IS NOT DISTINCT FROM restored_operation -> 'capture_absent')
    );

ALTER TABLE remote_module_attempt_obligations
    DROP CONSTRAINT remote_module_attempt_evidence_schema,
    ADD CONSTRAINT remote_module_attempt_evidence_schema CHECK (
        (terminal_operation IS NULL OR terminal_operation ->> 'protocol'
            IS NOT DISTINCT FROM 'remote-module-operation-v1')
        AND (terminal_result IS NULL OR terminal_result ->> 'protocol'
            IS NOT DISTINCT FROM 'remote-module-result-v1')
        AND (recovery_reference IS NULL OR recovery_reference ->> 'protocol'
            IN ('remote-module-recovery-ref-v1', 'remote-module-recovery-ref-v2'))
    );

CREATE FUNCTION reject_remote_module_restored_evidence_rewrite() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.restored_operation IS NOT NULL
       AND (NEW.restored_operation, NEW.restored_operation_identity,
            NEW.restored_result, NEW.restored_result_identity)
           IS DISTINCT FROM
           (OLD.restored_operation, OLD.restored_operation_identity,
            OLD.restored_result, OLD.restored_result_identity) THEN
        RAISE EXCEPTION 'remote module attempt restored evidence is immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER remote_module_attempt_restored_evidence_write_once
    BEFORE UPDATE ON remote_module_attempt_obligations
    FOR EACH ROW EXECUTE FUNCTION reject_remote_module_restored_evidence_rewrite();

REVOKE ALL ON FUNCTION reject_remote_module_restored_evidence_rewrite() FROM PUBLIC;

CREATE OR REPLACE FUNCTION public.commit_worker_remote_module_evidence(
    p_job_id uuid,
    p_credential_hash bytea,
    p_job_attempt integer,
    p_system_id uuid,
    p_run_id uuid,
    p_operation_nonce text,
    p_action text,
    p_evidence jsonb DEFAULT NULL
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_incarnation text;
    v_payload jsonb;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_worker', 'member') THEN
        RAISE EXCEPTION 'worker authority is required' USING ERRCODE = '42501';
    END IF;
    IF p_job_id IS NULL OR p_credential_hash IS NULL
       OR octet_length(p_credential_hash) <> 32 OR p_job_attempt IS NULL OR p_job_attempt < 1
       OR p_system_id IS NULL OR p_run_id IS NULL OR p_operation_nonce IS NULL
       OR p_operation_nonce !~ '^[0-9a-f]{32}$'
       OR p_action IS NULL
       OR p_action NOT IN ('record-terminal', 'record-restored', 'discharge-reap') THEN
        RAISE EXCEPTION 'remote module worker evidence facts are invalid'
            USING ERRCODE = '22023';
    END IF;

    SELECT worker.incarnation INTO v_incarnation
    FROM public.worker_incarnations AS worker
    WHERE worker.credential_hash = p_credential_hash;
    IF v_incarnation IS NULL THEN
        RETURN false;
    END IF;
    PERFORM pg_advisory_xact_lock(
        pg_catalog.hashtextextended('kdive:system:' || p_system_id::text, 2125)
    );
    PERFORM pg_advisory_xact_lock(
        pg_catalog.hashtextextended('kdive:worker-incarnation:' || v_incarnation, 1803)
    );
    PERFORM 1 FROM public.worker_incarnations AS worker
    WHERE worker.incarnation = v_incarnation
      AND worker.credential_hash = p_credential_hash
      AND worker.state = 'active'
      AND worker.fence_protocol = 4
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN false;
    END IF;

    SELECT job.payload INTO v_payload
    FROM public.jobs AS job
    WHERE job.id = p_job_id
      AND job.worker_id = v_incarnation
      AND job.attempt = p_job_attempt
      AND job.state = 'running'
      AND job.kind IN ('boot', 'teardown')
      AND job.lease_expires_at IS NOT NULL
      AND job.lease_expires_at > pg_catalog.clock_timestamp()
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    IF v_payload -> 'remote_module_attempt_v1' IS DISTINCT FROM
       pg_catalog.jsonb_build_object(
           'schema', 'module-attempt-preparation-request-v1',
           'module_attempt_obligation', pg_catalog.jsonb_build_object(
               'schema', 'module-attempt-obligation-receipt-v1',
               'system_id', p_system_id::text,
               'run_id', p_run_id::text,
               'operation_nonce', p_operation_nonce
           )
       ) THEN
        RETURN false;
    END IF;

    PERFORM 1 FROM public.jobs AS job
    WHERE job.id = p_job_id
      AND job.worker_id = v_incarnation
      AND job.attempt = p_job_attempt
      AND job.state = 'running'
      AND job.lease_expires_at > pg_catalog.clock_timestamp();
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    PERFORM 1 FROM public.remote_module_attempt_obligations AS obligation
    WHERE obligation.system_id = p_system_id
      AND obligation.run_id = p_run_id
      AND obligation.operation_nonce = p_operation_nonce
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN false;
    END IF;

    IF p_action = 'record-terminal' THEN
        IF p_evidence IS NULL OR pg_catalog.jsonb_typeof(p_evidence) <> 'object' THEN
            RAISE EXCEPTION 'remote module terminal evidence is invalid'
                USING ERRCODE = '22023';
        END IF;
        UPDATE public.remote_module_attempt_obligations
        SET terminal_operation = p_evidence -> 'terminal_operation',
            terminal_operation_identity = p_evidence ->> 'terminal_operation_identity',
            terminal_result = p_evidence -> 'terminal_result',
            terminal_result_identity = p_evidence ->> 'terminal_result_identity',
            baseline_operation_identity = p_evidence ->> 'baseline_operation_identity',
            baseline_result_identity = p_evidence ->> 'baseline_result_identity',
            installed_entry_count = (p_evidence ->> 'installed_entry_count')::integer,
            installed_content_bytes = (p_evidence ->> 'installed_content_bytes')::bigint,
            recovery_reference = p_evidence -> 'recovery_reference',
            reap_opened_at = COALESCE(reap_opened_at, pg_catalog.now())
        WHERE system_id = p_system_id AND run_id = p_run_id
          AND operation_nonce = p_operation_nonce
          AND mutation_discharged_at IS NULL;
    ELSIF p_action = 'record-restored' THEN
        IF p_evidence IS NULL OR pg_catalog.jsonb_typeof(p_evidence) <> 'object' THEN
            RAISE EXCEPTION 'remote module restored evidence is invalid'
                USING ERRCODE = '22023';
        END IF;
        UPDATE public.remote_module_attempt_obligations
        SET restored_operation = p_evidence -> 'restored_operation',
            restored_operation_identity = p_evidence ->> 'restored_operation_identity',
            restored_result = p_evidence -> 'restored_result',
            restored_result_identity = p_evidence ->> 'restored_result_identity'
        WHERE system_id = p_system_id AND run_id = p_run_id
          AND operation_nonce = p_operation_nonce
          AND mutation_discharged_at IS NULL
          AND reap_opened_at IS NOT NULL
          AND terminal_operation IS NOT NULL;
    ELSE
        IF p_evidence IS NOT NULL THEN
            RAISE EXCEPTION 'reap discharge does not accept evidence' USING ERRCODE = '22023';
        END IF;
        UPDATE public.remote_module_attempt_obligations
        SET reap_discharged_at = COALESCE(reap_discharged_at, pg_catalog.now())
        WHERE system_id = p_system_id AND run_id = p_run_id
          AND operation_nonce = p_operation_nonce
          AND reap_opened_at IS NOT NULL AND terminal_operation IS NOT NULL;
    END IF;
    RETURN FOUND;
END
$$;

CREATE FUNCTION public.read_authorized_remote_module_cleanup_evidence(
    p_peer_incarnation text,
    p_authority_id uuid,
    p_generation bigint,
    p_ack_sequence bigint,
    p_ack_digest text,
    p_system_id uuid,
    p_activation_id uuid,
    p_run_id uuid,
    p_plan_identity text,
    p_purpose text,
    p_operation text,
    p_provider_kind text,
    p_authority_instance text,
    p_operation_identity text,
    p_operation_digest text,
    p_operation_nonce text
) RETURNS TABLE (cleanup_state text, recovery_reference jsonb)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_authority public.external_boot_authorities%ROWTYPE;
    v_obligation public.remote_module_attempt_obligations%ROWTYPE;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_provider_authority', 'member') THEN
        RAISE EXCEPTION 'provider authority is required' USING ERRCODE = '42501';
    END IF;
    IF (p_peer_incarnation IS NOT NULL
        AND octet_length(p_peer_incarnation) BETWEEN 1 AND 255
        AND p_authority_id IS NOT NULL
        AND p_generation IS NOT NULL AND p_generation > 0
        AND p_ack_sequence IS NOT NULL AND p_ack_sequence > 0
        AND p_ack_digest ~ '^sha256:[0-9a-f]{64}$'
        AND p_system_id IS NOT NULL AND p_activation_id IS NOT NULL AND p_run_id IS NOT NULL
        AND p_plan_identity ~ '^sha256:[0-9a-f]{64}$'
        AND (p_purpose, p_operation) IN (
            ('recover', 'recover'),
            ('release', 'cleanup'),
            ('teardown', 'teardown'),
            ('resolve-conflict', 'resolve-conflict')
        )
        AND p_provider_kind = 'remote-libvirt'
        AND p_authority_instance IS NOT NULL
        AND octet_length(p_authority_instance) BETWEEN 1 AND 255
        AND p_operation_identity IS NOT NULL AND octet_length(p_operation_identity) BETWEEN 1 AND 255
        AND p_operation_digest ~ '^sha256:[0-9a-f]{64}$'
        AND p_operation_nonce ~ '^[0-9a-f]{32}$') IS NOT TRUE THEN
        RAISE EXCEPTION 'remote module cleanup evidence facts are invalid'
            USING ERRCODE = '22023';
    END IF;

    SELECT authority.* INTO v_authority
    FROM public.external_boot_authorities AS authority
    JOIN public.worker_incarnations AS worker
      ON worker.incarnation = authority.worker_incarnation
    JOIN public.external_boot_authority_acknowledgements AS acknowledgement
      ON acknowledgement.authority_id = authority.id
    JOIN public.jobs AS job
      ON job.id = authority.job_id AND job.attempt = authority.job_attempt
    WHERE authority.id = p_authority_id
      AND authority.generation = p_generation
      AND authority.worker_incarnation = p_peer_incarnation
      AND authority.system_id = p_system_id
      AND authority.activation_id = p_activation_id
      AND authority.run_id = p_run_id
      AND authority.plan_identity = p_plan_identity
      AND authority.purpose = p_purpose
      AND authority.operation = p_operation
      AND authority.provider_kind = p_provider_kind
      AND authority.authority_instance = p_authority_instance
      AND authority.operation_identity = p_operation_identity
      AND authority.operation_digest = p_operation_digest
      AND authority.state = 'current'
      AND worker.state = 'active' AND worker.fence_protocol = 4
      AND job.state = 'running' AND job.worker_id = p_peer_incarnation
      AND job.lease_expires_at > pg_catalog.clock_timestamp()
      AND acknowledgement.journal_sequence = p_ack_sequence
      AND acknowledgement.journal_digest = p_ack_digest
      AND NOT EXISTS (
          SELECT 1 FROM public.external_boot_authorities AS successor
          WHERE successor.system_id = authority.system_id
            AND successor.generation > authority.generation
            AND successor.state IN ('allocating', 'current')
      )
    FOR SHARE OF authority, worker, acknowledgement;
    IF NOT FOUND THEN
        RETURN;
    END IF;

    SELECT obligation.* INTO v_obligation
    FROM public.remote_module_attempt_obligations AS obligation
    WHERE obligation.system_id = p_system_id
      AND obligation.run_id = p_run_id
      AND obligation.operation_nonce = p_operation_nonce
    FOR SHARE;
    IF NOT FOUND OR v_obligation.reap_opened_at IS NULL
       OR v_obligation.terminal_result IS NULL
       OR (p_operation = 'cleanup' AND v_obligation.restored_result IS NULL)
       OR v_obligation.recovery_reference IS NULL THEN
        RETURN;
    END IF;

    IF (v_obligation.terminal_result->>'phase' = 'installed'
        AND v_obligation.terminal_result->>'status' = 'success'
        AND v_obligation.recovery_reference->>'system_id' = p_system_id::text
        AND v_obligation.recovery_reference->>'run_id' = p_run_id::text
        AND v_obligation.recovery_reference->>'plan_identity' = p_plan_identity
        AND v_obligation.recovery_reference->>'operation_nonce' = p_operation_nonce
        AND v_obligation.baseline_operation_identity =
            v_obligation.recovery_reference->>'operation_identity'
        AND v_obligation.baseline_result_identity =
            v_obligation.recovery_reference->>'result_identity'
        AND v_obligation.terminal_operation_identity = 'sha256:' || encode(sha256(
            convert_to(v_obligation.terminal_operation->>'protocol', 'UTF8') ||
            decode('00', 'hex') || convert_to(
                public.canonical_external_boot_authority_json(v_obligation.terminal_operation),
                'UTF8'
            )
        ), 'hex')
        AND v_obligation.terminal_result_identity = 'sha256:' || encode(sha256(
            convert_to(v_obligation.terminal_result->>'protocol', 'UTF8') ||
            decode('00', 'hex') || convert_to(
                public.canonical_external_boot_authority_json(v_obligation.terminal_result),
                'UTF8'
            )
        ), 'hex')
        AND (p_operation <> 'cleanup' OR (
            v_obligation.restored_result->>'phase' = 'restored'
            AND v_obligation.restored_result->>'status' = 'success'
            AND v_obligation.restored_result->>'system_id' = p_system_id::text
            AND v_obligation.restored_result->>'run_id' = p_run_id::text
            AND v_obligation.restored_result->>'plan_identity' = p_plan_identity
            AND v_obligation.restored_result->>'operation_nonce' = p_operation_nonce
            AND v_obligation.restored_operation_identity = 'sha256:' || encode(sha256(
                convert_to(v_obligation.restored_operation->>'protocol', 'UTF8') ||
                decode('00', 'hex') || convert_to(
                    public.canonical_external_boot_authority_json(
                        v_obligation.restored_operation
                    ), 'UTF8'
                )
            ), 'hex')
            AND v_obligation.restored_result_identity = 'sha256:' || encode(sha256(
                convert_to(v_obligation.restored_result->>'protocol', 'UTF8') ||
                decode('00', 'hex') || convert_to(
                    public.canonical_external_boot_authority_json(v_obligation.restored_result),
                    'UTF8'
                )
            ), 'hex')
        ))) IS NOT TRUE THEN
        RETURN;
    END IF;

    cleanup_state := CASE
        WHEN v_obligation.reap_discharged_at IS NULL THEN 'open'
        ELSE 'discharged'
    END;
    recovery_reference := v_obligation.recovery_reference;
    RETURN NEXT;
END
$$;

REVOKE ALL ON FUNCTION public.read_authorized_remote_module_cleanup_evidence(
    text, uuid, bigint, bigint, text, uuid, uuid, uuid, text, text, text, text, text,
    text, text, text
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.read_authorized_remote_module_cleanup_evidence(
    text, uuid, bigint, bigint, text, uuid, uuid, uuid, text, text, text, text, text,
    text, text, text
) TO kdive_provider_authority;
