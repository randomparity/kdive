-- 0136_external_boot_conflict_observation.sql — ADR-0610 exact conflict observation.

ALTER TABLE public.external_boot_recovery_attempts
    ADD COLUMN observed_composite_state text
        CONSTRAINT external_boot_attempt_observed_digest
        CHECK (observed_composite_state IS NULL OR observed_composite_state ~ '^sha256:[0-9a-f]{64}$');

DO $$
DECLARE
    v_function constant regprocedure :=
        'public.commit_external_boot_authority_result(bytea,uuid,integer,uuid,bigint,uuid,uuid,uuid,text,text,text,text,text,text,bigint,text,text,jsonb)'::regprocedure;
    v_definition text := pg_get_functiondef(v_function);
    v_old text := 'activation_id, attempt_number, attempt_id, authority_generation,' || chr(10) ||
                  '                recovery_basis, recovery_readiness_deadline, state' || chr(10) ||
                  '            ) VALUES (';
    v_new text := 'activation_id, attempt_number, attempt_id, authority_generation,' || chr(10) ||
                  '                recovery_basis, recovery_readiness_deadline, observed_composite_state, state' || chr(10) ||
                  '            ) VALUES (';
BEGIN
    v_old := 'WHERE field <> ALL (ARRAY[' || chr(10) ||
             '               ''schema'', ''operation'', ''attempt_id'', ''recovery_basis'', ''deadline''' || chr(10) ||
             '           ])';
    v_new := 'WHERE field <> ALL (ARRAY[' || chr(10) ||
             '               ''schema'', ''operation'', ''attempt_id'', ''recovery_basis'', ''deadline'',' || chr(10) ||
             '               ''observed_composite_state''' || chr(10) ||
             '           ])';
    IF position(v_old in v_definition) = 0 THEN
        RAISE EXCEPTION 'external boot recovery attempt result shape changed';
    END IF;
    v_definition := replace(v_definition, v_old, v_new);
    v_old := 'activation_id, attempt_number, attempt_id, authority_generation,' || chr(10) ||
             '                recovery_basis, recovery_readiness_deadline, state' || chr(10) ||
             '            ) VALUES (';
    v_new := 'activation_id, attempt_number, attempt_id, authority_generation,' || chr(10) ||
             '                recovery_basis, recovery_readiness_deadline, observed_composite_state, state' || chr(10) ||
             '            ) VALUES (';
    IF position(v_old in v_definition) = 0 THEN
        RAISE EXCEPTION 'external boot recovery attempt insert shape changed';
    END IF;
    v_definition := replace(v_definition, v_old, v_new);
    v_old := '                p_result ->> ''recovery_basis'',' || chr(10) ||
             '                v_deadline,' || chr(10) ||
             '                ''recovering''';
    v_new := '                p_result ->> ''recovery_basis'',' || chr(10) ||
             '                v_deadline,' || chr(10) ||
             '                CASE WHEN p_purpose = ''resolve-conflict'' THEN p_result ->> ''observed_composite_state'' END,' || chr(10) ||
             '                ''recovering''';
    IF position(v_old in v_definition) = 0 THEN
        RAISE EXCEPTION 'external boot recovery attempt values shape changed';
    END IF;
    v_definition := replace(v_definition, v_old, v_new);
    v_old := '                p_authority_id, p_generation, ''recovery_point'',' || chr(10) ||
             '                (p_result ->> ''recovery_readiness_deadline'')::timestamptz, ''recovering''';
    v_new := '                p_authority_id, p_generation, ''recovery_point'',' || chr(10) ||
             '                (p_result ->> ''recovery_readiness_deadline'')::timestamptz, NULL, ''recovering''';
    IF position(v_old in v_definition) = 0 THEN
        RAISE EXCEPTION 'external boot failure recovery attempt values shape changed';
    END IF;
    v_definition := replace(v_definition, v_old, v_new);
    EXECUTE v_definition;
END
$$;

CREATE FUNCTION public.resolve_external_boot_conflict_dispatch_binding(
    p_activation_id uuid,
    p_system_id uuid,
    p_run_id uuid,
    p_plan_identity text
) RETURNS TABLE (provider_kind text, authority_instance text)
LANGUAGE sql
SECURITY DEFINER
SET search_path = ''
STABLE
AS $$
    SELECT authority.provider_kind, authority.authority_instance
    FROM public.external_boot_activations AS activation
    JOIN public.external_boot_authorities AS authority
      ON authority.activation_id = activation.id
     AND authority.system_id = activation.system_id
     AND authority.run_id = activation.run_id
     AND authority.plan_identity = activation.plan_identity
    WHERE activation.id = p_activation_id
      AND activation.system_id = p_system_id
      AND activation.run_id = p_run_id
      AND activation.plan_identity = p_plan_identity
      AND activation.state = 'recovery_conflict'
      AND authority.state IN ('current', 'retired')
    ORDER BY authority.generation DESC
    LIMIT 1
$$;

REVOKE ALL ON FUNCTION public.resolve_external_boot_conflict_dispatch_binding(
    uuid, uuid, uuid, text
) FROM PUBLIC, kdive_worker, kdive_reconciler, kdive_lifecycle_witness,
    kdive_provider_authority;
GRANT EXECUTE ON FUNCTION public.resolve_external_boot_conflict_dispatch_binding(
    uuid, uuid, uuid, text
) TO kdive_server;
