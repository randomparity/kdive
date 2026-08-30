-- Trusted provider-host authority journal checkpoints (ADR-0584, #2126).

CREATE TABLE public.external_boot_authority_journal_heads (
    authority_instance text NOT NULL,
    system_id uuid NOT NULL REFERENCES public.systems (id) ON DELETE RESTRICT,
    sequence bigint NOT NULL,
    digest text NOT NULL,
    phase text NOT NULL,
    authority_id uuid NOT NULL,
    generation bigint NOT NULL,
    operation_identity text NOT NULL,
    pending_takeover jsonb,
    suspended_operation jsonb,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (authority_instance, system_id),
    CONSTRAINT external_boot_journal_sequence_positive CHECK (sequence > 0),
    CONSTRAINT external_boot_journal_generation_positive CHECK (generation > 0),
    CONSTRAINT external_boot_journal_digest CHECK (digest ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT external_boot_journal_instance_bounded
        CHECK (octet_length(authority_instance) BETWEEN 1 AND 255),
    CONSTRAINT external_boot_journal_operation_bounded
        CHECK (octet_length(operation_identity) BETWEEN 1 AND 255),
    CONSTRAINT external_boot_journal_phase CHECK (phase IN (
        'watermark-installed', 'takeover-superseded', 'takeover-acknowledged',
        'admitted', 'mutation-started', 'provider-returned', 'observed', 'terminal'
    )),
    CONSTRAINT external_boot_journal_pending_bounded CHECK (
        pending_takeover IS NULL OR (
            jsonb_typeof(pending_takeover) = 'object'
            AND octet_length(pending_takeover::text) <= 8192
            AND pending_takeover ?& ARRAY[
                'authority_id', 'generation', 'operation_identity', 'attempt_id',
                'request_digest', 'watermark_sequence', 'watermark_digest'
            ]
            AND (pending_takeover->>'generation')::numeric BETWEEN 1 AND 9223372036854775807
            AND (pending_takeover->>'watermark_sequence')::numeric
                BETWEEN 1 AND 9223372036854775807
            AND pending_takeover->>'request_digest' ~ '^sha256:[0-9a-f]{64}$'
            AND pending_takeover->>'watermark_digest' ~ '^sha256:[0-9a-f]{64}$'
            AND octet_length(pending_takeover->>'operation_identity') BETWEEN 1 AND 255
        )
    ),
    CONSTRAINT external_boot_journal_suspended_bounded CHECK (
        suspended_operation IS NULL OR (
            jsonb_typeof(suspended_operation) = 'object'
            AND octet_length(suspended_operation::text) <= 16384
            AND suspended_operation ?& ARRAY[
                'authority_id', 'generation', 'activation_id', 'operation_identity',
                'attempt_id', 'purpose', 'request_digest', 'phase', 'source_identity',
                'target_identity', 'ownership_digest'
            ]
            AND suspended_operation->>'phase' IN ('admitted', 'mutation-started')
            AND (suspended_operation->>'generation')::numeric BETWEEN 1 AND 9223372036854775807
            AND suspended_operation->>'request_digest' ~ '^sha256:[0-9a-f]{64}$'
            AND suspended_operation->>'ownership_digest' ~ '^sha256:[0-9a-f]{64}$'
            AND octet_length(suspended_operation->>'operation_identity') BETWEEN 1 AND 255
            AND octet_length(suspended_operation->>'source_identity') BETWEEN 1 AND 1024
            AND octet_length(suspended_operation->>'target_identity') BETWEEN 1 AND 1024
        )
    )
);

CREATE FUNCTION public.resolve_allocating_external_boot_authority(
    p_peer_incarnation text, p_authority_id uuid, p_generation bigint
) RETURNS TABLE (
    peer_incarnation_id text, authority_id uuid, generation bigint, system_id uuid,
    activation_id uuid, run_id uuid, plan_identity text, purpose text, provider_kind text,
    authority_instance text, operation_identity text, operation_digest text, state text
) LANGUAGE sql SECURITY DEFINER SET search_path = '' STABLE AS $$
    SELECT a.worker_incarnation, a.id, a.generation, a.system_id, a.activation_id, a.run_id,
           a.plan_identity, a.purpose, a.provider_kind, a.authority_instance,
           a.operation_identity, a.operation_digest, a.state
    FROM public.external_boot_authorities AS a
    JOIN public.worker_incarnations AS w ON w.incarnation = a.worker_incarnation
    WHERE pg_has_role(session_user, 'kdive_provider_authority', 'member')
      AND w.incarnation = p_peer_incarnation AND w.state = 'active' AND w.fence_protocol = 4
      AND a.id = p_authority_id AND a.generation = p_generation AND a.state = 'allocating'
      AND NOT EXISTS (
          SELECT 1 FROM public.external_boot_authorities AS newer
          WHERE newer.system_id = a.system_id AND newer.state = 'allocating'
            AND newer.generation > a.generation
      )
$$;

CREATE FUNCTION public.resolve_current_external_boot_authority(
    p_peer_incarnation text, p_authority_id uuid, p_generation bigint,
    p_ack_sequence bigint, p_ack_digest text
) RETURNS TABLE (
    peer_incarnation_id text, authority_id uuid, generation bigint, system_id uuid,
    activation_id uuid, run_id uuid, plan_identity text, purpose text, provider_kind text,
    authority_instance text, operation_identity text, operation_digest text, state text
) LANGUAGE sql SECURITY DEFINER SET search_path = '' STABLE AS $$
    SELECT a.worker_incarnation, a.id, a.generation, a.system_id, a.activation_id, a.run_id,
           a.plan_identity, a.purpose, a.provider_kind, a.authority_instance,
           a.operation_identity, a.operation_digest, a.state
    FROM public.external_boot_authorities AS a
    JOIN public.worker_incarnations AS w ON w.incarnation = a.worker_incarnation
    JOIN public.external_boot_authority_acknowledgements AS ack ON ack.authority_id = a.id
    WHERE pg_has_role(session_user, 'kdive_provider_authority', 'member')
      AND w.incarnation = p_peer_incarnation AND w.state = 'active' AND w.fence_protocol = 4
      AND a.id = p_authority_id AND a.generation = p_generation AND a.state = 'current'
      AND ack.journal_sequence = p_ack_sequence AND ack.journal_digest = p_ack_digest
$$;

CREATE FUNCTION public.read_external_boot_authority_journal_head(
    p_peer_incarnation text, p_authority_id uuid, p_generation bigint,
    p_authority_instance text
) RETURNS TABLE (
    authority_instance text, system_id uuid, sequence bigint, digest text, phase text,
    authority_id uuid, generation bigint, operation_identity text,
    pending_takeover jsonb, suspended_operation jsonb
) LANGUAGE sql SECURITY DEFINER SET search_path = '' STABLE AS $$
    SELECT h.authority_instance, h.system_id, h.sequence, h.digest, h.phase,
           h.authority_id, h.generation, h.operation_identity,
           h.pending_takeover, h.suspended_operation
    FROM public.external_boot_authorities AS a
    JOIN public.worker_incarnations AS w ON w.incarnation = a.worker_incarnation
    JOIN public.external_boot_authority_journal_heads AS h
      ON h.system_id = a.system_id AND h.authority_instance = a.authority_instance
    WHERE pg_has_role(session_user, 'kdive_provider_authority', 'member')
      AND w.incarnation = p_peer_incarnation AND w.state = 'active' AND w.fence_protocol = 4
      AND a.id = p_authority_id AND a.generation = p_generation
      AND a.authority_instance = p_authority_instance
$$;

CREATE FUNCTION public.advance_external_boot_authority_journal_head(
    p_peer_incarnation text, p_authority_id uuid, p_generation bigint,
    p_expected_sequence bigint, p_expected_digest text, p_record jsonb
) RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_authority public.external_boot_authorities%ROWTYPE;
    v_head public.external_boot_authority_journal_heads%ROWTYPE;
    v_phase text := p_record->>'phase';
    v_sequence bigint;
    v_digest text := p_record->>'record_digest';
    v_state_required text;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_provider_authority', 'member') THEN
        RAISE EXCEPTION 'provider authority is required' USING ERRCODE = '42501';
    END IF;
    IF p_expected_sequence < 0 OR p_expected_sequence >= 9223372036854775807
       OR p_expected_digest !~ '^sha256:[0-9a-f]{64}$'
       OR jsonb_typeof(p_record) IS DISTINCT FROM 'object'
       OR v_phase NOT IN (
           'watermark-installed', 'takeover-superseded', 'takeover-acknowledged',
           'admitted', 'mutation-started', 'provider-returned', 'observed', 'terminal'
       ) OR v_digest !~ '^sha256:[0-9a-f]{64}$' THEN
        RETURN 'conflict';
    END IF;
    v_sequence := p_expected_sequence + 1;
    SELECT a.* INTO v_authority FROM public.external_boot_authorities AS a
    WHERE a.id = p_authority_id AND a.generation = p_generation;
    IF NOT FOUND OR v_authority.worker_incarnation <> p_peer_incarnation THEN
        RETURN 'superseded';
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended('kdive:system:' || v_authority.system_id::text, 2126)
    );
    SELECT a.* INTO v_authority FROM public.external_boot_authorities AS a
    JOIN public.worker_incarnations AS w ON w.incarnation = a.worker_incarnation
    WHERE a.id = p_authority_id AND a.generation = p_generation
      AND w.incarnation = p_peer_incarnation AND w.state = 'active' AND w.fence_protocol = 4;
    IF NOT FOUND THEN RETURN 'superseded'; END IF;
    v_state_required := CASE WHEN v_phase LIKE 'takeover-%' OR v_phase = 'watermark-installed'
                             THEN 'allocating' ELSE 'current' END;
    IF v_authority.state <> v_state_required THEN RETURN 'superseded'; END IF;
    IF (p_record->>'sequence')::bigint <> v_sequence
       OR p_record->>'previous_digest' <> p_expected_digest
       OR (p_record->>'authority_id')::uuid <> v_authority.id
       OR (p_record->>'generation')::bigint <> v_authority.generation
       OR (p_record->>'system_id')::uuid <> v_authority.system_id
       OR (p_record->>'activation_id')::uuid <> v_authority.activation_id
       OR (p_record->>'run_id')::uuid <> v_authority.run_id
       OR p_record->>'plan_identity' <> v_authority.plan_identity
       OR p_record->>'purpose' <> v_authority.purpose
       OR p_record->>'provider_kind' <> v_authority.provider_kind
       OR p_record->>'authority_instance' <> v_authority.authority_instance
       OR p_record->>'operation_identity' <> v_authority.operation_identity
       OR p_record->>'operation_digest' <> v_authority.operation_digest THEN
        RETURN 'superseded';
    END IF;
    SELECT * INTO v_head FROM public.external_boot_authority_journal_heads
    WHERE authority_instance = v_authority.authority_instance
      AND system_id = v_authority.system_id FOR UPDATE;
    IF p_expected_sequence = 0 THEN
        IF FOUND OR p_expected_digest <> 'sha256:' || repeat('0', 64)
           OR v_phase <> 'watermark-installed' THEN RETURN 'conflict'; END IF;
        INSERT INTO public.external_boot_authority_journal_heads (
            authority_instance, system_id, sequence, digest, phase, authority_id,
            generation, operation_identity, pending_takeover
        ) VALUES (
            v_authority.authority_instance, v_authority.system_id, v_sequence, v_digest,
            v_phase, v_authority.id, v_authority.generation, p_record->>'operation_identity',
            jsonb_build_object(
                'authority_id', v_authority.id, 'generation', v_authority.generation,
                'operation_identity', p_record->>'operation_identity',
                'attempt_id', p_record->>'attempt_id', 'request_digest', p_record->>'operation_digest',
                'watermark_sequence', v_sequence, 'watermark_digest', v_digest
            )
        );
        RETURN 'advanced';
    END IF;
    IF NOT FOUND OR v_head.sequence <> p_expected_sequence OR v_head.digest <> p_expected_digest
    THEN RETURN 'conflict'; END IF;
    UPDATE public.external_boot_authority_journal_heads SET
        sequence = v_sequence, digest = v_digest, phase = v_phase,
        authority_id = v_authority.id, generation = v_authority.generation,
        operation_identity = p_record->>'operation_identity', updated_at = clock_timestamp(),
        pending_takeover = CASE
            WHEN v_phase = 'takeover-acknowledged' THEN NULL
            WHEN v_phase = 'watermark-installed' THEN jsonb_build_object(
                'authority_id', v_authority.id, 'generation', v_authority.generation,
                'operation_identity', p_record->>'operation_identity',
                'attempt_id', p_record->>'attempt_id', 'request_digest', p_record->>'operation_digest',
                'watermark_sequence', v_sequence, 'watermark_digest', v_digest
            ) ELSE pending_takeover END
    WHERE authority_instance = v_authority.authority_instance
      AND system_id = v_authority.system_id;
    RETURN 'advanced';
EXCEPTION WHEN data_exception OR check_violation OR numeric_value_out_of_range THEN
    RETURN 'conflict';
END
$$;

REVOKE ALL ON TABLE public.external_boot_authority_journal_heads
FROM PUBLIC, kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness,
    kdive_provider_authority;
REVOKE ALL ON FUNCTION
    public.resolve_allocating_external_boot_authority(text, uuid, bigint),
    public.resolve_current_external_boot_authority(text, uuid, bigint, bigint, text),
    public.read_external_boot_authority_journal_head(text, uuid, bigint, text),
    public.advance_external_boot_authority_journal_head(text, uuid, bigint, bigint, text, jsonb)
FROM PUBLIC, kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness,
    kdive_provider_authority;
GRANT EXECUTE ON FUNCTION
    public.resolve_allocating_external_boot_authority(text, uuid, bigint),
    public.resolve_current_external_boot_authority(text, uuid, bigint, bigint, text),
    public.read_external_boot_authority_journal_head(text, uuid, bigint, text),
    public.advance_external_boot_authority_journal_head(text, uuid, bigint, bigint, text, jsonb)
TO kdive_provider_authority;
