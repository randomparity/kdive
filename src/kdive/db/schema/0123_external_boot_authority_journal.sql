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
    head_record jsonb NOT NULL,
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
    CONSTRAINT external_boot_journal_head_record_bounded
        CHECK (jsonb_typeof(head_record) = 'object' AND octet_length(head_record::text) <= 1048576),
    CONSTRAINT external_boot_journal_phase CHECK (phase IN (
        'watermark-installed', 'takeover-superseded', 'takeover-acknowledged',
        'admitted', 'mutation-started', 'provider-returned', 'observed', 'terminal'
    )),
    CONSTRAINT external_boot_journal_pending_bounded CHECK (
        pending_takeover IS NULL OR (
            jsonb_typeof(pending_takeover) = 'object'
            AND octet_length(pending_takeover::text) <= 8192
            AND pending_takeover = jsonb_build_object(
                'authority_id', pending_takeover->'authority_id',
                'generation', pending_takeover->'generation',
                'operation_identity', pending_takeover->'operation_identity',
                'attempt_id', pending_takeover->'attempt_id',
                'request_digest', pending_takeover->'request_digest',
                'watermark_sequence', pending_takeover->'watermark_sequence',
                'watermark_digest', pending_takeover->'watermark_digest'
            )
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
            AND jsonb_typeof(pending_takeover->'authority_id') = 'string'
            AND jsonb_typeof(pending_takeover->'attempt_id') = 'string'
            AND (pending_takeover->>'authority_id')::uuid IS NOT NULL
            AND (pending_takeover->>'attempt_id')::uuid IS NOT NULL
        )
    ),
    CONSTRAINT external_boot_journal_suspended_bounded CHECK (
        suspended_operation IS NULL OR (
            jsonb_typeof(suspended_operation) = 'object'
            AND octet_length(suspended_operation::text) <= 16384
            AND suspended_operation = jsonb_build_object(
                'authority_id', suspended_operation->'authority_id',
                'generation', suspended_operation->'generation',
                'activation_id', suspended_operation->'activation_id',
                'operation_identity', suspended_operation->'operation_identity',
                'attempt_id', suspended_operation->'attempt_id',
                'purpose', suspended_operation->'purpose',
                'operation', suspended_operation->'operation',
                'request_digest', suspended_operation->'request_digest',
                'phase', suspended_operation->'phase',
                'source_identity', suspended_operation->'source_identity',
                'target_identity', suspended_operation->'target_identity',
                'ownership_digest', suspended_operation->'ownership_digest'
            )
            AND suspended_operation ?& ARRAY[
                'authority_id', 'generation', 'activation_id', 'operation_identity',
                'attempt_id', 'purpose', 'request_digest', 'phase', 'source_identity',
                'target_identity', 'ownership_digest', 'operation'
            ]
            AND suspended_operation->>'phase' IN (
                'admitted', 'mutation-started', 'provider-returned', 'observed'
            )
            AND (suspended_operation->>'generation')::numeric BETWEEN 1 AND 9223372036854775807
            AND suspended_operation->>'request_digest' ~ '^sha256:[0-9a-f]{64}$'
            AND suspended_operation->>'ownership_digest' ~ '^sha256:[0-9a-f]{64}$'
            AND octet_length(suspended_operation->>'operation_identity') BETWEEN 1 AND 255
            AND octet_length(suspended_operation->>'source_identity') BETWEEN 1 AND 1024
            AND octet_length(suspended_operation->>'target_identity') BETWEEN 1 AND 1024
            AND jsonb_typeof(suspended_operation->'authority_id') = 'string'
            AND jsonb_typeof(suspended_operation->'activation_id') = 'string'
            AND jsonb_typeof(suspended_operation->'attempt_id') = 'string'
            AND (suspended_operation->>'authority_id')::uuid IS NOT NULL
            AND (suspended_operation->>'activation_id')::uuid IS NOT NULL
            AND (suspended_operation->>'attempt_id')::uuid IS NOT NULL
            AND octet_length(suspended_operation->>'purpose') BETWEEN 1 AND 255
            AND octet_length(suspended_operation->>'operation') BETWEEN 1 AND 255
        )
    )
);

