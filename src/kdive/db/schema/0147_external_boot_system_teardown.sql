-- ADR-0620: the authority, not a worker provisioner, owns external-boot teardown.

ALTER TABLE public.external_boot_activations
    DROP CONSTRAINT external_boot_activation_state,
    ADD CONSTRAINT external_boot_activation_state CHECK (state IN (
        'preparing', 'prepared', 'activating', 'active', 'recovering', 'recovered',
        'recovery_conflict', 'recovery_failed', 'abandoned', 'torn_down'
    ));

ALTER TABLE public.external_boot_activations
    DROP CONSTRAINT external_boot_activation_cleanup_state,
    ADD CONSTRAINT external_boot_activation_cleanup_state CHECK (
        NOT cleanup_complete OR state IN (
            'recovered', 'abandoned', 'recovery_conflict', 'recovery_failed', 'torn_down'
        )
    ),
    DROP CONSTRAINT external_boot_activation_state_evidence,
    ADD CONSTRAINT external_boot_activation_state_evidence CHECK (
        (state = 'preparing')
        OR (state = 'prepared' AND materialization IS NOT NULL AND recovery_point IS NOT NULL)
        OR (state = 'activating' AND materialization IS NOT NULL AND recovery_point IS NOT NULL)
        OR (state = 'active' AND materialization IS NOT NULL AND recovery_point IS NOT NULL
            AND terminal_evidence ->> 'outcome' IS NOT DISTINCT FROM 'active')
        OR (state IN ('recovering', 'recovery_conflict', 'recovery_failed', 'recovered')
            AND materialization IS NOT NULL AND current_attempt_id IS NOT NULL
            AND (recovery_point IS NOT NULL OR pre_recovery_evidence IS NOT NULL))
        OR (state = 'abandoned'
            AND terminal_evidence ->> 'outcome' IS NOT DISTINCT FROM 'abandoned')
        OR (state = 'torn_down'
            AND teardown_evidence ->> 'schema'
                IS NOT DISTINCT FROM 'external-boot-teardown-evidence-v1')
    ),
    DROP CONSTRAINT external_boot_activation_cleanup_evidence,
    ADD CONSTRAINT external_boot_activation_cleanup_evidence CHECK (
        (NOT cleanup_complete AND cleanup_evidence IS NULL)
        OR (
            cleanup_evidence IS NOT NULL
            AND (
                (state IN ('recovered', 'abandoned')
                 AND cleanup_evidence ->> 'mode' IS NOT DISTINCT FROM 'ordinary'
                 AND teardown_evidence IS NULL)
                OR
                (state IN ('recovery_conflict', 'recovery_failed', 'torn_down')
                 AND cleanup_evidence ->> 'mode' IN ('system_teardown', 'pending_system_teardown')
                 AND teardown_evidence IS NOT NULL)
            )
        )
    );

DROP INDEX public.external_boot_activations_one_live_per_system;
CREATE UNIQUE INDEX external_boot_activations_one_live_per_system
    ON public.external_boot_activations (system_id)
    WHERE state NOT IN ('recovered', 'abandoned', 'torn_down') OR NOT cleanup_complete;

-- Keep the pre-existing authority commit fence intact: it already verifies the exact worker,
-- acknowledged generation, job attempt and result evidence.  Its legacy teardown tail wrote only
-- ``cleanup_complete``; with physical teardown now represented separately, that tail must record
-- the terminal activation state in the same transaction as the System transition.
DO $$
DECLARE
    v_definition text;
BEGIN
    SELECT pg_get_functiondef((
        'public.commit_external_boot_authority_result('
        || 'bytea,uuid,integer,uuid,bigint,uuid,uuid,uuid,text,text,text,text,text,text,'
        || 'bigint,text,text,jsonb)'
    )::regprocedure) INTO v_definition;
    IF v_definition NOT LIKE '%ELSIF v_operation = ''teardown'' THEN%' THEN
        RAISE EXCEPTION 'external boot authority teardown commit shape changed';
    END IF;
    v_definition := replace(
        v_definition,
        E'UPDATE public.external_boot_activations\n' ||
        E'        SET cleanup_complete = true,\n' ||
        E'            teardown_evidence = p_result -> ''teardown_evidence'',\n' ||
        E'            cleanup_evidence = p_result -> ''cleanup_evidence''\n' ||
        E'        WHERE id = p_activation_id;',
        E'UPDATE public.external_boot_activations\n' ||
        E'        SET state = ''torn_down'', cleanup_complete = true,\n' ||
        E'            teardown_evidence = p_result -> ''teardown_evidence'',\n' ||
        E'            cleanup_evidence = p_result -> ''cleanup_evidence''\n' ||
        E'        WHERE id = p_activation_id;'
    );
    IF v_definition NOT LIKE '%SET state = ''torn_down'', cleanup_complete = true%' THEN
        RAISE EXCEPTION 'external boot authority teardown transition was not installed';
    END IF;
    EXECUTE v_definition;
