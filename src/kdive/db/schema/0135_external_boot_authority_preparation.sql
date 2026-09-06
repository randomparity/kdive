-- 0135_external_boot_authority_preparation.sql — ADR-0608 authority-owned preparation.

CREATE FUNCTION public.derive_external_boot_preparation_binding(
    p_root jsonb, p_operation text
) RETURNS TABLE (operation_identity text, operation_digest text)
LANGUAGE plpgsql IMMUTABLE STRICT SET search_path = '' AS $$
DECLARE
    v_canonical text;
BEGIN
    IF jsonb_typeof(p_root) IS DISTINCT FROM 'object'
       OR p_operation NOT IN ('materialize', 'prepare')
       OR p_root <> jsonb_build_object(
           'authority_id', p_root->'authority_id',
           'generation', p_root->'generation',
           'system_id', p_root->'system_id',
           'activation_id', p_root->'activation_id',
           'run_id', p_root->'run_id',
           'plan_identity', p_root->'plan_identity',
           'purpose', p_root->'purpose',
           'provider_kind', p_root->'provider_kind',
           'authority_instance', p_root->'authority_instance',
           'worker_incarnation', p_root->'worker_incarnation',
           'root_operation', p_root->'root_operation',
           'root_operation_identity', p_root->'root_operation_identity',
           'root_operation_digest', p_root->'root_operation_digest'
       )
       OR p_root->>'purpose' <> 'activate'
       OR p_root->>'root_operation' <> 'activate'
       OR p_root->>'plan_identity' !~ '^sha256:[0-9a-f]{64}$'
       OR p_root->>'root_operation_digest' !~ '^sha256:[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'external boot preparation root binding is invalid'
            USING ERRCODE = '22023';
    END IF;
    v_canonical := public.canonical_external_boot_authority_json(
        p_root || jsonb_build_object('operation', p_operation)
    );
    operation_identity := 'sha256:' || encode(sha256(
        convert_to('kdive-external-boot-preparation-identity-v1', 'UTF8') ||
        decode('00', 'hex') || convert_to(v_canonical, 'UTF8')
    ), 'hex');
    operation_digest := 'sha256:' || encode(sha256(
        convert_to('kdive-external-boot-preparation-digest-v1', 'UTF8') ||
        decode('00', 'hex') || convert_to(v_canonical, 'UTF8')
    ), 'hex');
    RETURN NEXT;
END
$$;

CREATE FUNCTION public.resolve_current_external_boot_preparation_authority(
    p_peer_incarnation text, p_authority_id uuid, p_generation bigint,
    p_ack_sequence bigint, p_ack_digest text, p_operation text
) RETURNS TABLE (
    peer_incarnation_id text, authority_id uuid, generation bigint, system_id uuid,
    activation_id uuid, run_id uuid, plan_identity text, purpose text, operation text,
    provider_kind text, authority_instance text, operation_identity text,
    operation_digest text, state text, preparation_plan jsonb
) LANGUAGE sql SECURITY DEFINER SET search_path = '' STABLE AS $$
    SELECT a.worker_incarnation, a.id, a.generation, a.system_id, a.activation_id, a.run_id,
           a.plan_identity, a.purpose, p_operation, a.provider_kind, a.authority_instance,
           derived.operation_identity, derived.operation_digest, a.state,
           j.payload->'external_boot_plan_v1'
    FROM public.external_boot_authorities AS a
    JOIN public.worker_incarnations AS w ON w.incarnation = a.worker_incarnation
    JOIN public.external_boot_authority_acknowledgements AS ack ON ack.authority_id = a.id
    JOIN public.jobs AS j ON j.id = a.job_id AND j.attempt = a.job_attempt
    CROSS JOIN LATERAL public.derive_external_boot_preparation_binding(
        jsonb_build_object(
            'authority_id', a.id, 'generation', a.generation, 'system_id', a.system_id,
            'activation_id', a.activation_id, 'run_id', a.run_id,
            'plan_identity', a.plan_identity, 'purpose', a.purpose,
            'provider_kind', a.provider_kind, 'authority_instance', a.authority_instance,
            'worker_incarnation', a.worker_incarnation, 'root_operation', a.operation,
            'root_operation_identity', a.operation_identity,
            'root_operation_digest', a.operation_digest
        ), p_operation
    ) AS derived
    WHERE pg_has_role(session_user, 'kdive_provider_authority', 'member')
      AND p_operation IN ('materialize', 'prepare')
      AND w.incarnation = p_peer_incarnation AND w.state = 'active' AND w.fence_protocol = 4
      AND a.id = p_authority_id AND a.generation = p_generation AND a.state = 'current'
      AND a.purpose = 'activate' AND a.operation = 'activate'
      AND ack.journal_sequence = p_ack_sequence AND ack.journal_digest = p_ack_digest
      AND j.state = 'running' AND j.worker_id = p_peer_incarnation
      AND j.lease_expires_at > clock_timestamp()
      AND jsonb_typeof(j.payload->'external_boot_plan_v1') = 'object'
      AND j.payload #>> '{external_boot_plan_v1,ownership,system_id}' = a.system_id::text
      AND j.payload #>> '{external_boot_plan_v1,ownership,run_id}' = a.run_id::text
      AND 'sha256:' || encode(sha256(
          convert_to('kdive-external-boot-plan-v1', 'UTF8') || decode('00', 'hex') ||
          convert_to(public.canonical_external_boot_authority_json(
              j.payload->'external_boot_plan_v1'
          ), 'UTF8')
      ), 'hex') = a.plan_identity