CREATE FUNCTION public.canonical_external_boot_authority_json(p_value jsonb) RETURNS text
LANGUAGE sql IMMUTABLE STRICT SET search_path = '' AS $$
    SELECT CASE jsonb_typeof(p_value)
        WHEN 'object' THEN '{' || coalesce((
            SELECT string_agg(
                to_json(entry.key)::text || ':' ||
                public.canonical_external_boot_authority_json(entry.value), ',' ORDER BY entry.key
            ) FROM jsonb_each(p_value) AS entry
        ), '') || '}'
        WHEN 'array' THEN '[' || coalesce((
            SELECT string_agg(
                public.canonical_external_boot_authority_json(element.value),
                ',' ORDER BY element.ordinality
            ) FROM jsonb_array_elements(p_value) WITH ORDINALITY AS element(value, ordinality)
        ), '') || ']'
        ELSE p_value::text
    END
$$;

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
    v_canonical_record text := p_record->>'canonical_record';
    v_digest text;
    v_is_suspended boolean := false;
    v_pending_matches boolean := false;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_provider_authority', 'member') THEN
        RAISE EXCEPTION 'provider authority is required' USING ERRCODE = '42501';
    END IF;
    IF p_expected_sequence < 0 OR p_expected_sequence >= 9223372036854775807
       OR p_expected_digest !~ '^sha256:[0-9a-f]{64}$'
       OR jsonb_typeof(p_record) IS DISTINCT FROM 'object'
       OR (SELECT count(*) FROM jsonb_object_keys(p_record)) <> 26
       OR NOT p_record ?& ARRAY[
           'schema', 'authority_id', 'generation', 'system_id', 'activation_id', 'run_id',
           'plan_identity', 'purpose', 'provider_kind', 'authority_instance',
           'operation_identity', 'operation_digest', 'sequence', 'previous_digest', 'phase',
           'attempt_id', 'predecessor_generation', 'watermark_sequence', 'watermark_digest',
           'expected_source_identity', 'intended_target_identity', 'recovery_objects',
           'observation', 'outcome', 'operation', 'canonical_record'
       ]
       OR p_record->>'schema' <> 'external-boot-authority-v1'
       OR jsonb_typeof(p_record->'authority_id') <> 'string'
       OR jsonb_typeof(p_record->'system_id') <> 'string'
       OR jsonb_typeof(p_record->'activation_id') <> 'string'
       OR jsonb_typeof(p_record->'run_id') <> 'string'
       OR jsonb_typeof(p_record->'attempt_id') <> 'string'
       OR (p_record->>'attempt_id')::uuid IS NULL
       OR v_phase NOT IN (
           'watermark-installed', 'takeover-superseded', 'takeover-acknowledged',
           'admitted', 'mutation-started', 'provider-returned', 'observed', 'terminal'
       ) OR v_canonical_record IS NULL OR octet_length(v_canonical_record) > 1048576
       OR v_canonical_record <> public.canonical_external_boot_authority_json(
           p_record - 'canonical_record'
       ) THEN
        RETURN 'conflict';
    END IF;
    IF v_phase IN ('watermark-installed', 'takeover-superseded', 'takeover-acknowledged')
       AND (p_record->'expected_source_identity' <> 'null'::jsonb
            OR p_record->'intended_target_identity' <> 'null'::jsonb
            OR p_record->'recovery_objects' <> '[]'::jsonb
            OR p_record->'observation' <> 'null'::jsonb
            OR p_record->'outcome' <> 'null'::jsonb)
    THEN RETURN 'conflict'; END IF;
    IF v_phase IN ('watermark-installed', 'takeover-superseded', 'takeover-acknowledged')
       AND p_record->'operation' <> 'null'::jsonb
    THEN RETURN 'conflict'; END IF;
    IF v_phase NOT IN ('watermark-installed', 'takeover-superseded', 'takeover-acknowledged')
       AND (jsonb_typeof(p_record->'expected_source_identity') <> 'string'
            OR jsonb_typeof(p_record->'intended_target_identity') <> 'string'
            OR jsonb_typeof(p_record->'recovery_objects') <> 'array'
            OR jsonb_typeof(p_record->'operation') <> 'string')
    THEN RETURN 'conflict'; END IF;
    IF octet_length(p_record->>'authority_instance') NOT BETWEEN 1 AND 255
       OR octet_length(p_record->>'operation_identity') NOT BETWEEN 1 AND 255
       OR octet_length(p_record->>'provider_kind') NOT BETWEEN 1 AND 255
       OR (p_record->'operation' <> 'null'::jsonb
           AND octet_length(p_record->>'operation') NOT BETWEEN 1 AND 255)
       OR p_record->>'plan_identity' !~ '^sha256:[0-9a-f]{64}$'
       OR p_record->>'operation_digest' !~ '^sha256:[0-9a-f]{64}$'
       OR p_record->>'previous_digest' !~ '^sha256:[0-9a-f]{64}$'
       OR (p_record->>'generation')::numeric NOT BETWEEN 1 AND 9223372036854775807
       OR (p_record->>'sequence')::numeric NOT BETWEEN 1 AND 9223372036854775807
       OR jsonb_array_length(p_record->'recovery_objects') > 1024
       OR (v_phase NOT IN ('watermark-installed', 'takeover-superseded', 'takeover-acknowledged')
           AND (octet_length(p_record->>'expected_source_identity') NOT BETWEEN 1 AND 1024
                OR octet_length(p_record->>'intended_target_identity') NOT BETWEEN 1 AND 1024))
       OR EXISTS (
           SELECT 1 FROM jsonb_array_elements(p_record->'recovery_objects') AS item(value)
           WHERE jsonb_typeof(item.value) <> 'object'
              OR item.value <> jsonb_build_object(
                  'system_id', item.value->'system_id',
                  'activation_id', item.value->'activation_id',
                  'reference', item.value->'reference'
              )
              OR jsonb_typeof(item.value->'system_id') <> 'string'
              OR jsonb_typeof(item.value->'activation_id') <> 'string'
              OR jsonb_typeof(item.value->'reference') <> 'string'
              OR (item.value->>'system_id')::uuid <> (p_record->>'system_id')::uuid
              OR (item.value->>'activation_id')::uuid <> (p_record->>'activation_id')::uuid
              OR octet_length(item.value->>'reference') NOT BETWEEN 1 AND 1024
       )
       OR EXISTS (
           SELECT 1 FROM (
               SELECT public.canonical_external_boot_authority_json(item.value) AS encoded,
                      lag(public.canonical_external_boot_authority_json(item.value)) OVER (
                          ORDER BY item.ordinality
                      ) AS prior
               FROM jsonb_array_elements(p_record->'recovery_objects')
                    WITH ORDINALITY AS item(value, ordinality)
           ) AS ordered WHERE ordered.prior >= ordered.encoded
       )
    THEN RETURN 'conflict'; END IF;
    IF v_phase = 'watermark-installed' AND (
        p_record->'predecessor_generation' <> 'null'::jsonb
        OR p_record->'watermark_sequence' <> 'null'::jsonb
        OR p_record->'watermark_digest' <> 'null'::jsonb
    ) THEN RETURN 'conflict'; END IF;
    IF v_phase = 'takeover-superseded' AND (
        jsonb_typeof(p_record->'predecessor_generation') <> 'number'
        OR jsonb_typeof(p_record->'watermark_sequence') <> 'number'
        OR p_record->>'watermark_digest' !~ '^sha256:[0-9a-f]{64}$'
    ) THEN RETURN 'conflict'; END IF;
    IF v_phase = 'takeover-acknowledged' AND (
        p_record->'predecessor_generation' <> 'null'::jsonb
        OR jsonb_typeof(p_record->'watermark_sequence') <> 'number'
        OR p_record->>'watermark_digest' !~ '^sha256:[0-9a-f]{64}$'
    ) THEN RETURN 'conflict'; END IF;
    IF v_phase IN ('admitted', 'mutation-started', 'provider-returned')
       AND (p_record->'observation' <> 'null'::jsonb OR p_record->'outcome' <> 'null'::jsonb)
    THEN RETURN 'conflict'; END IF;
    IF v_phase = 'observed' AND (
        jsonb_typeof(p_record->'observation') <> 'object'
        OR p_record->'outcome' <> 'null'::jsonb
    ) THEN RETURN 'conflict'; END IF;
    IF p_record->'observation' <> 'null'::jsonb AND (
        p_record->'observation' <> jsonb_build_object(
            'schema', p_record->'observation'->'schema',
            'observation_id', p_record->'observation'->'observation_id',
            'category', p_record->'observation'->'category',
            'composite_state', p_record->'observation'->'composite_state'
        )
        OR p_record->'observation'->>'schema' <> 'external-boot-authority-v1'
        OR jsonb_typeof(p_record->'observation'->'observation_id') <> 'string'
        OR (p_record->'observation'->>'observation_id')::uuid IS NULL
        OR p_record->'observation'->>'category'
            NOT IN ('source', 'target', 'mixed', 'unreadable', 'conflict')
        OR p_record->'observation'->>'composite_state' !~ '^sha256:[0-9a-f]{64}$'
    ) THEN RETURN 'conflict'; END IF;
    IF v_phase = 'terminal' AND (
        p_record->>'outcome' NOT IN ('never-began', 'source', 'target', 'conflict')
        OR ((p_record->>'outcome' = 'never-began')
            IS DISTINCT FROM (p_record->'observation' = 'null'::jsonb))
    ) THEN RETURN 'conflict'; END IF;
    v_digest := 'sha256:' || encode(
        sha256(convert_to(v_canonical_record, 'UTF8')), 'hex'
    );
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
    IF (p_record->>'sequence')::bigint IS DISTINCT FROM v_sequence
       OR p_record->>'previous_digest' IS DISTINCT FROM p_expected_digest
    THEN RETURN 'conflict'; END IF;
    IF (p_record->>'authority_id')::uuid IS DISTINCT FROM v_authority.id
       OR (p_record->>'generation')::bigint IS DISTINCT FROM v_authority.generation
       OR (p_record->>'system_id')::uuid IS DISTINCT FROM v_authority.system_id
       OR (p_record->>'activation_id')::uuid IS DISTINCT FROM v_authority.activation_id
       OR (p_record->>'run_id')::uuid IS DISTINCT FROM v_authority.run_id
       OR p_record->>'plan_identity' IS DISTINCT FROM v_authority.plan_identity
       OR p_record->>'purpose' IS DISTINCT FROM v_authority.purpose
       OR p_record->>'provider_kind' IS DISTINCT FROM v_authority.provider_kind
       OR p_record->>'authority_instance' IS DISTINCT FROM v_authority.authority_instance
       OR p_record->>'operation_identity' IS DISTINCT FROM v_authority.operation_identity
       OR p_record->>'operation_digest' IS DISTINCT FROM v_authority.operation_digest THEN
        RETURN 'superseded';
    END IF;
    SELECT * INTO v_head FROM public.external_boot_authority_journal_heads
    WHERE authority_instance = v_authority.authority_instance
      AND system_id = v_authority.system_id FOR UPDATE;
    IF FOUND AND (p_record->>'sequence')::bigint = v_head.sequence THEN
        IF v_digest = v_head.digest AND p_record - 'canonical_record' = v_head.head_record
        THEN RETURN 'advanced'; ELSE RETURN 'conflict'; END IF;
    END IF;
    IF p_expected_sequence = 0 THEN
        IF FOUND OR p_expected_digest <> 'sha256:' || repeat('0', 64)
           OR v_phase <> 'watermark-installed' OR v_authority.state <> 'allocating'
           OR EXISTS (
               SELECT 1 FROM public.external_boot_authorities AS newer
               WHERE newer.system_id = v_authority.system_id
                 AND newer.state = 'allocating' AND newer.generation > v_authority.generation
           ) THEN RETURN 'superseded'; END IF;
        INSERT INTO public.external_boot_authority_journal_heads (
            authority_instance, system_id, sequence, digest, phase, authority_id,
            generation, operation_identity, head_record, pending_takeover
        ) VALUES (
            v_authority.authority_instance, v_authority.system_id, v_sequence, v_digest,
            v_phase, v_authority.id, v_authority.generation, p_record->>'operation_identity',
            p_record - 'canonical_record',
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
    IF v_head.phase IN ('admitted', 'mutation-started', 'provider-returned', 'observed')
       AND v_head.operation_identity = p_record->>'operation_identity'
       AND (p_record->>'attempt_id' IS DISTINCT FROM v_head.head_record->>'attempt_id'
            OR p_record->>'expected_source_identity'
                IS DISTINCT FROM v_head.head_record->>'expected_source_identity'
            OR p_record->>'intended_target_identity'
                IS DISTINCT FROM v_head.head_record->>'intended_target_identity'
            OR p_record->'recovery_objects'
                IS DISTINCT FROM v_head.head_record->'recovery_objects')
    THEN RETURN 'conflict'; END IF;
    v_is_suspended := v_head.suspended_operation IS NOT NULL
        AND v_head.suspended_operation->>'authority_id' = v_authority.id::text
        AND (v_head.suspended_operation->>'generation')::bigint = v_authority.generation
        AND v_head.suspended_operation->>'activation_id' = v_authority.activation_id::text
        AND v_head.suspended_operation->>'operation_identity' = p_record->>'operation_identity'
        AND v_head.suspended_operation->>'attempt_id' = p_record->>'attempt_id'
        AND v_head.suspended_operation->>'purpose' = p_record->>'purpose'
        AND v_head.suspended_operation->>'operation' = p_record->>'operation'
        AND v_head.suspended_operation->>'request_digest' = p_record->>'operation_digest'
        AND v_head.suspended_operation->>'source_identity' = p_record->>'expected_source_identity'
        AND v_head.suspended_operation->>'target_identity' = p_record->>'intended_target_identity'
        AND v_head.suspended_operation->>'ownership_digest' = 'sha256:' || encode(
            sha256(convert_to(
                public.canonical_external_boot_authority_json(p_record->'recovery_objects'),
                'UTF8'
            )), 'hex'
        );
    v_pending_matches := v_head.pending_takeover IS NOT NULL
        AND v_head.pending_takeover->>'authority_id' = v_authority.id::text
        AND (v_head.pending_takeover->>'generation')::bigint = v_authority.generation
        AND v_head.pending_takeover->>'operation_identity' = p_record->>'operation_identity'
        AND v_head.pending_takeover->>'attempt_id' = p_record->>'attempt_id'
        AND v_head.pending_takeover->>'request_digest' = p_record->>'operation_digest';
    IF v_phase IN ('watermark-installed', 'takeover-superseded', 'takeover-acknowledged') THEN
        IF v_authority.state <> 'allocating' OR EXISTS (
            SELECT 1 FROM public.external_boot_authorities AS newer
            WHERE newer.system_id = v_authority.system_id
              AND newer.state = 'allocating' AND newer.generation > v_authority.generation
        ) THEN RETURN 'superseded'; END IF;
    ELSIF v_phase IN ('admitted', 'mutation-started') THEN
        IF v_authority.state <> 'current' OR NOT EXISTS (
            SELECT 1 FROM public.external_boot_authority_acknowledgements AS ack
            WHERE ack.authority_id = v_authority.id
        ) THEN RETURN 'superseded'; END IF;
    ELSIF v_authority.state <> 'current' AND NOT v_is_suspended THEN
        RETURN 'superseded';
    END IF;
    IF v_phase = 'watermark-installed' THEN
        IF v_head.phase = 'takeover-superseded'
           AND v_head.operation_identity = p_record->>'operation_identity' THEN NULL;
        ELSIF v_head.phase IN (
            'admitted', 'mutation-started', 'provider-returned', 'observed'
        ) THEN NULL;
        ELSIF v_head.phase = 'terminal' AND v_head.pending_takeover IS NULL THEN NULL;
        ELSE RETURN 'conflict'; END IF;
    ELSIF v_phase = 'takeover-superseded' THEN
        IF v_head.pending_takeover IS NULL
           OR v_authority.generation <= (v_head.pending_takeover->>'generation')::bigint
           OR (p_record->>'predecessor_generation')::bigint
                <> (v_head.pending_takeover->>'generation')::bigint
           OR (p_record->>'watermark_sequence')::bigint
                <> (v_head.pending_takeover->>'watermark_sequence')::bigint
           OR p_record->>'watermark_digest' <> v_head.pending_takeover->>'watermark_digest'
        THEN RETURN 'conflict'; END IF;
    ELSIF v_phase = 'takeover-acknowledged' THEN
        IF NOT v_pending_matches OR v_head.suspended_operation IS NOT NULL
           OR v_head.phase NOT IN ('watermark-installed', 'terminal')
           OR (p_record->>'watermark_sequence')::bigint
                <> (v_head.pending_takeover->>'watermark_sequence')::bigint
           OR p_record->>'watermark_digest' <> v_head.pending_takeover->>'watermark_digest'
        THEN RETURN 'conflict'; END IF;
    ELSIF v_is_suspended THEN
        IF (v_head.suspended_operation->>'phase' = 'admitted'
            AND NOT (v_phase = 'terminal' AND p_record->>'outcome' = 'never-began'))
           OR (v_head.suspended_operation->>'phase' = 'mutation-started'
               AND v_phase <> 'provider-returned')
           OR (v_head.suspended_operation->>'phase' = 'provider-returned'
               AND v_phase <> 'observed')
           OR (v_head.suspended_operation->>'phase' = 'observed'
               AND v_phase <> 'terminal') THEN RETURN 'conflict'; END IF;
    ELSIF v_head.operation_identity <> p_record->>'operation_identity' OR NOT (
        (v_head.phase = 'takeover-acknowledged' AND v_phase = 'admitted')
        OR (v_head.phase = 'admitted' AND v_phase IN ('mutation-started', 'terminal'))
        OR (v_head.phase = 'mutation-started' AND v_phase = 'provider-returned')
        OR (v_head.phase = 'provider-returned' AND v_phase = 'observed')
        OR (v_head.phase = 'observed' AND v_phase = 'terminal')
    ) THEN RETURN 'conflict'; END IF;
    UPDATE public.external_boot_authority_journal_heads SET
        sequence = v_sequence, digest = v_digest, phase = v_phase,
        authority_id = v_authority.id, generation = v_authority.generation,
        operation_identity = p_record->>'operation_identity', updated_at = clock_timestamp(),
        head_record = p_record - 'canonical_record',
        pending_takeover = CASE
            WHEN v_phase = 'takeover-acknowledged' THEN NULL
            WHEN v_phase IN ('takeover-superseded', 'watermark-installed') THEN jsonb_build_object(
                'authority_id', v_authority.id, 'generation', v_authority.generation,
                'operation_identity', p_record->>'operation_identity',
                'attempt_id', p_record->>'attempt_id', 'request_digest', p_record->>'operation_digest',
                'watermark_sequence', v_sequence, 'watermark_digest', v_digest
            ) ELSE pending_takeover END,
        suspended_operation = CASE
            WHEN v_phase = 'watermark-installed' AND v_head.phase IN (
                'admitted', 'mutation-started', 'provider-returned', 'observed'
            ) THEN jsonb_build_object(
                'authority_id', v_head.authority_id, 'generation', v_head.generation,
                'activation_id', v_head.head_record->>'activation_id',
                'operation_identity', v_head.operation_identity,
                'attempt_id', v_head.head_record->>'attempt_id',
                'purpose', v_head.head_record->>'purpose',
                'operation', v_head.head_record->>'operation',
                'request_digest', v_head.head_record->>'operation_digest',
                'phase', v_head.phase,
                'source_identity', v_head.head_record->>'expected_source_identity',
                'target_identity', v_head.head_record->>'intended_target_identity',
                'ownership_digest', 'sha256:' || encode(sha256(convert_to(
                    public.canonical_external_boot_authority_json(
                        v_head.head_record->'recovery_objects'
                    ), 'UTF8')), 'hex')
            )
            WHEN v_is_suspended AND v_phase = 'terminal' THEN NULL
            WHEN v_is_suspended THEN jsonb_set(
                v_head.suspended_operation, '{phase}', to_jsonb(v_phase)
            )
            ELSE suspended_operation END
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
    public.canonical_external_boot_authority_json(jsonb),
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