END
$$;

CREATE FUNCTION public.resolve_external_boot_system_teardown_dispatch_binding(
    p_system_id uuid
) RETURNS TABLE (activation_id uuid, run_id uuid, plan_identity text, provider_kind text,
                 authority_instance text)
LANGUAGE sql SECURITY DEFINER SET search_path = '' STABLE AS $$
    SELECT activation.id, activation.run_id, activation.plan_identity,
           authority.provider_kind, authority.authority_instance
    FROM public.external_boot_activations AS activation
    JOIN LATERAL (
        SELECT a.provider_kind, a.authority_instance
        FROM public.external_boot_authorities AS a
        WHERE a.activation_id = activation.id
          AND a.system_id = activation.system_id
          AND a.run_id = activation.run_id
          AND a.plan_identity = activation.plan_identity
          AND a.state IN ('current', 'retired')
        ORDER BY a.generation DESC
        LIMIT 1
    ) AS authority ON true
    WHERE activation.system_id = p_system_id
    ORDER BY activation.created_at DESC, activation.id DESC
    LIMIT 1
$$;

REVOKE ALL ON FUNCTION public.resolve_external_boot_system_teardown_dispatch_binding(uuid)
    FROM PUBLIC, kdive_worker, kdive_reconciler, kdive_lifecycle_witness,
         kdive_provider_authority;
GRANT EXECUTE ON FUNCTION public.resolve_external_boot_system_teardown_dispatch_binding(uuid)
    TO kdive_server;

CREATE FUNCTION public.resolve_current_external_boot_teardown_authority(
    p_peer_incarnation text, p_authority_id uuid, p_generation bigint,
    p_ack_sequence bigint, p_ack_digest text
) RETURNS TABLE (
    peer_incarnation_id text, authority_id uuid, generation bigint, system_id uuid,
    activation_id uuid, run_id uuid, plan_identity text, purpose text, operation text,
    provider_kind text, authority_instance text, operation_identity text, operation_digest text,
    state text, reservation_disposition text, store_identity text, owner_key text,
    reserved_bytes bigint, release_identity text, release_evidence jsonb
) LANGUAGE sql SECURITY DEFINER SET search_path = '' STABLE AS $$
    SELECT a.worker_incarnation, a.id, a.generation, a.system_id, a.activation_id, a.run_id,
           a.plan_identity, a.purpose, a.operation, a.provider_kind, a.authority_instance,
           a.operation_identity, a.operation_digest, a.state,
           CASE WHEN reservation.activation_id IS NOT NULL THEN reservation.state
                ELSE 'released' END,
           coalesce(reservation.store_identity, released.store_identity),
           coalesce(reservation.owner_key, released.owner_key),
           coalesce(reservation.reserved_bytes, released.reserved_bytes),
           released.release_identity, released.release_evidence
    FROM public.external_boot_authorities AS a
    JOIN public.worker_incarnations AS worker ON worker.incarnation = a.worker_incarnation
    JOIN public.external_boot_authority_acknowledgements AS acknowledgement
      ON acknowledgement.authority_id = a.id
    JOIN public.external_boot_activations AS activation ON activation.id = a.activation_id
    LEFT JOIN public.external_boot_reservations AS reservation
      ON reservation.activation_id = activation.id
    LEFT JOIN public.external_boot_reservation_releases AS released
      ON released.activation_id = activation.id
    WHERE pg_has_role(session_user, 'kdive_provider_authority', 'member')
      AND worker.incarnation = p_peer_incarnation AND worker.state = 'active'
      AND worker.fence_protocol = 4
      AND a.id = p_authority_id AND a.generation = p_generation AND a.state = 'current'
      AND a.purpose = 'teardown' AND a.operation = 'teardown'
      AND acknowledgement.journal_sequence = p_ack_sequence
      AND acknowledgement.journal_digest = p_ack_digest
      AND (
          reservation.state IN ('pending', 'ready')
          OR (
              reservation.activation_id IS NULL AND released.activation_id IS NOT NULL
              AND activation.state IN ('recovered', 'abandoned') AND activation.cleanup_complete
          )
      )
$$;

REVOKE ALL ON FUNCTION public.resolve_current_external_boot_teardown_authority(
    text,uuid,bigint,bigint,text
) FROM PUBLIC, kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness;
GRANT EXECUTE ON FUNCTION public.resolve_current_external_boot_teardown_authority(
    text,uuid,bigint,bigint,text
) TO kdive_provider_authority;
