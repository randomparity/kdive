-- Authority-only recovery-object quarantine disposition (#2204).

ALTER TABLE public.external_boot_recovery_orphan_requests
    ADD COLUMN readiness_deadline timestamptz;

UPDATE public.external_boot_recovery_orphan_requests
SET readiness_deadline = created_at
WHERE readiness_deadline IS NULL;

ALTER TABLE public.external_boot_recovery_orphan_requests
    ALTER COLUMN readiness_deadline SET NOT NULL;

REVOKE ALL ON public.external_boot_recovery_quarantine
    FROM kdive_worker;
REVOKE ALL ON public.external_boot_recovery_orphan_requests
    FROM kdive_worker;

CREATE FUNCTION public.resolve_external_boot_recovery_orphan_authority(
    p_peer_incarnation text,
    p_request_id uuid,
    p_job_id uuid,
    p_job_attempt integer
) RETURNS TABLE (
    object_id uuid,
    system_id uuid,
    activation_id uuid,
    run_id uuid,
    disposition text,
    authority_instance text,
    object_kind text,
    object_reference text,
    ownership_digest text,
    observed_digest text,
    operation_identity text,
    attempt_id uuid,
    mutation_journal_sequence bigint,
    mutation_journal_digest text,
    reserved_bytes bigint,
    readiness_deadline timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_request public.external_boot_recovery_orphan_requests%ROWTYPE;
    v_payload jsonb;
    v_expected_status text;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_provider_authority', 'member') THEN
        RAISE EXCEPTION 'provider authority is required' USING ERRCODE = '42501';
    END IF;
    IF p_job_attempt < 1 THEN
        RAISE EXCEPTION 'job attempt is invalid' USING ERRCODE = '22023';
    END IF;

    SELECT request.* INTO v_request
    FROM public.external_boot_recovery_orphan_requests AS request
    JOIN public.jobs AS job ON job.id = request.job_id
    JOIN public.worker_incarnations AS worker ON worker.incarnation = job.worker_id
    WHERE request.id = p_request_id
      AND request.job_id = p_job_id
      AND job.kind = 'resolve_recovery_orphan'
      AND job.state = 'running'
      AND job.attempt = p_job_attempt
      AND job.lease_expires_at > clock_timestamp()
      AND worker.incarnation = p_peer_incarnation
      AND worker.state = 'active'
      AND worker.fence_protocol = 4;
    IF NOT FOUND OR v_request.readiness_deadline <= clock_timestamp() THEN
        RAISE EXCEPTION 'recovery orphan authority is superseded' USING ERRCODE = 'P0001';
    END IF;
    SELECT payload INTO v_payload FROM public.jobs WHERE id = p_job_id;
    IF COALESCE(p_request_id::text = v_payload->>'request_id', false) IS FALSE
       OR COALESCE(v_payload->>'schema' = 'resolve-recovery-orphan-v1', false) IS FALSE
       OR COALESCE(v_payload->>'binding_digest' = v_request.binding_digest, false) IS FALSE
       OR COALESCE((v_payload->'recovery_request_v1'->>'readiness_deadline')::timestamptz
                   = v_request.readiness_deadline, false) IS FALSE THEN
        RAISE EXCEPTION 'recovery orphan request payload is invalid' USING ERRCODE = 'P0001';
    END IF;

    v_expected_status := CASE v_request.disposition
        WHEN 'delete' THEN 'deleted'
        ELSE 'adopted'
    END;
    IF (SELECT count(*) FROM public.external_boot_recovery_quarantine AS q
        WHERE q.id = ANY(v_request.object_ids)
          AND q.system_id = v_request.system_id
          AND q.status IN ('quarantined', v_expected_status)) <> cardinality(v_request.object_ids) THEN
        RAISE EXCEPTION 'recovery orphan selection changed' USING ERRCODE = 'P0001';
    END IF;

    RETURN QUERY
    SELECT q.id, q.system_id, q.activation_id, activation.run_id, v_request.disposition,
           q.authority_instance, q.object_kind, q.object_reference, q.ownership_digest,
           q.observed_digest, q.operation_identity, q.attempt_id, q.mutation_journal_sequence,
           q.mutation_journal_digest, q.reserved_bytes, v_request.readiness_deadline
    FROM public.external_boot_recovery_quarantine AS q
    JOIN public.external_boot_activations AS activation ON activation.id = q.activation_id
    WHERE q.id = ANY(v_request.object_ids)
      AND q.system_id = v_request.system_id
      AND q.status IN ('quarantined', v_expected_status)
    ORDER BY q.object_identity;
END
$$;

CREATE FUNCTION public.commit_external_boot_recovery_orphan_disposition(
    p_peer_incarnation text,
    p_request_id uuid,
    p_job_id uuid,
    p_job_attempt integer,
    p_object_id uuid,
    p_ownership_digest text,
    p_observed_digest text
) RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_request public.external_boot_recovery_orphan_requests%ROWTYPE;
    v_expected_status text;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_provider_authority', 'member') THEN
        RAISE EXCEPTION 'provider authority is required' USING ERRCODE = '42501';
    END IF;
    SELECT request.* INTO v_request
    FROM public.external_boot_recovery_orphan_requests AS request
    JOIN public.jobs AS job ON job.id = request.job_id
    JOIN public.worker_incarnations AS worker ON worker.incarnation = job.worker_id
    WHERE request.id = p_request_id
      AND request.job_id = p_job_id
      AND job.kind = 'resolve_recovery_orphan'
      AND job.state = 'running'
      AND job.attempt = p_job_attempt
      AND job.lease_expires_at > clock_timestamp()
      AND worker.incarnation = p_peer_incarnation
      AND worker.state = 'active'
      AND worker.fence_protocol = 4;
    IF NOT FOUND OR v_request.readiness_deadline <= clock_timestamp() THEN
        RAISE EXCEPTION 'recovery orphan authority is superseded' USING ERRCODE = 'P0001';
    END IF;
    v_expected_status := CASE v_request.disposition
        WHEN 'delete' THEN 'deleted'
        ELSE 'adopted'
    END;
    UPDATE public.external_boot_recovery_quarantine AS q
    SET status = v_expected_status,
        observed_digest = p_observed_digest,
        resolved_at = clock_timestamp(),
        disposition_job_id = p_job_id
    WHERE q.id = p_object_id
      AND q.id = ANY(v_request.object_ids)
      AND q.system_id = v_request.system_id
      AND q.ownership_digest = p_ownership_digest
      AND q.status = 'quarantined';
    IF FOUND THEN
        UPDATE public.external_boot_recovery_orphan_requests AS request
        SET completed_at = clock_timestamp()
        WHERE request.id = p_request_id
          AND request.completed_at IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM public.external_boot_recovery_quarantine AS q
              WHERE q.id = ANY(request.object_ids) AND q.status = 'quarantined'
          );
        RETURN 'applied';
    END IF;
    IF EXISTS (
        SELECT 1 FROM public.external_boot_recovery_quarantine AS q
        WHERE q.id = p_object_id
          AND q.id = ANY(v_request.object_ids)
          AND q.system_id = v_request.system_id
          AND q.ownership_digest = p_ownership_digest
          AND q.status = v_expected_status
          AND q.disposition_job_id = p_job_id
    ) THEN
        RETURN 'applied';
    END IF;
    RAISE EXCEPTION 'recovery orphan disposition is superseded' USING ERRCODE = 'P0001';
END
$$;

REVOKE ALL ON FUNCTION public.resolve_external_boot_recovery_orphan_authority(text, uuid, uuid, integer),
    public.commit_external_boot_recovery_orphan_disposition(text, uuid, uuid, integer, uuid, text, text)
FROM PUBLIC, kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness;
GRANT EXECUTE ON FUNCTION public.resolve_external_boot_recovery_orphan_authority(text, uuid, uuid, integer),
    public.commit_external_boot_recovery_orphan_disposition(text, uuid, uuid, integer, uuid, text, text)
TO kdive_provider_authority;

ALTER TABLE public.external_boot_recovery_orphan_requests
    ADD COLUMN selection_snapshot jsonb;

UPDATE public.external_boot_recovery_orphan_requests
SET selection_snapshot = '[]'::jsonb
WHERE selection_snapshot IS NULL;

ALTER TABLE public.external_boot_recovery_orphan_requests
    ALTER COLUMN selection_snapshot SET NOT NULL,
    ADD CONSTRAINT external_boot_recovery_orphan_selection_snapshot_array
        CHECK (jsonb_typeof(selection_snapshot) = 'array');

CREATE FUNCTION public.verify_external_boot_recovery_orphan_inventory_authority(
    p_peer_incarnation text, p_request_id uuid, p_job_id uuid, p_job_attempt integer
) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path = ''
AS $$
DECLARE
    v_request public.external_boot_recovery_orphan_requests%ROWTYPE;
    v_expected_status text;
    v_count integer;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_provider_authority', 'member') THEN
        RAISE EXCEPTION 'provider authority is required' USING ERRCODE = '42501';
    END IF;
    SELECT request.* INTO v_request
    FROM public.external_boot_recovery_orphan_requests AS request
    JOIN public.jobs AS job ON job.id = request.job_id
    JOIN public.worker_incarnations AS worker ON worker.incarnation = job.worker_id
    WHERE request.id = p_request_id AND request.job_id = p_job_id
      AND job.state = 'running' AND job.attempt = p_job_attempt
      AND job.lease_expires_at > clock_timestamp() AND worker.incarnation = p_peer_incarnation
      AND worker.state = 'active' AND worker.fence_protocol = 4;
    IF NOT FOUND OR v_request.readiness_deadline <= clock_timestamp() THEN
        RAISE EXCEPTION 'recovery orphan inventory is superseded' USING ERRCODE = 'P0001';
    END IF;
    v_expected_status := CASE v_request.disposition WHEN 'delete' THEN 'deleted' ELSE 'adopted' END;
    SELECT count(*) INTO v_count
    FROM jsonb_to_recordset(v_request.selection_snapshot) AS snapshot(
        id uuid, resource_id uuid, activation_id uuid, provider_kind text,
        authority_instance text, object_kind text, object_reference text,
        ownership_digest text, observed_digest text, reserved_bytes text,
        resource_kind text, operation_identity text, attempt_id uuid,
        mutation_journal_sequence text, mutation_journal_digest text
    )
    JOIN public.external_boot_recovery_quarantine AS q ON q.id = snapshot.id
    JOIN public.systems AS system ON system.id = q.system_id
    JOIN public.allocations AS allocation ON allocation.id = system.allocation_id
    JOIN public.resources AS resource ON resource.id = allocation.resource_id
    WHERE q.id = ANY(v_request.object_ids) AND q.system_id = v_request.system_id
      AND q.resource_id = allocation.resource_id AND q.provider_kind = resource.kind
      AND q.resource_id = snapshot.resource_id AND q.activation_id = snapshot.activation_id
      AND q.provider_kind = snapshot.provider_kind AND q.authority_instance = snapshot.authority_instance
      AND q.object_kind = snapshot.object_kind AND q.object_reference = snapshot.object_reference
      AND q.ownership_digest = snapshot.ownership_digest AND q.reserved_bytes::text = snapshot.reserved_bytes
      AND resource.kind = snapshot.resource_kind AND q.operation_identity = snapshot.operation_identity
      AND q.attempt_id = snapshot.attempt_id
      AND q.mutation_journal_sequence::text = snapshot.mutation_journal_sequence
      AND q.mutation_journal_digest = snapshot.mutation_journal_digest
      AND ((q.status = 'quarantined' AND q.observed_digest = snapshot.observed_digest)
           OR (q.status = v_expected_status AND q.disposition_job_id = p_job_id));
    IF jsonb_array_length(v_request.selection_snapshot) <> cardinality(v_request.object_ids)
       OR v_count <> cardinality(v_request.object_ids) THEN
        RAISE EXCEPTION 'recovery orphan inventory changed after admission' USING ERRCODE = 'P0001';
    END IF;
END
$$;

REVOKE ALL ON FUNCTION public.verify_external_boot_recovery_orphan_inventory_authority(text, uuid, uuid, integer)
FROM PUBLIC, kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness;
GRANT EXECUTE ON FUNCTION public.verify_external_boot_recovery_orphan_inventory_authority(text, uuid, uuid, integer)
TO kdive_provider_authority;

CREATE FUNCTION public.publish_external_boot_recovery_quarantine_authority(
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
    WHERE system.id = v_authority.system_id;
    IF v_resource_id IS NULL THEN
        RAISE EXCEPTION 'cleanup quarantine resource is absent' USING ERRCODE = 'P0001';
    END IF;
    WITH objects AS (
        SELECT * FROM jsonb_to_recordset(p_objects) AS item(
            id uuid, object_identity text, object_kind text, object_reference text,
            ownership_digest text, observed_digest text, attempt_id uuid,
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
               objects.observed_digest, p_operation_identity, objects.attempt_id,
               objects.mutation_journal_sequence, objects.mutation_journal_digest, objects.reserved_bytes
        FROM objects ON CONFLICT (object_identity) DO NOTHING RETURNING id
    ) SELECT count(*) INTO v_count FROM inserted;
    IF v_count <> jsonb_array_length(p_objects) AND EXISTS (
        WITH objects AS (
            SELECT * FROM jsonb_to_recordset(p_objects) AS item(
                id uuid, object_identity text, object_kind text, object_reference text,
                ownership_digest text, observed_digest text, attempt_id uuid,
                mutation_journal_sequence bigint, mutation_journal_digest text, reserved_bytes bigint
            )
        ) SELECT 1 FROM objects LEFT JOIN public.external_boot_recovery_quarantine AS q
          ON q.object_identity = objects.object_identity
        WHERE q.id IS DISTINCT FROM objects.id OR q.system_id IS DISTINCT FROM v_authority.system_id
           OR q.activation_id IS DISTINCT FROM v_authority.activation_id
           OR q.provider_kind IS DISTINCT FROM v_authority.provider_kind
           OR q.authority_instance IS DISTINCT FROM v_authority.authority_instance
           OR q.operation_identity IS DISTINCT FROM p_operation_identity
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

REVOKE ALL ON FUNCTION public.publish_external_boot_recovery_quarantine_authority(
    text, uuid, bigint, text, bigint, text, jsonb
) FROM PUBLIC, kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness;
GRANT EXECUTE ON FUNCTION public.publish_external_boot_recovery_quarantine_authority(
    text, uuid, bigint, text, bigint, text, jsonb
) TO kdive_provider_authority;
