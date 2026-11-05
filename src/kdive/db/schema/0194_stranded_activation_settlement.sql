-- 0194_stranded_activation_settlement.sql — #3550, ADR-0816 (2026-11-04).
-- Forward-only (ADR-0015). Ends a stranded fixed-worker `preparing` activation on independent
-- evidence from both owners. Each owner records only its own daemon's fact through its own role.
CREATE TABLE public.external_boot_activation_settlements (
    activation_id uuid PRIMARY KEY
        REFERENCES public.external_boot_activations (id) ON DELETE RESTRICT,
    system_id uuid NOT NULL REFERENCES public.systems (id) ON DELETE RESTRICT,
    authority_instance text,
    authority_domain_name text,
    authority_journal_sequence bigint,
    authority_journal_digest text,
    authority_composite_state text,
    authority_absent_objects text[],
    authority_recorded_at timestamptz,
    worker_incarnation text,
    worker_job_id uuid,
    worker_job_attempt integer,
    worker_domain_name text,
    worker_recorded_at timestamptz,
    principal text,
    reason text,
    settled_at timestamptz,
    CHECK ((authority_recorded_at IS NULL) = (authority_composite_state IS NULL)),
    CHECK ((authority_recorded_at IS NULL) = (authority_absent_objects IS NULL)),
    CHECK ((worker_recorded_at IS NULL) = (worker_domain_name IS NULL)),
    CHECK (settled_at IS NULL OR (
        authority_recorded_at IS NOT NULL AND worker_recorded_at IS NOT NULL
        AND principal IS NOT NULL
        AND reason IS NOT NULL AND octet_length(btrim(reason)) BETWEEN 1 AND 1024
    ))
);

CREATE FUNCTION public.record_stranded_activation_authority_absence(
    p_activation_id uuid, p_authority_instance text, p_domain_name text,
    p_composite_state text, p_absent_objects text[]
) RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_activation public.external_boot_activations%ROWTYPE;
    v_head public.external_boot_authority_journal_heads%ROWTYPE;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_provider_authority', 'member') THEN
        RAISE EXCEPTION 'provider authority is required' USING ERRCODE = '42501';
    END IF;
    IF p_composite_state IS NULL OR p_composite_state !~ '^sha256:[0-9a-f]{64}$'
       OR p_absent_objects IS NULL OR cardinality(p_absent_objects) > 32
       OR array_position(p_absent_objects, NULL) IS NOT NULL
       OR EXISTS (SELECT 1 FROM unnest(p_absent_objects) AS ref
                  WHERE octet_length(ref) NOT BETWEEN 1 AND 1024
                     OR ref !~ '^[\x20-\x7e]+$' OR ref ~ '["\\:@]'
                     OR ref ~ '(^|/)\.{0,2}(/|$)') THEN
        RAISE EXCEPTION 'authority absence record is invalid' USING ERRCODE = '22023';
    END IF;
    SELECT * INTO v_activation FROM public.external_boot_activations WHERE id = p_activation_id;
    IF NOT FOUND THEN RETURN 'not_found'; END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'kdive:system:' || v_activation.system_id::text, 2125));
    SELECT * INTO v_activation FROM public.external_boot_activations
    WHERE id = p_activation_id FOR UPDATE;
    IF v_activation.state <> 'preparing' OR NOT (
        EXISTS (SELECT 1 FROM public.external_boot_authorities AS authority
                WHERE authority.activation_id = p_activation_id
                  AND authority.authority_instance = p_authority_instance)
        OR EXISTS (SELECT 1 FROM public.jobs AS job
                   WHERE job.kind = 'boot'
                     AND job.payload #>> '{external_boot_authority_v1,activation_id}'
                         = p_activation_id::text
                     AND job.payload #>> '{external_boot_authority_v1,authority_instance}'
                         = p_authority_instance)
    ) THEN RETURN 'superseded'; END IF;
    IF NOT EXISTS (SELECT 1 FROM public.systems AS system
                   WHERE system.id = v_activation.system_id
                     AND system.domain_name = p_domain_name) THEN
        RETURN 'superseded';
    END IF;
    SELECT * INTO v_head FROM public.external_boot_authority_journal_heads
    WHERE authority_instance = p_authority_instance AND system_id = v_activation.system_id;
    INSERT INTO public.external_boot_activation_settlements AS settlement (
        activation_id, system_id, authority_instance, authority_domain_name,
        authority_journal_sequence,
        authority_journal_digest, authority_composite_state, authority_absent_objects,
        authority_recorded_at
    ) VALUES (
        p_activation_id, v_activation.system_id, p_authority_instance, p_domain_name,
        v_head.sequence,
        v_head.digest, p_composite_state,
        ARRAY(SELECT DISTINCT ref FROM unnest(p_absent_objects) AS ref),
        clock_timestamp()
    ) ON CONFLICT (activation_id) DO UPDATE SET
        authority_instance = excluded.authority_instance,
        authority_domain_name = excluded.authority_domain_name,
        authority_journal_sequence = excluded.authority_journal_sequence,
        authority_journal_digest = excluded.authority_journal_digest,
        authority_composite_state = excluded.authority_composite_state,
        authority_absent_objects = excluded.authority_absent_objects,
        authority_recorded_at = excluded.authority_recorded_at
    WHERE settlement.settled_at IS NULL;
    RETURN 'applied';
