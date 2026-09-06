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
                (state IN ('recovery_conflict', 'recovery_failed')
                 AND cleanup_evidence ->> 'mode' IN ('system_teardown', 'pending_system_teardown')
                 AND teardown_evidence IS NOT NULL)
                OR
                (state = 'torn_down'
                 AND cleanup_evidence ->> 'mode' IN (
                     'ordinary', 'system_teardown', 'pending_system_teardown'
                 )
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

CREATE TABLE public.external_boot_teardown_receipts (
    root_authority_id uuid PRIMARY KEY REFERENCES public.external_boot_authorities (id)
        ON DELETE RESTRICT,
    job_id uuid NOT NULL REFERENCES public.jobs (id) ON DELETE RESTRICT,
    job_attempt integer NOT NULL CHECK (job_attempt > 0),
    journal_sequence bigint NOT NULL CHECK (journal_sequence > 0),
    journal_digest text NOT NULL CHECK (journal_digest ~ '^sha256:[0-9a-f]{64}$'),
    proof_bytes bytea NOT NULL CHECK (octet_length(proof_bytes) BETWEEN 1 AND 131072),
    proof_digest text NOT NULL CHECK (proof_digest ~ '^sha256:[0-9a-f]{64}$'),
    disposition text NOT NULL CHECK (disposition IN (
        'complete_ready', 'complete_pending', 'complete_released', 'retained_quarantine'
    )),
    consumed boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    consumed_at timestamptz,
    CHECK (consumed = (consumed_at IS NOT NULL))
);

CREATE FUNCTION public.finalize_external_boot_authority_teardown(
    p_credential_hash bytea, p_job_id uuid, p_attempt integer,
    p_authority_id uuid, p_generation bigint, p_journal_sequence bigint,
    p_journal_digest text, p_proof_bytes bytea
) RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_authority public.external_boot_authorities%ROWTYPE;
    v_job public.jobs%ROWTYPE;
    v_activation public.external_boot_activations%ROWTYPE;
    v_reservation public.external_boot_reservations%ROWTYPE;
    v_release public.external_boot_reservation_releases%ROWTYPE;
    v_head public.external_boot_authority_journal_heads%ROWTYPE;
    v_receipt public.external_boot_teardown_receipts%ROWTYPE;
    v_incarnation text;
    v_proof jsonb;
    v_digest text;
    v_disposition text;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_worker', 'member') THEN
        RAISE EXCEPTION 'worker authority is required' USING ERRCODE = '42501';
    END IF;
    IF octet_length(p_proof_bytes) NOT BETWEEN 1 AND 131072
       OR p_journal_sequence < 1 OR p_journal_digest !~ '^sha256:[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'external boot teardown receipt is invalid' USING ERRCODE = '22023';
    END IF;
    v_proof := convert_from(p_proof_bytes, 'UTF8')::jsonb;
    IF jsonb_typeof(v_proof) IS DISTINCT FROM 'object' THEN
        RAISE EXCEPTION 'external boot teardown proof must be an object' USING ERRCODE = '22023';
    END IF;
    v_disposition := v_proof->>'disposition';
    IF v_disposition NOT IN (
        'complete_ready', 'complete_pending', 'complete_released', 'retained_quarantine'
    ) THEN RAISE EXCEPTION 'external boot teardown disposition is invalid' USING ERRCODE = '22023'; END IF;
    v_digest := 'sha256:' || encode(sha256(
        convert_to('kdive-external-boot-teardown-proof-v1', 'UTF8') || decode('00', 'hex')
        || p_proof_bytes), 'hex');
    SELECT incarnation INTO v_incarnation FROM public.worker_incarnations
    WHERE credential_hash = p_credential_hash AND state = 'active' AND fence_protocol = 4;
    SELECT * INTO v_authority FROM public.external_boot_authorities
    WHERE id = p_authority_id AND generation = p_generation;
    IF v_incarnation IS NULL OR v_authority.id IS NULL THEN RETURN 'superseded'; END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'kdive:system:' || v_authority.system_id::text, 2125));
    SELECT * INTO v_authority FROM public.external_boot_authorities
    WHERE id = p_authority_id AND generation = p_generation FOR UPDATE;
    SELECT * INTO v_job FROM public.jobs WHERE id = p_job_id FOR UPDATE;
    SELECT * INTO v_activation FROM public.external_boot_activations
    WHERE id = v_authority.activation_id FOR UPDATE;
    SELECT * INTO v_reservation FROM public.external_boot_reservations
    WHERE activation_id = v_authority.activation_id FOR UPDATE;
    SELECT * INTO v_release FROM public.external_boot_reservation_releases
    WHERE activation_id = v_authority.activation_id;
    SELECT * INTO v_head FROM public.external_boot_authority_journal_heads
    WHERE system_id = v_authority.system_id AND authority_instance = v_authority.authority_instance
    FOR UPDATE;
    SELECT * INTO v_receipt FROM public.external_boot_teardown_receipts
    WHERE root_authority_id = p_authority_id FOR UPDATE;
    IF v_receipt.root_authority_id IS NOT NULL THEN
        RETURN CASE WHEN v_receipt.proof_bytes = p_proof_bytes
                       AND v_receipt.journal_sequence = p_journal_sequence
                       AND v_receipt.journal_digest = p_journal_digest
                    THEN CASE WHEN v_receipt.consumed THEN 'applied' ELSE 'retained' END
                    ELSE 'conflict' END;
    END IF;
    IF (v_authority.state = 'current' AND v_authority.purpose = 'teardown'
        AND v_authority.operation = 'teardown' AND v_authority.worker_incarnation = v_incarnation
        AND v_authority.job_id = p_job_id AND v_authority.job_attempt = p_attempt
        AND v_job.state = 'running' AND v_job.worker_id = v_incarnation
        AND v_job.attempt = p_attempt AND v_job.lease_expires_at > clock_timestamp()
        AND v_head.authority_id = p_authority_id AND v_head.generation = p_generation
        AND v_head.phase = 'terminal' AND v_head.sequence = p_journal_sequence
        AND v_head.digest = p_journal_digest
        AND v_head.head_record #>> '{observation,composite_state}' = v_digest
        AND ((v_disposition = 'retained_quarantine') =
             (v_head.head_record #>> '{observation,category}' <> 'absent'))
    ) IS NOT TRUE THEN RETURN 'superseded'; END IF;
    IF v_disposition = 'complete_ready' AND NOT (
        v_reservation.state = 'ready' AND v_proof->'release_evidence' = jsonb_build_object(
            'schema', 'external-boot-release-evidence-v1',
            'activation_id', v_authority.activation_id::text,
            'system_id', v_authority.system_id::text,
            'store_identity', jsonb_build_object('ref', v_reservation.store_identity),
            'owner_key', jsonb_build_object('ref', v_reservation.owner_key),
            'reserved_bytes', v_reservation.reserved_bytes, 'enumeration_complete', true,
            'objects', jsonb_build_array(), 'verified_at', v_proof #> '{release_evidence,verified_at}'
        ) AND v_proof->>'release_identity' = 'sha256:' || encode(sha256(
            convert_to('kdive-external-boot-release-evidence-v1', 'UTF8') || decode('00', 'hex')
            || convert_to(public.canonical_external_boot_authority_json(
                v_proof->'release_evidence'
            ), 'UTF8')
        ), 'hex'
        ) AND v_proof #>> '{cleanup_evidence,schema}' = 'external-boot-cleanup-evidence-v1'
          AND v_proof #>> '{cleanup_evidence,activation_id}' = v_authority.activation_id::text
          AND v_proof #>> '{cleanup_evidence,system_id}' = v_authority.system_id::text
          AND v_proof #>> '{cleanup_evidence,release_identity}' = v_proof->>'release_identity'
          AND v_proof #>> '{cleanup_evidence,mode}' = 'system_teardown'
          AND v_proof #>> '{cleanup_evidence,teardown_identity}' ~ '^sha256:[0-9a-f]{64}$'
    ) THEN RETURN 'superseded'; END IF;
    IF v_disposition = 'complete_pending' AND NOT (v_reservation.state = 'pending') THEN
        RETURN 'superseded'; END IF;
    IF v_disposition = 'complete_released' AND NOT (
        v_reservation.activation_id IS NULL AND v_release.activation_id IS NOT NULL
        AND v_activation.cleanup_complete AND v_activation.state IN ('recovered', 'abandoned')
    ) THEN RETURN 'superseded'; END IF;
    INSERT INTO public.external_boot_teardown_receipts (
        root_authority_id, job_id, job_attempt, journal_sequence, journal_digest,
        proof_bytes, proof_digest, disposition, consumed, consumed_at
    ) VALUES (p_authority_id, p_job_id, p_attempt, p_journal_sequence, p_journal_digest,
        p_proof_bytes, v_digest, v_disposition, v_disposition <> 'retained_quarantine',
        CASE WHEN v_disposition <> 'retained_quarantine' THEN clock_timestamp() END);
    IF v_disposition = 'retained_quarantine' THEN
        UPDATE public.jobs SET state = 'queued', worker_id = NULL, lease_expires_at = NULL,
            error_category = 'conflict' WHERE id = p_job_id;
        UPDATE public.external_boot_authorities SET state = 'superseded', superseded_at = clock_timestamp()
        WHERE id = p_authority_id;
        RETURN 'retained';
    END IF;
    IF v_disposition = 'complete_ready' THEN
        INSERT INTO public.external_boot_reservation_releases (
            activation_id, store_identity, owner_key, reserved_bytes, release_identity, release_evidence
        ) VALUES (v_reservation.activation_id, v_reservation.store_identity, v_reservation.owner_key,
            v_reservation.reserved_bytes, v_proof->>'release_identity', v_proof->'release_evidence');
        DELETE FROM public.external_boot_reservations WHERE activation_id = v_authority.activation_id;
    ELSIF v_disposition = 'complete_pending' THEN
        DELETE FROM public.external_boot_reservations WHERE activation_id = v_authority.activation_id;
    END IF;
    UPDATE public.external_boot_activations SET state = 'torn_down', cleanup_complete = true,
        teardown_evidence = v_proof->'teardown_evidence',
        cleanup_evidence = coalesce(v_proof->'cleanup_evidence', cleanup_evidence)
    WHERE id = v_authority.activation_id;
    UPDATE public.systems SET state = 'torn_down' WHERE id = v_authority.system_id;
    UPDATE public.jobs SET state = 'succeeded', result_ref = NULL WHERE id = p_job_id;
    UPDATE public.external_boot_authorities SET state = 'retired', retired_at = clock_timestamp()
    WHERE id = p_authority_id;
    RETURN 'applied';
END $$;

REVOKE ALL ON public.external_boot_teardown_receipts FROM PUBLIC;
REVOKE ALL ON FUNCTION public.finalize_external_boot_authority_teardown(
    bytea,uuid,integer,uuid,bigint,bigint,text,bytea
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.finalize_external_boot_authority_teardown(
    bytea,uuid,integer,uuid,bigint,bigint,text,bytea
) TO kdive_worker;

-- Full System teardown has no recovery identities.  Preserve the legacy paired-string shape for
-- every other mutation (including the old teardown records) while admitting the one null pair.
DO $$
DECLARE
    v_definition text;
BEGIN
    SELECT pg_get_functiondef(
        'public.advance_external_boot_authority_journal_head(text,uuid,bigint,bigint,text,jsonb)'::regprocedure
    ) INTO v_definition;
    IF v_definition NOT LIKE '%v_bound_operation text;%' THEN
        RAISE EXCEPTION 'external boot journal shape changed';
    END IF;
    v_definition := replace(
        v_definition,
        E'    IF v_phase NOT IN (''watermark-installed'', ''takeover-superseded'', ''takeover-acknowledged'')\n' ||
        E'       AND (jsonb_typeof(p_record->''expected_source_identity'') <> ''string''\n' ||
        E'            OR jsonb_typeof(p_record->''intended_target_identity'') <> ''string''\n' ||
        E'            OR jsonb_typeof(p_record->''recovery_objects'') <> ''array'')\n' ||
        E'    THEN RETURN ''conflict''; END IF;',
        E'    IF v_phase NOT IN (''watermark-installed'', ''takeover-superseded'', ''takeover-acknowledged'')\n' ||
        E'       AND NOT (\n' ||
        E'           (jsonb_typeof(p_record->''expected_source_identity'') = ''string''\n' ||
        E'            AND jsonb_typeof(p_record->''intended_target_identity'') = ''string''\n' ||
        E'            AND jsonb_typeof(p_record->''recovery_objects'') = ''array'')\n' ||
        E'           OR (p_record->>''purpose'' = ''teardown'' AND p_record->>''operation'' = ''teardown''\n' ||
        E'               AND p_record->''expected_source_identity'' = ''null''::jsonb\n' ||
        E'               AND p_record->''intended_target_identity'' = ''null''::jsonb\n' ||
        E'               AND p_record->''recovery_objects'' = ''[]''::jsonb)\n' ||
        E'       )\n' ||
        E'    THEN RETURN ''conflict''; END IF;'
    );
    v_definition := replace(
        v_definition,
        E'       OR (v_phase NOT IN (''watermark-installed'', ''takeover-superseded'', ''takeover-acknowledged'')\n' ||
        E'           AND (octet_length(p_record->>''expected_source_identity'') NOT BETWEEN 1 AND 1024\n' ||
        E'                OR octet_length(p_record->>''intended_target_identity'') NOT BETWEEN 1 AND 1024))',
        E'       OR (v_phase NOT IN (''watermark-installed'', ''takeover-superseded'', ''takeover-acknowledged'')\n' ||
        E'           AND NOT (\n' ||
        E'               (octet_length(p_record->>''expected_source_identity'') BETWEEN 1 AND 1024\n' ||
        E'                AND octet_length(p_record->>''intended_target_identity'') BETWEEN 1 AND 1024)\n' ||
        E'               OR (p_record->>''purpose'' = ''teardown'' AND p_record->>''operation'' = ''teardown''\n' ||
        E'                   AND p_record->''expected_source_identity'' = ''null''::jsonb\n' ||
        E'                   AND p_record->''intended_target_identity'' = ''null''::jsonb\n' ||
        E'                   AND p_record->''recovery_objects'' = ''[]''::jsonb)\n' ||
        E'           ))'
    );
    v_definition := replace(
        v_definition,
        E'        AND v_head.suspended_operation->>''source_identity'' = p_record->>''expected_source_identity''\n' ||
        E'        AND v_head.suspended_operation->>''target_identity'' = p_record->>''intended_target_identity''',
        E'        AND v_head.suspended_operation->>''source_identity''\n' ||
        E'            IS NOT DISTINCT FROM p_record->>''expected_source_identity''\n' ||
        E'        AND v_head.suspended_operation->>''target_identity''\n' ||
        E'            IS NOT DISTINCT FROM p_record->>''intended_target_identity'''
    );
    IF v_definition NOT LIKE '%IS NOT DISTINCT FROM p_record->>''expected_source_identity''%'
       OR v_definition NOT LIKE '%p_record->''expected_source_identity'' = ''null''::jsonb%' THEN
        RAISE EXCEPTION 'external boot full teardown journal gate was not installed';
    END IF;
    EXECUTE v_definition;
END
$$;
