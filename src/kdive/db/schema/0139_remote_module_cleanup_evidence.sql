-- 0139_remote_module_cleanup_evidence.sql — authority read of worker-owned reap evidence

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
    WHERE authority.id = p_authority_id
      AND authority.generation = p_generation
      AND authority.worker_incarnation = p_peer_incarnation
      AND authority.system_id = p_system_id
      AND authority.activation_id = p_activation_id
      AND authority.run_id = p_run_id
      AND authority.plan_identity = p_plan_identity
      AND authority.operation = 'teardown'
      AND authority.operation_identity = p_operation_identity
      AND authority.operation_digest = p_operation_digest
      AND authority.state = 'current'
      AND worker.state = 'active' AND worker.fence_protocol = 4
      AND acknowledgement.journal_sequence = p_ack_sequence
      AND acknowledgement.journal_digest = p_ack_digest
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
       OR v_obligation.recovery_reference IS NULL THEN
        RETURN;
    END IF;

    IF (v_obligation.terminal_result->>'phase' = 'restored'
        AND v_obligation.terminal_result->>'status' = 'success'
        AND v_obligation.terminal_result->>'system_id' = p_system_id::text
        AND v_obligation.terminal_result->>'run_id' = p_run_id::text
        AND v_obligation.terminal_result->>'plan_identity' = p_plan_identity
        AND v_obligation.terminal_result->>'operation_nonce' = p_operation_nonce
        AND v_obligation.recovery_reference->>'system_id' = p_system_id::text
        AND v_obligation.recovery_reference->>'run_id' = p_run_id::text
        AND v_obligation.recovery_reference->>'plan_identity' = p_plan_identity
        AND v_obligation.recovery_reference->>'operation_nonce' = p_operation_nonce
        AND v_obligation.baseline_operation_identity =
            v_obligation.recovery_reference->>'operation_identity'
        AND v_obligation.baseline_result_identity =
            v_obligation.recovery_reference->>'result_identity') IS NOT TRUE THEN
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
    text, uuid, bigint, bigint, text, uuid, uuid, uuid, text, text, text, text
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.read_authorized_remote_module_cleanup_evidence(
    text, uuid, bigint, bigint, text, uuid, uuid, uuid, text, text, text, text
) TO kdive_provider_authority;
