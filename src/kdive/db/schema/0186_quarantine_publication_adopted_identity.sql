-- 0186_quarantine_publication_adopted_identity.sql — a quarantine row names the operation its
-- provider receipt names (ADR-0787 amendment 2026-10-30, #3434).
--
-- A retried release generation adopts the earlier generation's terminal cleanup and runs no
-- commit, so the provider receipt keeps that cleanup's operation identity. 0144 stored the
-- publishing authority's own identity on the row and refused an existing row under any other,
-- so an aborted retry could neither publish nor confirm the receipt, and a row it did publish
-- could not be resolved: the resolver rebuilds the provider binding from the row's identity.
-- This replaces the function with the same signature, grants and fences. Each object may carry
-- its own operation identity, stored on the row and compared against an existing one. A
-- foreign identity is admitted only for a release authority, and only when it is the 0143
-- cleanup derivation of an earlier acknowledged release authority of the same system,
-- activation, run, plan, provider kind and authority instance. An object without the key keeps
-- 0144's behaviour, so a build older than this migration publishes exactly as before.

CREATE OR REPLACE FUNCTION public.publish_external_boot_recovery_quarantine_authority(
    p_peer_incarnation text, p_authority_id uuid, p_generation bigint, p_operation_identity text,
    p_terminal_sequence bigint, p_terminal_digest text, p_objects jsonb
) RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
AS $$
DECLARE
    v_authority public.external_boot_authorities%ROWTYPE;
    v_resource_id uuid;
    v_count integer;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_provider_authority', 'member') THEN
        RAISE EXCEPTION 'provider authority is required' USING ERRCODE = '42501';
    END IF;
    IF jsonb_typeof(p_objects) <> 'array' OR jsonb_array_length(p_objects) NOT BETWEEN 1 AND 64 THEN
        RAISE EXCEPTION 'quarantine inventory is outside its bound' USING ERRCODE = '22023';
    END IF;
    SELECT authority.* INTO v_authority
    FROM public.external_boot_authorities AS authority
    JOIN public.jobs AS job ON job.id = authority.job_id
    JOIN public.worker_incarnations AS worker ON worker.incarnation = job.worker_id
    JOIN public.external_boot_authority_journal_heads AS head
      ON head.system_id = authority.system_id
     AND head.authority_instance = authority.authority_instance
    LEFT JOIN LATERAL public.derive_external_boot_release_phase_binding(
        jsonb_build_object(
            'authority_id', authority.id, 'generation', authority.generation,
            'system_id', authority.system_id, 'activation_id', authority.activation_id,
            'run_id', authority.run_id, 'plan_identity', authority.plan_identity,
            'provider_kind', authority.provider_kind,
            'authority_instance', authority.authority_instance,
            'worker_incarnation', authority.worker_incarnation,
            'root_operation_identity', authority.operation_identity,
            'root_operation_digest', authority.operation_digest
        ), 'cleanup'
    ) AS derived ON authority.purpose = 'release' AND authority.operation = 'release'
    WHERE authority.id = p_authority_id AND authority.generation = p_generation
      AND (authority.operation_identity = p_operation_identity
           OR derived.operation_identity = p_operation_identity)
      AND authority.state = 'current'
      AND (
          authority.operation IN ('cleanup', 'teardown')
          OR (authority.purpose = 'release' AND authority.operation = 'release'
              AND derived.operation_identity = p_operation_identity)
      )
      AND authority.worker_incarnation = p_peer_incarnation AND job.state = 'running'
      AND job.attempt = authority.job_attempt AND job.lease_expires_at > clock_timestamp()
      AND worker.incarnation = p_peer_incarnation AND worker.state = 'active' AND worker.fence_protocol = 4
      AND head.authority_id = p_authority_id AND head.generation = p_generation
      AND head.operation_identity = p_operation_identity AND head.sequence = p_terminal_sequence
      AND head.digest = p_terminal_digest AND head.phase = 'terminal';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'cleanup quarantine publication is superseded' USING ERRCODE = 'P0001';
    END IF;
    SELECT allocation.resource_id INTO v_resource_id
    FROM public.systems AS system
    JOIN public.allocations AS allocation ON allocation.id = system.allocation_id
    JOIN public.resources AS resource ON resource.id = allocation.resource_id
    WHERE system.id = v_authority.system_id AND resource.kind = v_authority.provider_kind;
    IF v_resource_id IS NULL THEN
        RAISE EXCEPTION 'cleanup quarantine resource is absent' USING ERRCODE = 'P0001';
    END IF;
    -- An object names its own operation: the current authority's, or, for a release, the
    -- cleanup an earlier acknowledged release authority of this activation derived (#3434).
    -- An absent identity is the current authority's, as under 0144.
    IF EXISTS (
        SELECT 1 FROM jsonb_to_recordset(p_objects) AS item(operation_identity text)
        WHERE COALESCE(item.operation_identity, p_operation_identity) <> p_operation_identity
          AND NOT (
              v_authority.purpose = 'release' AND v_authority.operation = 'release'
              AND EXISTS (
                  SELECT 1
                  FROM public.external_boot_authorities AS earlier
                  CROSS JOIN LATERAL public.derive_external_boot_release_phase_binding(
                      jsonb_build_object(
                          'authority_id', earlier.id, 'generation', earlier.generation,
                          'system_id', earlier.system_id, 'activation_id', earlier.activation_id,
                          'run_id', earlier.run_id, 'plan_identity', earlier.plan_identity,
                          'provider_kind', earlier.provider_kind,
                          'authority_instance', earlier.authority_instance,
                          'worker_incarnation', earlier.worker_incarnation,
                          'root_operation_identity', earlier.operation_identity,
                          'root_operation_digest', earlier.operation_digest
                      ), 'cleanup'
                  ) AS derived
                  WHERE earlier.system_id = v_authority.system_id
                    AND earlier.activation_id = v_authority.activation_id
                    AND earlier.run_id = v_authority.run_id
                    AND earlier.plan_identity = v_authority.plan_identity
                    AND earlier.provider_kind = v_authority.provider_kind
                    AND earlier.authority_instance = v_authority.authority_instance
                    AND earlier.purpose = 'release' AND earlier.operation = 'release'
                    AND earlier.generation < v_authority.generation
                    AND earlier.acknowledged_at IS NOT NULL
                    AND derived.operation_identity = item.operation_identity
              )
          )
    ) THEN
        RAISE EXCEPTION 'cleanup quarantine object operation is not admissible'
            USING ERRCODE = 'P0001';
    END IF;
    WITH objects AS (
        SELECT * FROM jsonb_to_recordset(p_objects) AS item(
            id uuid, object_identity text, object_kind text, object_reference text,
            ownership_digest text, observed_digest text, operation_identity text, attempt_id uuid,
            mutation_journal_sequence bigint, mutation_journal_digest text, reserved_bytes bigint
        )
    ), inserted AS (
        INSERT INTO public.external_boot_recovery_quarantine (
            id, object_identity, resource_id, system_id, activation_id, provider_kind,
            authority_instance, object_kind, object_reference, ownership_digest, observed_digest,
            operation_identity, attempt_id, mutation_journal_sequence, mutation_journal_digest, reserved_bytes
        )
        SELECT objects.id, objects.object_identity, v_resource_id, v_authority.system_id,
               v_authority.activation_id, v_authority.provider_kind, v_authority.authority_instance,
               objects.object_kind, objects.object_reference, objects.ownership_digest,
               objects.observed_digest,
               COALESCE(objects.operation_identity, p_operation_identity), objects.attempt_id,
               objects.mutation_journal_sequence, objects.mutation_journal_digest, objects.reserved_bytes
        FROM objects ON CONFLICT (object_identity) DO NOTHING RETURNING id
    ) SELECT count(*) INTO v_count FROM inserted;
    IF v_count <> jsonb_array_length(p_objects) AND EXISTS (
        WITH objects AS (
            SELECT * FROM jsonb_to_recordset(p_objects) AS item(
                id uuid, object_identity text, object_kind text, object_reference text,
                ownership_digest text, observed_digest text, operation_identity text, attempt_id uuid,
                mutation_journal_sequence bigint, mutation_journal_digest text, reserved_bytes bigint
            )
        ) SELECT 1 FROM objects LEFT JOIN public.external_boot_recovery_quarantine AS q
          ON q.object_identity = objects.object_identity
        WHERE q.id IS DISTINCT FROM objects.id OR q.system_id IS DISTINCT FROM v_authority.system_id
           OR q.activation_id IS DISTINCT FROM v_authority.activation_id
           OR q.provider_kind IS DISTINCT FROM v_authority.provider_kind
           OR q.authority_instance IS DISTINCT FROM v_authority.authority_instance
           OR q.operation_identity
              IS DISTINCT FROM COALESCE(objects.operation_identity, p_operation_identity)
           OR q.ownership_digest IS DISTINCT FROM objects.ownership_digest
           OR q.observed_digest IS DISTINCT FROM objects.observed_digest
           OR q.object_kind IS DISTINCT FROM objects.object_kind
           OR q.object_reference IS DISTINCT FROM objects.object_reference
           OR q.attempt_id IS DISTINCT FROM objects.attempt_id
           OR q.reserved_bytes IS DISTINCT FROM objects.reserved_bytes
           OR q.resource_id IS DISTINCT FROM v_resource_id
           OR q.mutation_journal_sequence IS DISTINCT FROM objects.mutation_journal_sequence
           OR q.mutation_journal_digest IS DISTINCT FROM objects.mutation_journal_digest
    ) THEN
        RAISE EXCEPTION 'cleanup quarantine inventory conflicts with existing evidence' USING ERRCODE = 'P0001';
    END IF;
    RETURN jsonb_array_length(p_objects);
END
$$;
