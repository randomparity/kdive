-- ADR-0614: derived cleanup proof precedes the root release capacity credit.

CREATE TABLE public.external_boot_release_cleanup_receipts (
    root_authority_id uuid PRIMARY KEY REFERENCES public.external_boot_authorities (id)
        ON DELETE RESTRICT,
    job_id uuid NOT NULL REFERENCES public.jobs (id) ON DELETE RESTRICT,
    job_attempt integer NOT NULL CHECK (job_attempt > 0),
    activation_id uuid NOT NULL REFERENCES public.external_boot_activations (id)
        ON DELETE RESTRICT,
    system_id uuid NOT NULL REFERENCES public.systems (id) ON DELETE RESTRICT,
    run_id uuid NOT NULL REFERENCES public.runs (id) ON DELETE RESTRICT,
    plan_identity text NOT NULL CHECK (plan_identity ~ '^sha256:[0-9a-f]{64}$'),
    operation_identity text NOT NULL CHECK (operation_identity ~ '^sha256:[0-9a-f]{64}$'),
    operation_digest text NOT NULL CHECK (operation_digest ~ '^sha256:[0-9a-f]{64}$'),
    journal_sequence bigint NOT NULL CHECK (journal_sequence > 0),
    journal_digest text NOT NULL CHECK (journal_digest ~ '^sha256:[0-9a-f]{64}$'),
    observed_absent_digest text NOT NULL CHECK (observed_absent_digest ~ '^sha256:[0-9a-f]{64}$'),
    adopted_from_root_authority_id uuid REFERENCES public.external_boot_authorities (id)
        ON DELETE RESTRICT,
    consumed boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    consumed_at timestamptz,
    CHECK (consumed = (consumed_at IS NOT NULL)),
    CHECK (adopted_from_root_authority_id IS NULL
           OR adopted_from_root_authority_id <> root_authority_id)
);

CREATE FUNCTION public.derive_external_boot_release_phase_binding(
    p_root jsonb, p_operation text
) RETURNS TABLE (operation_identity text, operation_digest text)
LANGUAGE plpgsql IMMUTABLE STRICT SET search_path = '' AS $$
DECLARE v_canonical text;
BEGIN
    IF jsonb_typeof(p_root) IS DISTINCT FROM 'object'
       OR p_operation NOT IN ('recover', 'cleanup')
       OR p_root <> jsonb_build_object(
           'authority_id', p_root->'authority_id', 'generation', p_root->'generation',
           'system_id', p_root->'system_id', 'activation_id', p_root->'activation_id',
           'run_id', p_root->'run_id', 'plan_identity', p_root->'plan_identity',
           'provider_kind', p_root->'provider_kind',
           'authority_instance', p_root->'authority_instance',
           'worker_incarnation', p_root->'worker_incarnation',
           'root_operation_identity', p_root->'root_operation_identity',
           'root_operation_digest', p_root->'root_operation_digest'
       ) OR p_root->>'plan_identity' !~ '^sha256:[0-9a-f]{64}$'
       OR p_root->>'root_operation_digest' !~ '^sha256:[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'external boot release phase root binding is invalid'
            USING ERRCODE = '22023';
    END IF;
    v_canonical := public.canonical_external_boot_authority_json(
        p_root || jsonb_build_object('operation', p_operation));
    operation_identity := 'sha256:' || encode(sha256(
        convert_to('kdive-external-boot-release-phase-identity-v1', 'UTF8') ||
        decode('00', 'hex') || convert_to(v_canonical, 'UTF8')), 'hex');
    operation_digest := 'sha256:' || encode(sha256(
        convert_to('kdive-external-boot-release-phase-digest-v1', 'UTF8') ||
        decode('00', 'hex') || convert_to(v_canonical, 'UTF8')), 'hex');
    RETURN NEXT;
END $$;