END $$;

CREATE FUNCTION public.record_stranded_activation_worker_ownership(
    p_credential_hash bytea, p_job_id uuid, p_job_attempt integer, p_activation_id uuid,
    p_domain_name text
) RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_activation public.external_boot_activations%ROWTYPE;
    v_system public.systems%ROWTYPE;
    v_job public.jobs%ROWTYPE;
    v_incarnation text;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_worker', 'member') THEN
        RAISE EXCEPTION 'worker authority is required' USING ERRCODE = '42501';
    END IF;
    SELECT incarnation INTO v_incarnation FROM public.worker_incarnations
    WHERE credential_hash = p_credential_hash AND state = 'active' AND fence_protocol = 4;
    IF v_incarnation IS NULL THEN RETURN 'superseded'; END IF;
    SELECT * INTO v_activation FROM public.external_boot_activations WHERE id = p_activation_id;
    IF NOT FOUND THEN RETURN 'not_found'; END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'kdive:system:' || v_activation.system_id::text, 2125));
    SELECT * INTO v_job FROM public.jobs WHERE id = p_job_id FOR UPDATE;
    SELECT * INTO v_activation FROM public.external_boot_activations
    WHERE id = p_activation_id FOR UPDATE;
    SELECT * INTO v_system FROM public.systems WHERE id = v_activation.system_id;
    IF v_activation.state <> 'preparing'
       OR (v_job.state = 'running' AND v_job.worker_id = v_incarnation
           AND v_job.attempt = p_job_attempt AND v_job.lease_expires_at > clock_timestamp()
           AND v_job.payload ->> 'system_id' = v_system.id::text
           AND NOT v_job.payload ?| ARRAY['external_boot_authority_v1', 'authority_system_v1']
           AND v_system.domain_name = p_domain_name) IS NOT TRUE THEN
        RETURN 'superseded';
    END IF;
    INSERT INTO public.external_boot_activation_settlements AS settlement (
        activation_id, system_id, worker_incarnation, worker_job_id, worker_job_attempt,
        worker_domain_name, worker_recorded_at
    ) VALUES (
        p_activation_id, v_system.id, v_incarnation, p_job_id, p_job_attempt, p_domain_name,
        clock_timestamp()
    ) ON CONFLICT (activation_id) DO UPDATE SET
        worker_incarnation = excluded.worker_incarnation,
        worker_job_id = excluded.worker_job_id,
        worker_job_attempt = excluded.worker_job_attempt,
        worker_domain_name = excluded.worker_domain_name,
        worker_recorded_at = excluded.worker_recorded_at
    WHERE settlement.settled_at IS NULL;
    RETURN 'applied';
END $$;

