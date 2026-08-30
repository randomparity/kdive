-- Bind persisted external-boot recovery points to their activation (ADR-0586, #2108).

DO $$
DECLARE
    ownership_check text;
BEGIN
    SELECT pg_get_constraintdef(oid, true) INTO ownership_check
    FROM pg_constraint
    WHERE conrelid = 'public.external_boot_activations'::regclass
      AND conname = 'external_boot_activation_evidence_ownership'
      AND contype = 'c';

    IF ownership_check IS NULL
       OR ownership_check NOT LIKE '%recovery_point #>> ''{ownership,system_id}''%'
       OR ownership_check NOT LIKE '%recovery_point #>> ''{ownership,run_id}''%'
       OR ownership_check LIKE '%recovery_point #>> ''{binding,%' THEN
        RAISE EXCEPTION '0124 requires the exact migration 0121 ownership CHECK';
    END IF;

    IF EXISTS (
        SELECT 1 FROM public.external_boot_activations
        WHERE recovery_point IS NOT NULL AND (
            recovery_point ? 'ownership'
            OR jsonb_typeof(recovery_point->'binding') IS DISTINCT FROM 'object'
            OR NOT recovery_point->'binding' ?& ARRAY['system_id', 'run_id', 'activation_id']
            OR recovery_point->'binding' <> jsonb_build_object(
                'system_id', recovery_point->'binding'->'system_id',
                'run_id', recovery_point->'binding'->'run_id',
                'activation_id', recovery_point->'binding'->'activation_id'
            )
            OR jsonb_typeof(recovery_point#>'{binding,system_id}') IS DISTINCT FROM 'string'
            OR jsonb_typeof(recovery_point#>'{binding,run_id}') IS DISTINCT FROM 'string'
            OR jsonb_typeof(recovery_point#>'{binding,activation_id}') IS DISTINCT FROM 'string'
            OR NOT (recovery_point#>>'{binding,system_id}' ~
                '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$')
            OR NOT (recovery_point#>>'{binding,run_id}' ~
                '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$')
            OR NOT (recovery_point#>>'{binding,activation_id}' ~
                '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$')
            OR recovery_point#>>'{binding,system_id}' IS DISTINCT FROM system_id::text
            OR recovery_point#>>'{binding,run_id}' IS DISTINCT FROM run_id::text
            OR recovery_point#>>'{binding,activation_id}' IS DISTINCT FROM id::text
        )
    ) THEN
        RAISE EXCEPTION '0124 found an incompatible persisted recovery point';
    END IF;
END
$$;

ALTER TABLE public.external_boot_activations
    DROP CONSTRAINT external_boot_activation_evidence_ownership;

ALTER TABLE public.external_boot_activations
    ADD CONSTRAINT external_boot_activation_evidence_ownership CHECK (
        (materialization IS NULL OR (
            materialization #>> '{ownership,system_id}' IS NOT DISTINCT FROM system_id::text
            AND materialization #>> '{ownership,run_id}' IS NOT DISTINCT FROM run_id::text
            AND materialization ->> 'plan_identity' IS NOT DISTINCT FROM plan_identity))
        AND (recovery_point IS NULL OR (
            NOT recovery_point ? 'ownership'
            AND jsonb_typeof(recovery_point->'binding') IS NOT DISTINCT FROM 'object'
            AND recovery_point->'binding' ?& ARRAY['system_id', 'run_id', 'activation_id']
            AND recovery_point->'binding' = jsonb_build_object(
                'system_id', recovery_point->'binding'->'system_id',
                'run_id', recovery_point->'binding'->'run_id',
                'activation_id', recovery_point->'binding'->'activation_id'
            )
            AND jsonb_typeof(recovery_point#>'{binding,system_id}') IS NOT DISTINCT FROM 'string'
            AND jsonb_typeof(recovery_point#>'{binding,run_id}') IS NOT DISTINCT FROM 'string'
            AND jsonb_typeof(recovery_point#>'{binding,activation_id}') IS NOT DISTINCT FROM 'string'
            AND (recovery_point#>>'{binding,system_id}')::uuid = system_id
            AND (recovery_point#>>'{binding,run_id}')::uuid = run_id
            AND (recovery_point#>>'{binding,activation_id}')::uuid = id
            AND recovery_point ->> 'plan_identity' IS NOT DISTINCT FROM plan_identity))
        AND (pre_recovery_evidence IS NULL OR (
            pre_recovery_evidence ->> 'activation_id' IS NOT DISTINCT FROM id::text
            AND pre_recovery_evidence ->> 'system_id' IS NOT DISTINCT FROM system_id::text
            AND pre_recovery_evidence ->> 'run_id' IS NOT DISTINCT FROM run_id::text
            AND pre_recovery_evidence ->> 'plan_identity' IS NOT DISTINCT FROM plan_identity))
        AND (terminal_evidence IS NULL OR (
            terminal_evidence ->> 'activation_id' IS NOT DISTINCT FROM id::text
            AND terminal_evidence ->> 'system_id' IS NOT DISTINCT FROM system_id::text))
        AND (teardown_evidence IS NULL OR teardown_evidence ->> 'system_id'
            IS NOT DISTINCT FROM system_id::text)
        AND (cleanup_evidence IS NULL OR (
            cleanup_evidence ->> 'activation_id' IS NOT DISTINCT FROM id::text
            AND cleanup_evidence ->> 'system_id' IS NOT DISTINCT FROM system_id::text))
    );