CREATE FUNCTION public.resolve_current_external_boot_release_phase_authority(
    p_peer_incarnation text, p_authority_id uuid, p_generation bigint,
    p_ack_sequence bigint, p_ack_digest text, p_operation text
) RETURNS TABLE (
    peer_incarnation_id text, authority_id uuid, generation bigint, system_id uuid,
    activation_id uuid, run_id uuid, plan_identity text, purpose text, operation text,
    provider_kind text, authority_instance text, operation_identity text,
    operation_digest text, state text
) LANGUAGE sql SECURITY DEFINER SET search_path = '' STABLE AS $$
    SELECT a.worker_incarnation, a.id, a.generation, a.system_id, a.activation_id, a.run_id,
           a.plan_identity, a.purpose, p_operation, a.provider_kind, a.authority_instance,
           derived.operation_identity, derived.operation_digest, a.state
    FROM public.external_boot_authorities AS a
    JOIN public.worker_incarnations AS w ON w.incarnation = a.worker_incarnation
    JOIN public.external_boot_authority_acknowledgements AS ack ON ack.authority_id = a.id
    JOIN public.jobs AS j ON j.id = a.job_id AND j.attempt = a.job_attempt
    CROSS JOIN LATERAL public.derive_external_boot_release_phase_binding(
        jsonb_build_object(
            'authority_id', a.id, 'generation', a.generation, 'system_id', a.system_id,
            'activation_id', a.activation_id, 'run_id', a.run_id,
            'plan_identity', a.plan_identity, 'provider_kind', a.provider_kind,
            'authority_instance', a.authority_instance,
            'worker_incarnation', a.worker_incarnation,
            'root_operation_identity', a.operation_identity,
            'root_operation_digest', a.operation_digest), p_operation) AS derived
    WHERE pg_has_role(session_user, 'kdive_provider_authority', 'member')
      AND p_operation IN ('recover', 'cleanup')
      AND w.incarnation = p_peer_incarnation AND w.state = 'active' AND w.fence_protocol = 4
      AND a.id = p_authority_id AND a.generation = p_generation AND a.state = 'current'
      AND a.purpose = 'release' AND a.operation = 'release'
      AND ack.journal_sequence = p_ack_sequence AND ack.journal_digest = p_ack_digest
      AND j.state = 'running' AND j.worker_id = p_peer_incarnation
      AND j.lease_expires_at > clock_timestamp()
$$;

CREATE FUNCTION public.finalize_external_boot_derived_release(
    p_credential_hash bytea, p_job_id uuid, p_attempt integer,
    p_authority_id uuid, p_generation bigint,
    p_release_identity text, p_release_evidence jsonb, p_cleanup_evidence jsonb
) RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_authority public.external_boot_authorities%ROWTYPE;
    v_job public.jobs%ROWTYPE;
    v_activation public.external_boot_activations%ROWTYPE;
    v_receipt public.external_boot_release_cleanup_receipts%ROWTYPE;
    v_reservation public.external_boot_reservations%ROWTYPE;
    v_release public.external_boot_reservation_releases%ROWTYPE;
    v_ack public.external_boot_authority_acknowledgements%ROWTYPE;
    v_incarnation text;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_worker', 'member') THEN
        RAISE EXCEPTION 'worker authority is required' USING ERRCODE = '42501';
    END IF;
    IF p_release_identity !~ '^sha256:[0-9a-f]{64}$'
       OR jsonb_typeof(p_release_evidence) IS DISTINCT FROM 'object'
       OR jsonb_typeof(p_cleanup_evidence) IS DISTINCT FROM 'object'
       OR pg_column_size(p_release_evidence) > 65536
       OR pg_column_size(p_cleanup_evidence) > 65536 THEN
        RAISE EXCEPTION 'derived release evidence is invalid' USING ERRCODE = '22023';
    END IF;
    SELECT incarnation INTO v_incarnation FROM public.worker_incarnations
    WHERE credential_hash = p_credential_hash AND state = 'active' AND fence_protocol = 4;
    SELECT * INTO v_authority FROM public.external_boot_authorities
    WHERE id = p_authority_id AND generation = p_generation FOR UPDATE;
    IF v_incarnation IS NULL OR NOT FOUND THEN RETURN 'superseded'; END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'kdive:system:' || v_authority.system_id::text, 2125));
    SELECT * INTO v_job FROM public.jobs WHERE id = p_job_id FOR UPDATE;
    SELECT * INTO v_activation FROM public.external_boot_activations
    WHERE id = v_authority.activation_id FOR UPDATE;
    SELECT * INTO v_receipt FROM public.external_boot_release_cleanup_receipts
    WHERE root_authority_id = p_authority_id FOR UPDATE;
    SELECT * INTO v_reservation FROM public.external_boot_reservations
    WHERE activation_id = v_authority.activation_id FOR UPDATE;
    SELECT * INTO v_release FROM public.external_boot_reservation_releases
    WHERE activation_id = v_authority.activation_id;
    SELECT * INTO v_ack FROM public.external_boot_authority_acknowledgements
    WHERE authority_id = p_authority_id;
    IF v_receipt.consumed THEN
        RETURN CASE WHEN v_activation.cleanup_complete
                    AND v_job.state = 'succeeded' AND v_authority.state = 'retired'
                    AND v_release.release_identity = p_release_identity
                    AND v_release.release_evidence = p_release_evidence
                    AND v_activation.cleanup_evidence = p_cleanup_evidence
                    THEN 'applied' ELSE 'conflict' END;
    END IF;
    IF (v_authority.state = 'current' AND v_authority.purpose = 'release'
       AND v_authority.operation = 'release' AND v_authority.worker_incarnation = v_incarnation
       AND v_authority.job_id = p_job_id AND v_authority.job_attempt = p_attempt
       AND v_job.state = 'running' AND v_job.worker_id = v_incarnation
       AND v_job.attempt = p_attempt AND v_job.lease_expires_at > clock_timestamp()
       AND v_activation.state = 'recovered' AND NOT v_activation.cleanup_complete
       AND v_receipt.job_id = p_job_id AND v_receipt.job_attempt = p_attempt
       AND v_receipt.activation_id = v_authority.activation_id
       AND v_receipt.system_id = v_authority.system_id
       AND v_receipt.run_id = v_authority.run_id
       AND v_receipt.plan_identity = v_authority.plan_identity
       AND NOT v_receipt.consumed AND v_reservation.state = 'ready'
       AND p_release_evidence = jsonb_build_object(
           'schema', 'external-boot-release-evidence-v1',
           'activation_id', v_authority.activation_id::text,
           'system_id', v_authority.system_id::text,
           'store_identity', jsonb_build_object('ref', v_reservation.store_identity),
           'owner_key', jsonb_build_object('ref', v_reservation.owner_key),
           'reserved_bytes', v_reservation.reserved_bytes,
           'enumeration_complete', true,
           'objects', jsonb_build_array(),
           'verified_at', p_release_evidence->'verified_at')
       AND jsonb_typeof(p_release_evidence->'verified_at') = 'string'
       AND p_cleanup_evidence = jsonb_build_object(
           'schema', 'external-boot-cleanup-evidence-v1',
           'activation_id', v_authority.activation_id::text,
           'system_id', v_authority.system_id::text,
           'release_identity', p_release_identity,
           'mode', 'ordinary',
           'completed_at', p_cleanup_evidence->'completed_at')
       AND jsonb_typeof(p_cleanup_evidence->'completed_at') = 'string') IS NOT TRUE THEN
        RETURN 'superseded';
    END IF;
    INSERT INTO public.external_boot_reservation_releases (
        activation_id, store_identity, owner_key, reserved_bytes,
        release_identity, release_evidence
    ) VALUES (
        v_reservation.activation_id, v_reservation.store_identity, v_reservation.owner_key,
        v_reservation.reserved_bytes, p_release_identity, p_release_evidence);
    DELETE FROM public.external_boot_reservations
    WHERE activation_id = v_authority.activation_id;
    UPDATE public.external_boot_activations SET cleanup_complete = true,
        cleanup_evidence = p_cleanup_evidence WHERE id = v_authority.activation_id;
    UPDATE public.external_boot_release_cleanup_receipts
    SET consumed = true, consumed_at = clock_timestamp()
    WHERE root_authority_id = p_authority_id;
    UPDATE public.jobs SET state = 'succeeded', result_ref = NULL
    WHERE id = p_job_id AND state = 'running';
    UPDATE public.external_boot_authorities SET state = 'retired', retired_at = clock_timestamp()
    WHERE id = p_authority_id AND state = 'current';
    INSERT INTO public.external_boot_authority_audit (
        authority_id, system_id, allocation_id, activation_id, run_id, plan_identity, job_id,
        job_attempt, worker_incarnation, generation, purpose, provider_kind,
        authority_instance, operation, operation_identity, operation_digest,
        journal_sequence, journal_digest, outcome
    ) VALUES (
        p_authority_id, v_authority.system_id, v_authority.allocation_id, v_authority.activation_id,
        v_authority.run_id, v_authority.plan_identity, p_job_id, p_attempt, v_incarnation,
        p_generation, v_authority.purpose, v_authority.provider_kind, v_authority.authority_instance,
        v_authority.operation, v_authority.operation_identity, v_authority.operation_digest,
        v_ack.journal_sequence, v_ack.journal_digest, 'result_committed'
    );
    RETURN 'applied';