CREATE FUNCTION public.settle_stranded_external_boot_activation(
    p_activation_id uuid, p_principal text, p_agent_session text, p_actor text, p_reason text
) RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_activation public.external_boot_activations%ROWTYPE;
    v_system public.systems%ROWTYPE;
    v_settlement public.external_boot_activation_settlements%ROWTYPE;
    v_reservation public.external_boot_reservations%ROWTYPE;
    v_head public.external_boot_authority_journal_heads%ROWTYPE;
    v_known text[];
    v_refs text[];
    v_now text;
    v_release jsonb;
    v_release_identity text;
    v_reason text := regexp_replace(p_reason, '^\s+|\s+$', '', 'g');
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_server', 'member') THEN
        RAISE EXCEPTION 'server authority is required' USING ERRCODE = '42501';
    END IF;
    IF v_reason IS NULL OR octet_length(v_reason) NOT BETWEEN 1 AND 1024
       OR p_principal IS NULL OR btrim(p_principal) = '' OR p_actor IS NULL THEN
        RAISE EXCEPTION 'settlement requires a principal and a reason' USING ERRCODE = '22023';
    END IF;
    SELECT * INTO v_activation FROM public.external_boot_activations WHERE id = p_activation_id;
    IF NOT FOUND THEN RETURN 'not_found'; END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'kdive:system:' || v_activation.system_id::text, 2125));
    SELECT * INTO v_activation FROM public.external_boot_activations
    WHERE id = p_activation_id FOR UPDATE;
    SELECT * INTO v_system FROM public.systems WHERE id = v_activation.system_id FOR UPDATE;
    SELECT * INTO v_settlement FROM public.external_boot_activation_settlements
    WHERE activation_id = p_activation_id FOR UPDATE;
    IF v_settlement.settled_at IS NOT NULL THEN RETURN 'replayed'; END IF;
    IF EXISTS (SELECT 1 FROM public.authority_system_ownership AS owner
               WHERE owner.system_id = v_system.id) THEN
        RETURN 'authority_owned';
    END IF;
    IF v_activation.state <> 'preparing' THEN RETURN 'not_preparing'; END IF;
    IF v_system.state IN ('torn_down', 'tearing_down') THEN RETURN 'system_ended'; END IF;
    IF EXISTS (SELECT 1 FROM public.jobs AS job
               WHERE job.state IN ('queued', 'running')
                 AND job.payload #>> '{external_boot_authority_v1,system_id}'
                     = v_system.id::text) THEN
        RETURN 'job_live';
    END IF;
    IF v_settlement.authority_recorded_at IS NULL THEN RETURN 'authority_evidence_missing'; END IF;
    IF v_settlement.worker_recorded_at IS NULL THEN RETURN 'worker_evidence_missing'; END IF;
    IF least(v_settlement.authority_recorded_at, v_settlement.worker_recorded_at)
       < clock_timestamp() - interval '1 hour'
       OR v_settlement.authority_domain_name IS DISTINCT FROM v_system.domain_name
       OR v_settlement.worker_domain_name IS DISTINCT FROM v_system.domain_name THEN
        RETURN 'evidence_stale';
    END IF;
    SELECT * INTO v_head FROM public.external_boot_authority_journal_heads
    WHERE authority_instance = v_settlement.authority_instance AND system_id = v_system.id
    FOR UPDATE;
    IF (v_head.sequence, v_head.digest) IS DISTINCT FROM
       (v_settlement.authority_journal_sequence, v_settlement.authority_journal_digest) THEN
        RETURN 'authority_journal_moved';
    END IF;
    SELECT * INTO v_reservation FROM public.external_boot_reservations
    WHERE activation_id = p_activation_id FOR UPDATE;
    IF NOT FOUND THEN RETURN 'reservation_missing'; END IF;
    v_known := ARRAY[
        v_activation.materialization #>> '{artifacts,kernel,ref}',
        v_activation.materialization #>> '{artifacts,modules,ref}',
        v_activation.materialization #>> '{artifacts,initrd,ref}',
        v_activation.materialization #>> '{artifacts,debuginfo,ref}',
        v_activation.recovery_point #>> '{recovery_ref,ref}',
        v_activation.pre_recovery_evidence #>> '{recovery_object,ref}'
    ];
    IF EXISTS (SELECT 1 FROM unnest(v_known) AS ref
               WHERE ref IS NOT NULL
                 AND ref <> ALL (v_settlement.authority_absent_objects)) THEN
        RETURN 'objects_unproven';
    END IF;
    v_refs := ARRAY(SELECT ref FROM unnest(v_settlement.authority_absent_objects) AS ref
                    ORDER BY convert_to(ref || '"', 'UTF8'));
    v_now := to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"');
    v_release := jsonb_build_object(
        'schema', 'external-boot-release-evidence-v1',
        'activation_id', p_activation_id::text, 'system_id', v_system.id::text,
        'store_identity', jsonb_build_object('ref', v_reservation.store_identity),
        'owner_key', jsonb_build_object('ref', v_reservation.owner_key),
        'reserved_bytes', v_reservation.reserved_bytes, 'enumeration_complete', true,
        'objects', coalesce((
            SELECT jsonb_agg(jsonb_build_object(
                'absent', true, 'object', jsonb_build_object('ref', ref)) ORDER BY ordinal)
            FROM unnest(v_refs) WITH ORDINALITY AS listed (ref, ordinal)), '[]'::jsonb),
        'verified_at', v_now
    );
    v_release_identity := 'sha256:' || encode(sha256(
        convert_to('kdive-external-boot-release-evidence-v1', 'UTF8') || decode('00', 'hex')
        || convert_to(public.canonical_external_boot_authority_json(v_release), 'UTF8')), 'hex');
    INSERT INTO public.external_boot_reservation_releases (
        activation_id, store_identity, owner_key, reserved_bytes, release_identity,
        release_evidence
    ) VALUES (
        p_activation_id, v_reservation.store_identity, v_reservation.owner_key,
        v_reservation.reserved_bytes, v_release_identity, v_release
    );
    DELETE FROM public.external_boot_reservations WHERE activation_id = p_activation_id;
    UPDATE public.external_boot_activations SET state = 'abandoned', cleanup_complete = true,
        terminal_evidence = jsonb_build_object(
            'schema', 'external-boot-terminal-evidence-v1',
            'activation_id', p_activation_id::text, 'system_id', v_system.id::text,
            'outcome', 'abandoned',
            'composite_state', v_settlement.authority_composite_state,
            'objects', coalesce((
                SELECT jsonb_agg(jsonb_build_object('ref', ref) ORDER BY ordinal)
                FROM unnest(v_refs) WITH ORDINALITY AS listed (ref, ordinal)), '[]'::jsonb),
            'observed_at', to_char(v_settlement.authority_recorded_at AT TIME ZONE 'UTC',
                                   'YYYY-MM-DD"T"HH24:MI:SS"Z"')),
        cleanup_evidence = jsonb_build_object(
            'schema', 'external-boot-cleanup-evidence-v1',
            'activation_id', p_activation_id::text, 'system_id', v_system.id::text,
            'release_identity', v_release_identity, 'mode', 'ordinary', 'completed_at', v_now)
    WHERE id = p_activation_id;
    UPDATE public.external_boot_authorities
    SET state = 'superseded', superseded_at = clock_timestamp()
    WHERE activation_id = p_activation_id AND state IN ('allocating', 'current');
    UPDATE public.remote_module_attempt_obligations
    SET mutation_discharged_at = now(), mutation_discharge_reason = 'terminal_escape'
    WHERE system_id = v_system.id AND mutation_discharged_at IS NULL;
    UPDATE public.external_boot_activation_settlements
    SET principal = p_principal, reason = v_reason, settled_at = clock_timestamp()
    WHERE activation_id = p_activation_id;
    INSERT INTO public.platform_audit_log (
        principal, agent_session, platform_role, tool, scope, args_digest, actor
    ) VALUES (
        p_principal, p_agent_session, 'platform_admin',
        'external_boot.settle_stranded_activation', 'settled:' || p_activation_id::text,
        encode(sha256(convert_to(public.canonical_external_boot_authority_json(
            jsonb_build_object('activation_id', p_activation_id::text, 'reason', v_reason)
        ), 'UTF8')), 'hex'),
        p_actor
    );
    RETURN 'applied';
END $$;

REVOKE ALL ON TABLE public.external_boot_activation_settlements
FROM PUBLIC, kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness,
     kdive_provider_authority;
GRANT SELECT ON TABLE public.external_boot_activation_settlements
    TO kdive_server, kdive_worker, kdive_reconciler;
REVOKE ALL ON FUNCTION
    public.record_stranded_activation_authority_absence(uuid,text,text,text,text[]),
    public.record_stranded_activation_worker_ownership(bytea,uuid,integer,uuid,text),
    public.settle_stranded_external_boot_activation(uuid,text,text,text,text)
FROM PUBLIC, kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness,
     kdive_provider_authority;
GRANT EXECUTE ON FUNCTION public.record_stranded_activation_authority_absence(uuid,text,text,text,text[])
    TO kdive_provider_authority;
GRANT EXECUTE ON FUNCTION
    public.record_stranded_activation_worker_ownership(bytea,uuid,integer,uuid,text)
    TO kdive_worker;
GRANT EXECUTE ON FUNCTION public.settle_stranded_external_boot_activation(uuid,text,text,text,text)
    TO kdive_server;