$$;

-- A preparing activation is now admitted to the same activate authority that will finish it.
DO $$
DECLARE
    v_definition text;
    v_old text := E'v_system.state <> ''ready'' OR v_run.state <> ''succeeded''\n' ||
                  E'           OR v_activation.state NOT IN (''prepared'', ''activating'')';
    v_new text := E'v_system.state <> ''ready'' OR v_run.state <> ''succeeded''\n' ||
                  E'           OR v_activation.state NOT IN ' ||
                  E'(''preparing'', ''prepared'', ''activating'')';
BEGIN
    SELECT pg_get_functiondef(
        'public.allocate_external_boot_authority(bytea,uuid,integer,uuid,uuid,uuid,text,text,text,text,text)'::regprocedure
    ) INTO v_definition;
    IF v_definition NOT LIKE '%' || v_old || '%' THEN
        RAISE EXCEPTION 'external boot allocation source shape changed';
    END IF;
    EXECUTE replace(v_definition, v_old, v_new);
END
$$;

-- Keep one journal head while allowing only the two preparation successor operations.
DO $$
DECLARE
    v_definition text;
BEGIN
    SELECT pg_get_functiondef(
        'public.advance_external_boot_authority_journal_head(text,uuid,bigint,bigint,text,jsonb)'::regprocedure
    ) INTO v_definition;
    IF v_definition NOT LIKE '%v_inherited_completion boolean := false;%' THEN
        RAISE EXCEPTION 'external boot journal declaration shape changed';
    END IF;
    v_definition := replace(
        v_definition,
        'v_inherited_completion boolean := false;',
        E'v_inherited_completion boolean := false;\n' ||
        E'    v_bound_operation text;\n    v_bound_identity text;\n    v_bound_digest text;'
    );
    v_definition := replace(
        v_definition,
        E'''activate'', ''recover'', ''resolve-conflict'', ''release'', ''cleanup'', ''teardown'',\n' ||
        E'           ''deadline'', ''recovery-attempt'', ''fail''',
        E'''materialize'', ''prepare'', ''activate'', ''recover'', ''resolve-conflict'', ' ||
        E'''release'', ''cleanup'', ''teardown'',\n' ||
        E'           ''deadline'', ''recovery-attempt'', ''fail'''
    );
    v_definition := replace(
        v_definition,
        E'WHEN ''activate'' THEN (p_record->>''operation'') = ' ||
        E'ANY (ARRAY[''activate'', ''deadline'', ''fail''])',
        E'WHEN ''activate'' THEN (p_record->>''operation'') = ' ||
        E'ANY (ARRAY[''materialize'', ''prepare'', ''activate'', ''deadline'', ''fail''])'
    );
    IF v_definition NOT LIKE '%IF p_record->>''operation'' IS DISTINCT FROM v_authority.operation%'
    THEN RAISE EXCEPTION 'external boot journal operation gate shape changed'; END IF;
    v_definition := replace(
        v_definition,
        E'IF p_record->>''operation'' IS DISTINCT FROM v_authority.operation\n' ||
        E'       AND NOT v_inherited_completion',
        E'v_bound_operation := v_authority.operation;\n' ||
        E'    v_bound_identity := v_authority.operation_identity;\n' ||
        E'    v_bound_digest := v_authority.operation_digest;\n' ||
        E'    IF p_record->>''operation'' IN (''materialize'', ''prepare'')\n' ||
        E'       AND v_authority.purpose = ''activate'' AND v_authority.operation = ''activate'' THEN\n' ||
        E'        SELECT operation_identity, operation_digest\n' ||
        E'        INTO v_bound_identity, v_bound_digest\n' ||
        E'        FROM public.derive_external_boot_preparation_binding(jsonb_build_object(\n' ||
        E'            ''authority_id'', v_authority.id, ''generation'', v_authority.generation,\n' ||
        E'            ''system_id'', v_authority.system_id, ''activation_id'', v_authority.activation_id,\n' ||
        E'            ''run_id'', v_authority.run_id, ''plan_identity'', v_authority.plan_identity,\n' ||
        E'            ''purpose'', v_authority.purpose, ''provider_kind'', v_authority.provider_kind,\n' ||
        E'            ''authority_instance'', v_authority.authority_instance,\n' ||
        E'            ''worker_incarnation'', v_authority.worker_incarnation,\n' ||
        E'            ''root_operation'', v_authority.operation,\n' ||
        E'            ''root_operation_identity'', v_authority.operation_identity,\n' ||
        E'            ''root_operation_digest'', v_authority.operation_digest\n' ||
        E'        ), p_record->>''operation'');\n' ||
        E'        v_bound_operation := p_record->>''operation'';\n' ||
        E'    END IF;\n' ||
        E'    IF p_record->>''operation'' IS DISTINCT FROM v_bound_operation\n' ||
        E'       AND NOT v_inherited_completion'
    );
    v_definition := replace(
        v_definition,
        E'OR p_record->>''operation_identity'' IS DISTINCT FROM v_authority.operation_identity\n' ||
        E'       OR p_record->>''operation_digest'' IS DISTINCT FROM v_authority.operation_digest',
        E'OR p_record->>''operation_identity'' IS DISTINCT FROM v_bound_identity\n' ||
        E'       OR p_record->>''operation_digest'' IS DISTINCT FROM v_bound_digest'
    );
    v_definition := replace(
        v_definition,
        E'ELSIF v_head.operation_identity <> p_record->>''operation_identity'' OR NOT (\n' ||
        E'        (v_head.phase = ''takeover-acknowledged'' AND v_phase = ''admitted'')',
        E'ELSIF v_head.operation_identity <> p_record->>''operation_identity'' AND NOT (\n' ||
        E'        v_phase = ''admitted'' AND (\n' ||
        E'            (v_head.phase = ''takeover-acknowledged''\n' ||
        E'             AND p_record->>''operation'' = ''materialize'')\n' ||
        E'            OR (v_head.phase = ''terminal''\n' ||
        E'                AND v_head.head_record->>''operation'' = ''materialize''\n' ||
        E'                AND p_record->>''operation'' = ''prepare'')\n' ||
        E'            OR (v_head.phase = ''terminal''\n' ||
        E'                AND v_head.head_record->>''operation'' = ''prepare''\n' ||
        E'                AND p_record->>''operation'' = ''activate'')\n' ||
        E'        )\n' ||
        E'    ) OR NOT (\n' ||
        E'        (v_head.phase = ''takeover-acknowledged'' AND v_phase = ''admitted'')'
    );
    EXECUTE v_definition;
END
$$;

REVOKE ALL ON FUNCTION
    public.derive_external_boot_preparation_binding(jsonb, text),
    public.resolve_current_external_boot_preparation_authority(
        text, uuid, bigint, bigint, text, text
    )
FROM PUBLIC, kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness,
    kdive_provider_authority;

GRANT EXECUTE ON FUNCTION public.resolve_current_external_boot_preparation_authority(
    text, uuid, bigint, bigint, text, text
) TO kdive_provider_authority;