END $$;

CREATE FUNCTION public.begin_external_boot_derived_release_recovery(
    p_credential_hash bytea, p_job_id uuid, p_attempt integer,
    p_authority_id uuid, p_generation bigint, p_attempt_id uuid, p_deadline timestamptz
) RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_authority public.external_boot_authorities%ROWTYPE;
    v_job public.jobs%ROWTYPE;
    v_activation public.external_boot_activations%ROWTYPE;
    v_incarnation text;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_worker', 'member') THEN
        RAISE EXCEPTION 'worker authority is required' USING ERRCODE = '42501';
    END IF;
    SELECT incarnation INTO v_incarnation FROM public.worker_incarnations
    WHERE credential_hash = p_credential_hash AND state = 'active' AND fence_protocol = 4;
    SELECT * INTO v_authority FROM public.external_boot_authorities
    WHERE id = p_authority_id AND generation = p_generation FOR UPDATE;
    IF v_incarnation IS NULL OR v_authority.id IS NULL THEN RETURN 'superseded'; END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('kdive:system:' || v_authority.system_id::text, 2125));
    SELECT * INTO v_job FROM public.jobs WHERE id = p_job_id FOR UPDATE;
    SELECT * INTO v_activation FROM public.external_boot_activations
    WHERE id = v_authority.activation_id FOR UPDATE;
    IF (v_authority.state = 'current' AND v_authority.purpose = 'release'
        AND v_authority.operation = 'release' AND v_authority.worker_incarnation = v_incarnation
        AND v_authority.job_id = p_job_id AND v_authority.job_attempt = p_attempt
        AND v_job.state = 'running' AND v_job.worker_id = v_incarnation
        AND v_job.attempt = p_attempt AND v_job.lease_expires_at > clock_timestamp()
        AND p_deadline > clock_timestamp() AND v_activation.cleanup_complete = false) IS NOT TRUE THEN
        RETURN 'superseded';
    END IF;
    IF v_activation.state = 'recovering' AND v_activation.current_attempt_id = p_attempt_id THEN
        RETURN 'applied';
    END IF;
    IF v_activation.state <> 'active' THEN RETURN 'superseded'; END IF;
    INSERT INTO public.external_boot_recovery_attempts (
        activation_id, attempt_number, attempt_id, authority_generation, recovery_basis,
        recovery_readiness_deadline, state
    ) VALUES (
        v_activation.id,
        coalesce((SELECT max(attempt_number) + 1 FROM public.external_boot_recovery_attempts
                  WHERE activation_id = v_activation.id), 1),
        p_attempt_id, p_generation, 'recovery_point', p_deadline, 'recovering'
    );
    UPDATE public.external_boot_activations SET state = 'recovering', current_attempt_id = p_attempt_id
    WHERE id = v_activation.id AND state = 'active';
    RETURN 'applied';
