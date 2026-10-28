-- 0182_release_cleanup_evidence_phase.sql — the cleanup-evidence read admits a derived release
-- CLEANUP against the root release authority row (ADR-0614, #3401).
--
-- Migration 0145's read matched the authority row by the request's own purpose, operation,
-- identity and digest. A release holds one root authority row (release/release); its CLEANUP is a
-- derived phase whose identity and digest 0143 derives from that row, so the exact match could
-- never find it and the gate refused every release CLEANUP. This replaces the function with the
-- same signature and fences, adding one alternative for the (release, cleanup) pair only: the
-- root release row, with the request's identity and digest equal to the 0143 derivation. The
-- other admitted pairs keep their exact match, and the standalone (release, cleanup) root keeps
-- its exact match too.

CREATE OR REPLACE FUNCTION public.read_authorized_remote_module_cleanup_evidence(
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
      AND authority.provider_kind = p_provider_kind
      AND authority.authority_instance = p_authority_instance
      AND (
          (authority.purpose = p_purpose
           AND authority.operation = p_operation
           AND authority.operation_identity = p_operation_identity
           AND authority.operation_digest = p_operation_digest)
          OR (
              -- A release CLEANUP is a derived phase of the root release authority (ADR-0614):
              -- the row is the root release row and the request carries the identity and digest
              -- 0143 derives from it, exactly as resolve_current_external_boot_release_phase_
              -- authority does.
              p_purpose = 'release' AND p_operation = 'cleanup'
              AND authority.purpose = 'release' AND authority.operation = 'release'
              AND EXISTS (
                  SELECT 1
                  FROM public.derive_external_boot_release_phase_binding(
                      pg_catalog.jsonb_build_object(
                          'authority_id', authority.id, 'generation', authority.generation,
                          'system_id', authority.system_id,
                          'activation_id', authority.activation_id,
                          'run_id', authority.run_id,
                          'plan_identity', authority.plan_identity,
                          'provider_kind', authority.provider_kind,
                          'authority_instance', authority.authority_instance,
                          'worker_incarnation', authority.worker_incarnation,
                          'root_operation_identity', authority.operation_identity,
                          'root_operation_digest', authority.operation_digest),
                      'cleanup') AS derived
                  WHERE derived.operation_identity = p_operation_identity
                    AND derived.operation_digest = p_operation_digest
              )
          )
      )
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