END $$;

CREATE FUNCTION public.commit_external_boot_derived_release_recovery(
    p_credential_hash bytea, p_job_id uuid, p_attempt integer,
    p_authority_id uuid, p_generation bigint, p_operation_identity text,
    p_operation_digest text, p_evidence jsonb
) RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_authority public.external_boot_authorities%ROWTYPE;
    v_job public.jobs%ROWTYPE;
    v_activation public.external_boot_activations%ROWTYPE;
    v_attempt public.external_boot_recovery_attempts%ROWTYPE;
    v_ack public.external_boot_authority_acknowledgements%ROWTYPE;
    v_head public.external_boot_authority_journal_heads%ROWTYPE;
    v_bound record;
    v_incarnation text;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_worker', 'member') THEN
        RAISE EXCEPTION 'worker authority is required' USING ERRCODE = '42501';
    END IF;
    SELECT incarnation INTO v_incarnation FROM public.worker_incarnations
    WHERE credential_hash = p_credential_hash AND state = 'active' AND fence_protocol = 4;
    SELECT * INTO v_authority FROM public.external_boot_authorities
    WHERE id = p_authority_id AND generation = p_generation FOR UPDATE;
    IF v_incarnation IS NULL OR v_authority.id IS NULL THEN RETURN 'superseded'; END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('kdive:system:' || v_authority.system_id::text, 2125));
    SELECT * INTO v_job FROM public.jobs WHERE id = p_job_id FOR UPDATE;
    SELECT * INTO v_activation FROM public.external_boot_activations
    WHERE id = v_authority.activation_id FOR UPDATE;
    SELECT * INTO v_attempt FROM public.external_boot_recovery_attempts
    WHERE activation_id = v_authority.activation_id AND attempt_id = v_activation.current_attempt_id FOR UPDATE;
    SELECT * INTO v_ack FROM public.external_boot_authority_acknowledgements WHERE authority_id = p_authority_id;
    SELECT * INTO v_head FROM public.external_boot_authority_journal_heads
    WHERE system_id = v_authority.system_id AND authority_instance = v_authority.authority_instance FOR UPDATE;
    SELECT * INTO v_bound FROM public.derive_external_boot_release_phase_binding(jsonb_build_object(
        'authority_id', v_authority.id, 'generation', v_authority.generation,
        'system_id', v_authority.system_id, 'activation_id', v_authority.activation_id,
        'run_id', v_authority.run_id, 'plan_identity', v_authority.plan_identity,
        'provider_kind', v_authority.provider_kind, 'authority_instance', v_authority.authority_instance,
        'worker_incarnation', v_authority.worker_incarnation,
        'root_operation_identity', v_authority.operation_identity,
        'root_operation_digest', v_authority.operation_digest), 'recover');
    IF v_activation.state = 'recovered' THEN
        RETURN CASE WHEN v_activation.terminal_evidence = p_evidence THEN 'applied' ELSE 'conflict' END;
    END IF;
    IF (v_authority.state = 'current' AND v_authority.purpose = 'release'
        AND v_authority.operation = 'release' AND v_authority.worker_incarnation = v_incarnation
        AND v_authority.job_id = p_job_id AND v_authority.job_attempt = p_attempt
        AND v_job.state = 'running' AND v_job.worker_id = v_incarnation AND v_job.attempt = p_attempt
        AND v_job.lease_expires_at > clock_timestamp() AND v_activation.state = 'recovering'
        AND v_attempt.state = 'recovering' AND v_attempt.authority_generation = p_generation
        AND v_bound.operation_identity = p_operation_identity AND v_bound.operation_digest = p_operation_digest
        AND v_head.authority_id = p_authority_id AND v_head.generation = p_generation
        AND v_head.phase = 'terminal' AND v_head.head_record->>'operation' = 'recover'
        AND v_head.head_record->>'operation_identity' = p_operation_identity
        AND v_head.head_record->>'operation_digest' = p_operation_digest
        AND v_head.head_record #>> '{observation,category}' = 'source'
        AND p_evidence = jsonb_build_object(
            'schema', 'external-boot-terminal-evidence-v1',
            'activation_id', v_authority.activation_id::text, 'system_id', v_authority.system_id::text,
            'outcome', 'recovered', 'composite_state', v_ack.positive_quiescence_digest,
            'objects', p_evidence->'objects', 'observed_at', p_evidence->'observed_at')
        AND jsonb_typeof(p_evidence->'objects') = 'array'
        AND jsonb_typeof(p_evidence->'observed_at') = 'string') IS NOT TRUE THEN
        RETURN 'superseded';
    END IF;
    UPDATE public.external_boot_recovery_attempts SET state = 'recovered', terminal_evidence = p_evidence,
        recovery_readiness_deadline = NULL WHERE activation_id = v_activation.id AND attempt_id = v_attempt.attempt_id;
    UPDATE public.external_boot_activations SET state = 'recovered', terminal_evidence = p_evidence
    WHERE id = v_activation.id AND state = 'recovering';
    RETURN 'applied';
END $$;

CREATE FUNCTION public.record_external_boot_release_cleanup_receipt(
    p_credential_hash bytea, p_job_id uuid, p_attempt integer,
    p_authority_id uuid, p_generation bigint, p_operation_identity text,
    p_operation_digest text, p_journal_sequence bigint, p_journal_digest text,
    p_observed_absent_digest text
) RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_authority public.external_boot_authorities%ROWTYPE;
    v_job public.jobs%ROWTYPE;
    v_activation public.external_boot_activations%ROWTYPE;
    v_head public.external_boot_authority_journal_heads%ROWTYPE;
    v_incarnation text;
    v_bound record;
    v_existing public.external_boot_release_cleanup_receipts%ROWTYPE;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_worker', 'member') THEN
        RAISE EXCEPTION 'worker authority is required' USING ERRCODE = '42501';
    END IF;
    SELECT incarnation INTO v_incarnation FROM public.worker_incarnations
    WHERE credential_hash = p_credential_hash AND state = 'active' AND fence_protocol = 4;
    SELECT * INTO v_authority FROM public.external_boot_authorities
    WHERE id = p_authority_id AND generation = p_generation FOR UPDATE;
    IF v_incarnation IS NULL OR NOT FOUND THEN RETURN 'superseded'; END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'kdive:system:' || v_authority.system_id::text, 2125));
    SELECT * INTO v_job FROM public.jobs WHERE id = p_job_id FOR UPDATE;
    SELECT * INTO v_activation FROM public.external_boot_activations
    WHERE id = v_authority.activation_id FOR UPDATE;
    SELECT * INTO v_head FROM public.external_boot_authority_journal_heads
    WHERE system_id = v_authority.system_id
      AND authority_instance = v_authority.authority_instance FOR UPDATE;
    SELECT * INTO v_bound FROM public.derive_external_boot_release_phase_binding(
        jsonb_build_object(
            'authority_id', v_authority.id, 'generation', v_authority.generation,
            'system_id', v_authority.system_id, 'activation_id', v_authority.activation_id,
            'run_id', v_authority.run_id, 'plan_identity', v_authority.plan_identity,
            'provider_kind', v_authority.provider_kind,
            'authority_instance', v_authority.authority_instance,
            'worker_incarnation', v_authority.worker_incarnation,
            'root_operation_identity', v_authority.operation_identity,
            'root_operation_digest', v_authority.operation_digest), 'cleanup');
    IF (v_authority.state = 'current' AND v_authority.purpose = 'release'
       AND v_authority.operation = 'release' AND v_authority.worker_incarnation = v_incarnation
       AND v_authority.job_id = p_job_id AND v_authority.job_attempt = p_attempt
       AND v_job.state = 'running' AND v_job.worker_id = v_incarnation
       AND v_job.attempt = p_attempt AND v_job.lease_expires_at > clock_timestamp()
       AND v_activation.state = 'recovered' AND NOT v_activation.cleanup_complete
       AND v_bound.operation_identity = p_operation_identity
       AND v_bound.operation_digest = p_operation_digest
       AND v_head.authority_id = p_authority_id AND v_head.generation = p_generation
       AND v_head.phase = 'terminal' AND v_head.sequence = p_journal_sequence
       AND v_head.digest = p_journal_digest
       AND v_head.head_record->>'operation' = 'cleanup'
       AND v_head.head_record->>'operation_identity' = p_operation_identity
       AND v_head.head_record->>'operation_digest' = p_operation_digest
       AND v_head.head_record #>> '{observation,category}' = 'absent'
       AND v_head.head_record #>> '{observation,composite_state}' = p_observed_absent_digest
    ) IS NOT TRUE THEN RETURN 'superseded'; END IF;
    INSERT INTO public.external_boot_release_cleanup_receipts (
        root_authority_id, job_id, job_attempt, activation_id, system_id, run_id,
        plan_identity, operation_identity, operation_digest, journal_sequence,
        journal_digest, observed_absent_digest
    ) VALUES (
        p_authority_id, p_job_id, p_attempt, v_authority.activation_id,
        v_authority.system_id, v_authority.run_id, v_authority.plan_identity,
        p_operation_identity, p_operation_digest, p_journal_sequence,
        p_journal_digest, p_observed_absent_digest
    ) ON CONFLICT (root_authority_id) DO NOTHING;
    SELECT * INTO v_existing FROM public.external_boot_release_cleanup_receipts
    WHERE root_authority_id = p_authority_id;
    IF (v_existing.job_id, v_existing.job_attempt, v_existing.operation_identity,
        v_existing.operation_digest, v_existing.journal_sequence,
        v_existing.journal_digest, v_existing.observed_absent_digest, v_existing.consumed)
       IS DISTINCT FROM
       (p_job_id, p_attempt, p_operation_identity, p_operation_digest,
        p_journal_sequence, p_journal_digest, p_observed_absent_digest, false) THEN
        RETURN 'conflict';
    END IF;
    RETURN 'applied';
END $$;

-- The release authority remains the immutable root while these two provider mutations advance
-- its single journal lane.  Migration 0135 installed the preparation-aware definition; alter that
-- exact definition rather than adding another mutable head or rewriting the root job payload.
DO $$
DECLARE
    v_definition text;
BEGIN
    SELECT pg_get_functiondef(
        'public.advance_external_boot_authority_journal_head(text,uuid,bigint,bigint,text,jsonb)'::regprocedure
    ) INTO v_definition;
    IF v_definition NOT LIKE '%v_bound_operation text;%' THEN
        RAISE EXCEPTION 'external boot preparation journal shape changed';
    END IF;
    v_definition := replace(
        v_definition,
        E'WHEN ''release'' THEN (p_record->>''operation'') = ' ||
        E'ANY (ARRAY[''release'', ''cleanup'', ''fail''])',
        E'WHEN ''release'' THEN (p_record->>''operation'') = ' ||
        E'ANY (ARRAY[''recover'', ''release'', ''cleanup'', ''fail''])'
    );
    v_definition := replace(
        v_definition,
        E'NOT IN (''source'', ''target'', ''mixed'', ''unreadable'', ''conflict'')',
        E'NOT IN (''source'', ''target'', ''mixed'', ''unreadable'', ''conflict'', ''absent'')'
    );
    v_definition := replace(
        v_definition,
        E'NOT IN (''never-began'', ''source'', ''target'', ''conflict'')',
        E'NOT IN (''never-began'', ''source'', ''target'', ''conflict'', ''absent'')'
    );
    v_definition := replace(
        v_definition,
        E'    IF p_record->>''operation'' IN (''materialize'', ''prepare'')\n' ||
        E'       AND v_authority.purpose = ''activate'' AND v_authority.operation = ''activate'' THEN',
        E'    IF p_record->>''operation'' IN (''recover'', ''cleanup'')\n' ||
        E'       AND v_authority.purpose = ''release'' AND v_authority.operation = ''release'' THEN\n' ||
        E'        SELECT operation_identity, operation_digest\n' ||
        E'        INTO v_bound_identity, v_bound_digest\n' ||
        E'        FROM public.derive_external_boot_release_phase_binding(jsonb_build_object(\n' ||
        E'            ''authority_id'', v_authority.id, ''generation'', v_authority.generation,\n' ||
        E'            ''system_id'', v_authority.system_id, ''activation_id'', v_authority.activation_id,\n' ||
        E'            ''run_id'', v_authority.run_id, ''plan_identity'', v_authority.plan_identity,\n' ||
        E'            ''provider_kind'', v_authority.provider_kind,\n' ||
        E'            ''authority_instance'', v_authority.authority_instance,\n' ||
        E'            ''worker_incarnation'', v_authority.worker_incarnation,\n' ||
        E'            ''root_operation_identity'', v_authority.operation_identity,\n' ||
        E'            ''root_operation_digest'', v_authority.operation_digest\n' ||
        E'        ), p_record->>''operation'');\n' ||
        E'        v_bound_operation := p_record->>''operation'';\n' ||
        E'    ELSIF p_record->>''operation'' IN (''materialize'', ''prepare'')\n' ||
        E'       AND v_authority.purpose = ''activate'' AND v_authority.operation = ''activate'' THEN'
    );
    IF v_definition NOT LIKE '%derive_external_boot_release_phase_binding%' THEN
        RAISE EXCEPTION 'external boot release journal operation gate was not installed';
    END IF;
    v_definition := replace(
        v_definition,
        E'            OR (v_head.phase = ''terminal''\n' ||
        E'                AND v_head.head_record->>''operation'' = ''prepare''\n' ||
        E'                AND p_record->>''operation'' = ''activate'')',
        E'            OR (v_head.phase = ''terminal''\n' ||
        E'                AND v_head.head_record->>''operation'' = ''prepare''\n' ||
        E'                AND p_record->>''operation'' = ''activate'')\n' ||
        E'            OR (v_head.phase = ''takeover-acknowledged''\n' ||
        E'                AND p_record->>''operation'' = ''recover'')\n' ||
        E'            OR (v_head.phase = ''takeover-acknowledged''\n' ||
        E'                AND p_record->>''operation'' = ''cleanup''\n' ||
        E'                AND EXISTS (SELECT 1 FROM public.external_boot_activations AS activation\n' ||
        E'                            WHERE activation.id = v_authority.activation_id\n' ||
        E'                              AND activation.state = ''recovered''))\n' ||
        E'            OR (v_head.phase = ''terminal''\n' ||
        E'                AND v_head.head_record->>''operation'' = ''recover''\n' ||
        E'                AND p_record->>''operation'' = ''cleanup'')'
    );
    EXECUTE v_definition;
END
$$;

CREATE FUNCTION public.record_external_boot_release_cleanup_receipt_from_head(
    p_credential_hash bytea, p_job_id uuid, p_attempt integer,
    p_authority_id uuid, p_generation bigint, p_observed_absent_digest text
) RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_authority public.external_boot_authorities%ROWTYPE;
    v_head public.external_boot_authority_journal_heads%ROWTYPE;
    v_bound record;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_worker', 'member') THEN
        RAISE EXCEPTION 'worker authority is required' USING ERRCODE = '42501';
    END IF;
    SELECT * INTO v_authority FROM public.external_boot_authorities
    WHERE id = p_authority_id AND generation = p_generation;
    IF v_authority.id IS NULL THEN RETURN 'superseded'; END IF;
    SELECT * INTO v_head FROM public.external_boot_authority_journal_heads
    WHERE system_id = v_authority.system_id AND authority_instance = v_authority.authority_instance;
    SELECT * INTO v_bound FROM public.derive_external_boot_release_phase_binding(jsonb_build_object(
        'authority_id', v_authority.id, 'generation', v_authority.generation,
        'system_id', v_authority.system_id, 'activation_id', v_authority.activation_id,
        'run_id', v_authority.run_id, 'plan_identity', v_authority.plan_identity,
        'provider_kind', v_authority.provider_kind, 'authority_instance', v_authority.authority_instance,
        'worker_incarnation', v_authority.worker_incarnation,
        'root_operation_identity', v_authority.operation_identity,
        'root_operation_digest', v_authority.operation_digest), 'cleanup');
    RETURN public.record_external_boot_release_cleanup_receipt(
        p_credential_hash, p_job_id, p_attempt, p_authority_id, p_generation,
        v_bound.operation_identity, v_bound.operation_digest, v_head.sequence, v_head.digest,
        p_observed_absent_digest
    );
END $$;

CREATE FUNCTION public.adopt_external_boot_release_cleanup_receipt_from_head(
    p_credential_hash bytea, p_job_id uuid, p_attempt integer,
    p_authority_id uuid, p_generation bigint, p_observed_absent_digest text
) RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_authority public.external_boot_authorities%ROWTYPE;
    v_job public.jobs%ROWTYPE;
    v_head public.external_boot_authority_journal_heads%ROWTYPE;
    v_source public.external_boot_release_cleanup_receipts%ROWTYPE;
    v_bound record;
    v_incarnation text;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_worker', 'member') THEN
        RAISE EXCEPTION 'worker authority is required' USING ERRCODE = '42501';
    END IF;
    SELECT incarnation INTO v_incarnation FROM public.worker_incarnations
    WHERE credential_hash = p_credential_hash AND state = 'active' AND fence_protocol = 4;
    SELECT * INTO v_authority FROM public.external_boot_authorities
    WHERE id = p_authority_id AND generation = p_generation FOR UPDATE;
    IF v_incarnation IS NULL OR v_authority.id IS NULL THEN RETURN 'superseded'; END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'kdive:system:' || v_authority.system_id::text, 2125));
    SELECT * INTO v_job FROM public.jobs WHERE id = p_job_id FOR UPDATE;
    SELECT * INTO v_head FROM public.external_boot_authority_journal_heads
    WHERE system_id = v_authority.system_id AND authority_instance = v_authority.authority_instance
    FOR UPDATE;
    SELECT * INTO v_bound FROM public.derive_external_boot_release_phase_binding(jsonb_build_object(
        'authority_id', v_authority.id, 'generation', v_authority.generation,
        'system_id', v_authority.system_id, 'activation_id', v_authority.activation_id,
        'run_id', v_authority.run_id, 'plan_identity', v_authority.plan_identity,
        'provider_kind', v_authority.provider_kind, 'authority_instance', v_authority.authority_instance,
        'worker_incarnation', v_authority.worker_incarnation,
        'root_operation_identity', v_authority.operation_identity,
        'root_operation_digest', v_authority.operation_digest), 'cleanup');
    SELECT receipt.* INTO v_source FROM public.external_boot_release_cleanup_receipts AS receipt
    JOIN public.external_boot_authorities AS source ON source.id = receipt.root_authority_id
    WHERE receipt.activation_id = v_authority.activation_id
      AND receipt.system_id = v_authority.system_id AND receipt.run_id = v_authority.run_id
      AND receipt.plan_identity = v_authority.plan_identity AND receipt.job_id = p_job_id
      AND NOT receipt.consumed AND source.state IN ('retired', 'superseded')
    ORDER BY receipt.created_at DESC LIMIT 1 FOR UPDATE OF receipt;
    IF v_source.root_authority_id IS NULL THEN RETURN 'not_applicable'; END IF;
    IF (v_authority.state = 'current' AND v_authority.purpose = 'release'
        AND v_authority.operation = 'release' AND v_authority.worker_incarnation = v_incarnation
        AND v_authority.job_id = p_job_id AND v_authority.job_attempt = p_attempt
        AND v_job.state = 'running' AND v_job.worker_id = v_incarnation
        AND v_job.attempt = p_attempt AND v_job.lease_expires_at > clock_timestamp()
        AND v_head.authority_id = p_authority_id AND v_head.generation = p_generation
        AND v_head.phase = 'terminal' AND v_head.head_record->>'operation' = 'cleanup'
        AND v_head.head_record->>'operation_identity' = v_bound.operation_identity
        AND v_head.head_record->>'operation_digest' = v_bound.operation_digest
        AND v_head.head_record #>> '{observation,category}' = 'absent'
        AND v_head.head_record #>> '{observation,composite_state}' = p_observed_absent_digest
        AND v_source.observed_absent_digest = p_observed_absent_digest
    ) IS NOT TRUE THEN RETURN 'superseded'; END IF;
    INSERT INTO public.external_boot_release_cleanup_receipts (
        root_authority_id, job_id, job_attempt, activation_id, system_id, run_id,
        plan_identity, operation_identity, operation_digest, journal_sequence,
        journal_digest, observed_absent_digest, adopted_from_root_authority_id
    ) VALUES (
        p_authority_id, p_job_id, p_attempt, v_authority.activation_id, v_authority.system_id,
        v_authority.run_id, v_authority.plan_identity, v_bound.operation_identity,
        v_bound.operation_digest, v_head.sequence, v_head.digest, p_observed_absent_digest,
        v_source.root_authority_id
    ) ON CONFLICT (root_authority_id) DO NOTHING;
    RETURN 'applied';
END $$;

REVOKE ALL ON public.external_boot_release_cleanup_receipts FROM PUBLIC;
REVOKE ALL ON FUNCTION public.derive_external_boot_release_phase_binding(jsonb,text),
    public.resolve_current_external_boot_release_phase_authority(
        text,uuid,bigint,bigint,text,text),
    public.record_external_boot_release_cleanup_receipt(
        bytea,uuid,integer,uuid,bigint,text,text,bigint,text,text),
    public.begin_external_boot_derived_release_recovery(bytea,uuid,integer,uuid,bigint,uuid,timestamptz),
    public.commit_external_boot_derived_release_recovery(bytea,uuid,integer,uuid,bigint,text,text,jsonb),
    public.adopt_external_boot_release_cleanup_receipt_from_head(bytea,uuid,integer,uuid,bigint,text),
    public.record_external_boot_release_cleanup_receipt_from_head(bytea,uuid,integer,uuid,bigint,text)
FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.record_external_boot_release_cleanup_receipt(
    bytea,uuid,integer,uuid,bigint,text,text,bigint,text,text) TO kdive_worker;
GRANT EXECUTE ON FUNCTION public.adopt_external_boot_release_cleanup_receipt_from_head(
    bytea,uuid,integer,uuid,bigint,text) TO kdive_worker;
GRANT EXECUTE ON FUNCTION public.begin_external_boot_derived_release_recovery(
    bytea,uuid,integer,uuid,bigint,uuid,timestamptz),
    public.commit_external_boot_derived_release_recovery(bytea,uuid,integer,uuid,bigint,text,text,jsonb),
    public.record_external_boot_release_cleanup_receipt_from_head(bytea,uuid,integer,uuid,bigint,text)
TO kdive_worker;
GRANT EXECUTE ON FUNCTION public.resolve_current_external_boot_release_phase_authority(
    text,uuid,bigint,bigint,text,text) TO kdive_provider_authority;
REVOKE ALL ON FUNCTION public.finalize_external_boot_derived_release(
    bytea,uuid,integer,uuid,bigint,text,jsonb,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.finalize_external_boot_derived_release(
    bytea,uuid,integer,uuid,bigint,text,jsonb,jsonb) TO kdive_worker;
