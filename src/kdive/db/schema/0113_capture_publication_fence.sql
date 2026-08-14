-- Fresh-install publication fencing for supervised capture operations (#1952).

SELECT pg_advisory_xact_lock(hashtextextended('kdive:capture-protocol', 1951));
LOCK TABLE public.worker_incarnations, public.jobs, public.capture_operations, public.artifacts
    IN SHARE ROW EXCLUSIVE MODE;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM public.worker_incarnations)
       OR EXISTS (SELECT 1 FROM public.jobs)
       OR EXISTS (SELECT 1 FROM public.capture_operations)
       OR EXISTS (SELECT 1 FROM public.artifacts) THEN
        RAISE EXCEPTION
            'capture publication protocol 4 requires a fresh database and object-store namespace'
            USING ERRCODE = '23514';
    END IF;
END
$$;

ALTER TABLE public.capture_operation_cutoff
    DROP CONSTRAINT capture_operation_cutoff_protocol_check,
    ADD COLUMN publication_closed boolean NOT NULL DEFAULT false,
    ADD COLUMN complete boolean NOT NULL DEFAULT false;

UPDATE public.capture_operation_cutoff
SET protocol = 4,
    publication_closed = true,
    complete = true,
    cutoff_at = clock_timestamp()
WHERE singleton;

ALTER TABLE public.capture_operation_cutoff
    ALTER COLUMN publication_closed DROP DEFAULT,
    ALTER COLUMN complete DROP DEFAULT,
    ADD CONSTRAINT capture_operation_cutoff_protocol_check CHECK (protocol = 4),
    ADD CONSTRAINT capture_operation_cutoff_publication_closed_check CHECK (publication_closed),
    ADD CONSTRAINT capture_operation_cutoff_complete_check CHECK (
        complete = (operation_quiescent AND publication_closed)
    );

ALTER TABLE public.capture_operations
    ADD COLUMN publication_state text NOT NULL DEFAULT 'pending',
    ADD COLUMN publication_object_key text,
    ADD COLUMN publication_etag text,
    ADD COLUMN publication_artifact_id uuid
        REFERENCES public.artifacts(id) ON DELETE RESTRICT,
    ADD COLUMN cleanup_capture_version_id text,
    ADD COLUMN publication_tombstone_version text,
    ADD COLUMN publication_started_at timestamptz,
    ADD COLUMN publication_closed_at timestamptz,
    ADD COLUMN spool_disposed_at timestamptz,
    ADD CONSTRAINT capture_operations_publication_artifact_key
        UNIQUE (publication_artifact_id),
    ADD CONSTRAINT capture_operations_publication_state_check CHECK (
        publication_state IN ('pending', 'publishing', 'canceling', 'published', 'discarded')
    ),
    ADD CONSTRAINT capture_operations_publication_values_bounded CHECK (
        (publication_object_key IS NULL
            OR octet_length(publication_object_key) BETWEEN 1 AND 2048)
        AND (publication_etag IS NULL OR octet_length(publication_etag) BETWEEN 1 AND 1024)
        AND (cleanup_capture_version_id IS NULL
            OR octet_length(cleanup_capture_version_id) BETWEEN 1 AND 1024)
        AND (publication_tombstone_version IS NULL
            OR octet_length(publication_tombstone_version) BETWEEN 1 AND 1024)
    ),
    ADD CONSTRAINT capture_operations_publication_shape CHECK (
        (
            publication_state = 'pending'
            AND publication_object_key IS NULL
            AND publication_etag IS NULL
            AND publication_artifact_id IS NULL
            AND cleanup_capture_version_id IS NULL
            AND publication_tombstone_version IS NULL
            AND publication_started_at IS NULL
            AND publication_closed_at IS NULL
        ) OR (
            publication_state = 'publishing'
            AND publication_object_key IS NOT NULL
            AND publication_artifact_id IS NULL
            AND publication_tombstone_version IS NULL
            AND publication_started_at IS NOT NULL
            AND publication_closed_at IS NULL
            AND (cleanup_capture_version_id IS NULL) = (publication_etag IS NULL)
        ) OR (
            publication_state = 'canceling'
            AND publication_object_key IS NOT NULL
            AND publication_artifact_id IS NULL
            AND publication_tombstone_version IS NULL
            AND publication_started_at IS NOT NULL
            AND publication_closed_at IS NULL
        ) OR (
            publication_state = 'published'
            AND publication_object_key IS NOT NULL
            AND publication_etag IS NOT NULL
            AND publication_artifact_id IS NOT NULL
            AND cleanup_capture_version_id IS NOT NULL
            AND publication_tombstone_version IS NULL
            AND publication_started_at IS NOT NULL
            AND publication_closed_at IS NOT NULL
        ) OR (
            publication_state = 'discarded'
            AND publication_object_key IS NOT NULL
            AND publication_artifact_id IS NULL
            AND publication_tombstone_version IS NOT NULL
            AND publication_started_at IS NOT NULL
            AND publication_closed_at IS NOT NULL
        )
    ),
    ADD CONSTRAINT capture_operations_spool_disposal_shape CHECK (
        spool_disposed_at IS NULL OR publication_state IN ('published', 'discarded')
    );

-- Migration 0112 is immutable. Reinstall every security-definer function that embeds the exact
-- protocol-3 boundary with protocol 4, including registration, authentication, transition,
-- heartbeat, completion, failure, and claim functions. Catalog discovery makes omission fail
-- closed if an earlier immutable migration adds another exact protocol-bound function.
DO $$
DECLARE
    v_function record;
    v_definition text;
BEGIN
    FOR v_function IN
        SELECT p.oid, p.oid::regprocedure::text AS identity
        FROM pg_catalog.pg_proc AS p
        JOIN pg_catalog.pg_namespace AS n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public'
          AND p.prosecdef
          AND (
              pg_get_functiondef(p.oid) LIKE '%fence_protocol = 3%'
              OR pg_get_functiondef(p.oid) LIKE '%fence_protocol < 3%'
              OR pg_get_functiondef(p.oid) LIKE '%fence_protocol IS DISTINCT FROM 3%'
              OR pg_get_functiondef(p.oid) LIKE '%p_fence_protocol IS DISTINCT FROM 3%'
              OR pg_get_functiondef(p.oid) LIKE '%protocol 3%'
          )
        ORDER BY p.oid::regprocedure::text
    LOOP
        v_definition := pg_get_functiondef(v_function.oid);
        v_definition := replace(v_definition, 'fence_protocol = 3', 'fence_protocol = 4');
        v_definition := replace(v_definition, 'fence_protocol < 3', 'fence_protocol < 4');
        v_definition := replace(
            v_definition,
            'fence_protocol IS DISTINCT FROM 3',
            'fence_protocol IS DISTINCT FROM 4'
        );
        v_definition := replace(v_definition, 'protocol 3', 'protocol 4');
        IF v_definition LIKE '%fence_protocol = 3%'
           OR v_definition LIKE '%fence_protocol < 3%'
           OR v_definition LIKE '%fence_protocol IS DISTINCT FROM 3%'
           OR v_definition LIKE '%protocol 3%' THEN
            RAISE EXCEPTION 'protocol-4 function replacement was incomplete for %',
                v_function.identity;
        END IF;
        EXECUTE v_definition;
    END LOOP;
END
$$;

-- Replacement workers must also see provider-exited attempts whose publication product remains
-- open. Protocol 3 selected only live provider operations, so replace all three selection guards
-- together before any protocol-4 worker can recover.
DO $$
DECLARE
    v_function regprocedure;
    v_definition text;
    v_state_predicate text := 'o.state <> ''exited''';
    v_locked_state_predicate text := 'v_operation.state <> ''exited''';
    v_product_predicate text := $predicate$(
                o.state <> 'exited'
                OR o.publication_state NOT IN ('published', 'discarded')
                OR o.spool_disposed_at IS NULL
            )$predicate$;
BEGIN
    FOREACH v_function IN ARRAY ARRAY[
        'public.capture_recovery_candidate_replacement(bytea)'::regprocedure,
        'public.list_capture_recovery_candidates(bytea)'::regprocedure
    ]
    LOOP
        v_definition := pg_get_functiondef(v_function);
        IF v_definition NOT LIKE '%' || v_state_predicate || '%' THEN
            RAISE EXCEPTION 'protocol-4 recovery selection has an unexpected source shape for %',
                v_function;
        END IF;
        v_definition := replace(v_definition, v_state_predicate, v_product_predicate);
        IF v_function = 'public.list_capture_recovery_candidates(bytea)'::regprocedure THEN
            IF v_definition NOT LIKE '%' || v_locked_state_predicate || '%' THEN
                RAISE EXCEPTION 'protocol-4 locked recovery selection has an unexpected shape';
            END IF;
            v_definition := replace(
                v_definition,
                v_locked_state_predicate,
                replace(v_product_predicate, 'o.', 'v_operation.')
            );
        END IF;
        EXECUTE v_definition;
    END LOOP;
END
$$;

CREATE FUNCTION public.claim_capture_publication_recovery(
    p_credential_hash bytea,
    p_operation_id uuid
) RETURNS SETOF public.capture_operations
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_context record;
    v_operation public.capture_operations%ROWTYPE;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_worker', 'member') THEN
        RAISE EXCEPTION 'worker authority is required' USING ERRCODE = '42501';
    END IF;
    SELECT context.owner, context.replacement, context.operation INTO v_context
    FROM public.capture_recovery_context(p_credential_hash, p_operation_id) AS context;
    IF NOT FOUND
       OR NOT coalesce(
           public.capture_recovery_authorized(v_context.owner, v_context.replacement), false
       ) THEN
        RETURN;
    END IF;
    v_operation := v_context.operation;
    IF v_operation.state <> 'exited'
       OR (
           v_operation.publication_state IN ('published', 'discarded')
           AND v_operation.spool_disposed_at IS NOT NULL
       ) THEN
        RETURN;
    END IF;
    RETURN NEXT v_operation;
END
$$;

-- A queued/lapsed capture retry is not claimable until the previous attempt's complete product is
-- closed. Keep this replacement adjacent to the protocol replacement above: both reinstall the
-- immutable 0112 claim function for protocol 4.
DO $$
DECLARE
    v_function regprocedure :=
        'public.claim_worker_job(text,bytea,interval,text[])'::regprocedure;
    v_definition text;
    v_provider_marker text := 'OR o.provider_quiescence = ''{}''::jsonb';
    v_complete_marker text :=
        'OR o.publication_state NOT IN (''published'', ''discarded'')';
BEGIN
    v_definition := pg_get_functiondef(v_function);
    IF v_definition NOT LIKE '%' || v_provider_marker || '%'
       OR v_definition LIKE '%' || v_complete_marker || '%' THEN
        RAISE EXCEPTION 'protocol-4 claim closure replacement has an unexpected source shape';
    END IF;
    v_definition := replace(
        v_definition,
        v_provider_marker,
        v_provider_marker || E'\n                        ' || v_complete_marker
            || E'\n                        OR o.spool_disposed_at IS NULL'
    );
    EXECUTE v_definition;
END
$$;

-- Success closes the exact current link only after provider, publication, and spool closure. The
-- same complete-product predicate bars failure/requeue from advancing an incomplete attempt.
DO $$
DECLARE
    v_function regprocedure :=
        'public.complete_worker_job(uuid,bytea,integer,text)'::regprocedure;
    v_definition text;
    v_set_marker text := 'SET state = ''succeeded'', result_ref = p_result_ref';
    v_where_marker text := 'AND state = ''running''';
    v_closure text := $closure$
      AND (
          kind <> 'capture_traffic'
          OR EXISTS (
              SELECT 1 FROM public.capture_operations AS o
              WHERE o.id = jobs.current_capture_operation_id
                AND o.job_id = jobs.id
                AND o.job_attempt = jobs.attempt
                AND o.state = 'exited'
                AND o.process_absent
                AND o.provider_quiescence <> '{}'::jsonb
                AND o.publication_state IN ('published', 'discarded')
                AND o.spool_disposed_at IS NOT NULL
          )
      )$closure$;
BEGIN
    v_definition := pg_get_functiondef(v_function);
    IF v_definition NOT LIKE '%' || v_set_marker || '%'
       OR v_definition NOT LIKE '%' || v_where_marker || '%'
       OR v_definition LIKE '%spool_disposed_at IS NOT NULL%' THEN
        RAISE EXCEPTION 'protocol-4 completion replacement has an unexpected source shape';
    END IF;
    v_definition := replace(
        v_definition,
        v_set_marker,
        v_set_marker || E',\n        current_capture_operation_id = CASE'
            || E'\n            WHEN kind = ''capture_traffic'' THEN NULL'
            || E'\n            ELSE current_capture_operation_id\n        END'
    );
    v_definition := replace(v_definition, v_where_marker, v_where_marker || v_closure);
    EXECUTE v_definition;
END
$$;

DO $$
DECLARE
    v_function regprocedure :=
        'public.fail_worker_job(uuid,bytea,integer,text,jsonb,boolean)'::regprocedure;
    v_definition text;
    v_where_marker text := 'AND state = ''running''';
    v_closure text := $closure$
      AND (
          kind <> 'capture_traffic'
          OR EXISTS (
              SELECT 1 FROM public.capture_operations AS o
              WHERE o.id = jobs.current_capture_operation_id
                AND o.job_id = jobs.id
                AND o.job_attempt = jobs.attempt
                AND o.state = 'exited'
                AND o.process_absent
                AND o.provider_quiescence <> '{}'::jsonb
                AND o.publication_state IN ('published', 'discarded')
                AND o.spool_disposed_at IS NOT NULL
          )
      )$closure$;
BEGIN
    v_definition := pg_get_functiondef(v_function);
    IF v_definition NOT LIKE '%' || v_where_marker || '%'
       OR v_definition LIKE '%spool_disposed_at IS NOT NULL%' THEN
        RAISE EXCEPTION 'protocol-4 failure replacement has an unexpected source shape';
    END IF;
    v_definition := replace(v_definition, v_where_marker, v_where_marker || v_closure);
    EXECUTE v_definition;
END
$$;

CREATE FUNCTION public.capture_publication_operation(
    p_credential_hash bytea,
    p_operation_id uuid,
    p_allow_replacement boolean,
    p_allow_canceled boolean
) RETURNS SETOF public.capture_operations
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_context record;
    v_operation public.capture_operations%ROWTYPE;
    v_owner public.worker_incarnations%ROWTYPE;
    v_worker public.worker_incarnations%ROWTYPE;
    v_owner_id text;
    v_worker_id text;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_worker', 'member') THEN
        RAISE EXCEPTION 'worker authority is required' USING ERRCODE = '42501';
    END IF;
    IF p_credential_hash IS NULL
       OR octet_length(p_credential_hash) <> 32
       OR p_operation_id IS NULL
       OR p_allow_replacement IS NULL
       OR p_allow_canceled IS NULL THEN
        RETURN;
    END IF;
    SELECT w.incarnation INTO v_worker_id
    FROM public.worker_incarnations AS w
    WHERE w.credential_hash = p_credential_hash;
    SELECT o.worker_incarnation INTO v_owner_id
    FROM public.capture_operations AS o
    WHERE o.id = p_operation_id;
    IF v_worker_id IS NULL OR v_owner_id IS NULL THEN
        RETURN;
    END IF;
    IF v_worker_id = v_owner_id THEN
        SELECT authenticated.* INTO v_worker
        FROM public.capture_authenticated_worker(p_credential_hash) AS authenticated;
        IF NOT FOUND THEN
            RETURN;
        END IF;
        PERFORM pg_advisory_xact_lock(
            hashtextextended('kdive:capture-operation:' || p_operation_id::text, 1951)
        );
        SELECT o.* INTO v_operation
        FROM public.capture_operations AS o
        WHERE o.id = p_operation_id
          AND o.worker_incarnation = v_worker.incarnation
        FOR UPDATE;
    ELSIF p_allow_replacement THEN
        SELECT context.owner, context.replacement, context.operation INTO v_context
        FROM public.capture_recovery_context(p_credential_hash, p_operation_id) AS context;
        IF NOT FOUND THEN
            RETURN;
        END IF;
        v_owner := v_context.owner;
        v_worker := v_context.replacement;
        v_operation := v_context.operation;
        IF NOT coalesce(public.capture_recovery_authorized(v_owner, v_worker), false) THEN
            RETURN;
        END IF;
    ELSE
        RETURN;
    END IF;
    IF v_operation.id IS NULL THEN
        RETURN;
    END IF;
    PERFORM 1
    FROM public.jobs AS j
    WHERE j.id = v_operation.job_id
      AND j.attempt = v_operation.job_attempt
      AND j.current_capture_operation_id = v_operation.id
      AND j.worker_id = v_operation.worker_incarnation
      AND (
          j.state = 'running'
          OR (p_allow_canceled AND j.state = 'canceled')
      )
    FOR UPDATE;
    IF FOUND THEN
        RETURN NEXT v_operation;
    END IF;
END
$$;

CREATE FUNCTION public.begin_capture_publication(
    p_credential_hash bytea,
    p_operation_id uuid,
    p_object_key text
) RETURNS SETOF public.capture_operations
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_operation public.capture_operations%ROWTYPE;
BEGIN
    IF p_object_key IS NULL OR octet_length(p_object_key) NOT BETWEEN 1 AND 2048 THEN
        RAISE EXCEPTION 'capture publication key is invalid' USING ERRCODE = '22023';
    END IF;
    SELECT operation.* INTO v_operation
    FROM public.capture_publication_operation(
        p_credential_hash, p_operation_id, false, false
    ) AS operation;
    IF NOT FOUND OR v_operation.state <> 'exited' THEN
        RETURN;
    END IF;
    IF v_operation.publication_state = 'pending' THEN
        UPDATE public.capture_operations
        SET publication_state = 'publishing',
            publication_object_key = p_object_key,
            publication_started_at = clock_timestamp(),
            updated_at = clock_timestamp()
        WHERE id = p_operation_id
        RETURNING * INTO v_operation;
    ELSIF v_operation.publication_state NOT IN ('publishing', 'published')
       OR v_operation.publication_object_key IS DISTINCT FROM p_object_key THEN
        RETURN;
    END IF;
    RETURN NEXT v_operation;
END
$$;

CREATE FUNCTION public.begin_cancel_capture_publication(
    p_credential_hash bytea,
    p_operation_id uuid,
    p_object_key text
) RETURNS SETOF public.capture_operations
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_operation public.capture_operations%ROWTYPE;
BEGIN
    IF p_object_key IS NULL OR octet_length(p_object_key) NOT BETWEEN 1 AND 2048 THEN
        RAISE EXCEPTION 'capture publication key is invalid' USING ERRCODE = '22023';
    END IF;
    SELECT operation.* INTO v_operation
    FROM public.capture_publication_operation(
        p_credential_hash, p_operation_id, true, true
    ) AS operation;
    IF NOT FOUND OR v_operation.state <> 'exited' THEN
        RETURN;
    END IF;
    IF v_operation.publication_state = 'pending' THEN
        UPDATE public.capture_operations
        SET publication_state = 'canceling',
            publication_object_key = p_object_key,
            publication_started_at = clock_timestamp(),
            updated_at = clock_timestamp()
        WHERE id = p_operation_id
        RETURNING * INTO v_operation;
    ELSIF v_operation.publication_state = 'publishing'
       AND v_operation.publication_object_key = p_object_key THEN
        UPDATE public.capture_operations
        SET publication_state = 'canceling', updated_at = clock_timestamp()
        WHERE id = p_operation_id
        RETURNING * INTO v_operation;
    ELSIF v_operation.publication_state NOT IN ('canceling', 'discarded')
       OR v_operation.publication_object_key IS DISTINCT FROM p_object_key THEN
        RETURN;
    END IF;
    RETURN NEXT v_operation;
END
$$;

CREATE FUNCTION public.record_capture_publication_version(
    p_credential_hash bytea,
    p_operation_id uuid,
    p_version_id text,
    p_etag text
) RETURNS SETOF public.capture_operations
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_operation public.capture_operations%ROWTYPE;
BEGIN
    IF p_version_id IS NULL OR octet_length(p_version_id) NOT BETWEEN 1 AND 1024
       OR p_etag IS NULL OR octet_length(p_etag) NOT BETWEEN 1 AND 1024 THEN
        RAISE EXCEPTION 'capture publication object identity is invalid' USING ERRCODE = '22023';
    END IF;
    SELECT operation.* INTO v_operation
    FROM public.capture_publication_operation(
        p_credential_hash, p_operation_id, false, false
    ) AS operation;
    IF NOT FOUND OR v_operation.state <> 'exited' THEN
        RETURN;
    END IF;
    IF v_operation.publication_state = 'publishing'
       AND v_operation.cleanup_capture_version_id IS NULL
       AND v_operation.publication_etag IS NULL THEN
        UPDATE public.capture_operations
        SET cleanup_capture_version_id = p_version_id,
            publication_etag = p_etag,
            updated_at = clock_timestamp()
        WHERE id = p_operation_id
        RETURNING * INTO v_operation;
    ELSIF v_operation.publication_state NOT IN ('publishing', 'published')
       OR (v_operation.cleanup_capture_version_id, v_operation.publication_etag)
          IS DISTINCT FROM (p_version_id, p_etag) THEN
        RETURN;
    END IF;
    RETURN NEXT v_operation;
END
$$;

CREATE FUNCTION public.record_capture_cleanup_version(
    p_credential_hash bytea,
    p_operation_id uuid,
    p_version_id text
) RETURNS SETOF public.capture_operations
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_operation public.capture_operations%ROWTYPE;
BEGIN
    IF p_version_id IS NULL OR octet_length(p_version_id) NOT BETWEEN 1 AND 1024 THEN
        RAISE EXCEPTION 'capture cleanup version identity is invalid' USING ERRCODE = '22023';
    END IF;
    SELECT operation.* INTO v_operation
    FROM public.capture_publication_operation(
        p_credential_hash, p_operation_id, true, true
    ) AS operation;
    IF NOT FOUND OR v_operation.state <> 'exited'
       OR v_operation.publication_state <> 'canceling' THEN
        RETURN;
    END IF;
    IF v_operation.cleanup_capture_version_id IS NULL THEN
        UPDATE public.capture_operations
        SET cleanup_capture_version_id = p_version_id, updated_at = clock_timestamp()
        WHERE id = p_operation_id
        RETURNING * INTO v_operation;
    ELSIF v_operation.cleanup_capture_version_id IS DISTINCT FROM p_version_id THEN
        RETURN;
    END IF;
    RETURN NEXT v_operation;
END
$$;

CREATE FUNCTION public.commit_capture_published(
    p_credential_hash bytea, p_operation_id uuid, p_artifact jsonb, p_audit jsonb
) RETURNS SETOF public.capture_operations
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_artifact_id uuid;
    v_job public.jobs%ROWTYPE;
    v_operation public.capture_operations%ROWTYPE;
BEGIN
    IF p_artifact IS NULL OR jsonb_typeof(p_artifact) <> 'object'
       OR p_audit IS NULL OR jsonb_typeof(p_audit) <> 'object' THEN
        RAISE EXCEPTION 'capture publication commit facts are invalid' USING ERRCODE = '22023';
    END IF;
    BEGIN
        v_artifact_id := (p_artifact ->> 'id')::uuid;
    EXCEPTION WHEN invalid_text_representation THEN
        RAISE EXCEPTION 'capture publication artifact identity is invalid' USING ERRCODE = '22023';
    END;
    SELECT operation.* INTO v_operation
    FROM public.capture_publication_operation(
        p_credential_hash, p_operation_id, false, false) AS operation;
    IF NOT FOUND OR v_operation.state <> 'exited' THEN
        RETURN;
    END IF;
    SELECT j.* INTO v_job FROM public.jobs AS j WHERE j.id = v_operation.job_id;
    IF NOT FOUND
       OR v_artifact_id IS NULL
       OR p_artifact ->> 'owner_kind' IS DISTINCT FROM 'runs'
       OR p_artifact ->> 'owner_id' IS DISTINCT FROM v_job.payload ->> 'run_id'
       OR p_artifact ->> 'run_id' IS DISTINCT FROM v_job.payload ->> 'run_id'
       OR p_artifact ->> 'object_key' IS DISTINCT FROM v_operation.publication_object_key
       OR p_artifact ->> 'etag' IS DISTINCT FROM v_operation.publication_etag
       OR p_artifact ->> 'sensitivity' IS DISTINCT FROM 'sensitive'
       OR p_artifact ->> 'retention_class' IS DISTINCT FROM 'pcap'
       OR p_artifact -> 'encoding' IS DISTINCT FROM 'null'::jsonb
       OR p_artifact -> 'uncompressed_size' IS DISTINCT FROM 'null'::jsonb
       OR p_audit ->> 'tool' IS DISTINCT FROM 'control.capture_traffic'
       OR p_audit ->> 'object_kind' IS DISTINCT FROM 'runs'
       OR p_audit ->> 'object_id' IS DISTINCT FROM v_job.payload ->> 'run_id'
       OR p_audit ->> 'transition' IS DISTINCT FROM 'capture_traffic'
       OR p_audit ->> 'project' IS DISTINCT FROM v_job.authorizing ->> 'project'
       OR p_audit ->> 'args_digest' IS NULL
       OR (p_audit ->> 'args_digest') !~ '^[0-9a-f]{64}$' THEN
        RETURN;
    END IF;
    IF v_operation.publication_state = 'published' THEN
        IF v_operation.publication_artifact_id = v_artifact_id
           AND EXISTS (
               SELECT 1 FROM public.artifacts AS a WHERE a.id = v_artifact_id
                 AND a.owner_kind = 'runs'
                 AND a.owner_id::text = v_job.payload ->> 'run_id'
                 AND a.run_id = a.owner_id
                 AND a.object_key = v_operation.publication_object_key
                 AND a.etag = v_operation.publication_etag
                 AND a.sensitivity = 'sensitive'
                 AND a.retention_class = 'pcap'
                 AND a.encoding IS NULL AND a.uncompressed_size IS NULL
           ) THEN
            RETURN NEXT v_operation;
        END IF;
        RETURN;
    END IF;
    IF v_operation.publication_state <> 'publishing' OR v_operation.publication_etag IS NULL
       OR v_operation.cleanup_capture_version_id IS NULL THEN
        RETURN;
    END IF;
    INSERT INTO public.artifacts (
        id, owner_kind, owner_id, object_key, etag, sensitivity, retention_class, run_id, encoding,
        uncompressed_size) VALUES (
        v_artifact_id, 'runs', (p_artifact ->> 'owner_id')::uuid, p_artifact ->> 'object_key',
        p_artifact ->> 'etag', 'sensitive', 'pcap', (p_artifact ->> 'run_id')::uuid, NULL, NULL
    ) ON CONFLICT DO NOTHING;
    IF NOT EXISTS (
        SELECT 1 FROM public.artifacts AS a WHERE a.id = v_artifact_id
          AND a.owner_kind = 'runs'
          AND a.owner_id = (p_artifact ->> 'owner_id')::uuid
          AND a.run_id = a.owner_id
          AND a.object_key = p_artifact ->> 'object_key'
          AND a.etag = p_artifact ->> 'etag'
          AND a.sensitivity = 'sensitive'
          AND a.retention_class = 'pcap'
          AND a.encoding IS NULL AND a.uncompressed_size IS NULL
    ) THEN
        RETURN;
    END IF;
    INSERT INTO public.audit_log (
        principal, agent_session, project, tool, object_kind, object_id, transition, args_digest
    ) VALUES (
        v_job.authorizing ->> 'principal', v_job.authorizing ->> 'agent_session',
        p_audit ->> 'project', p_audit ->> 'tool', p_audit ->> 'object_kind',
        (p_audit ->> 'object_id')::uuid, p_audit ->> 'transition', p_audit ->> 'args_digest'
    );
    UPDATE public.capture_operations
    SET publication_state = 'published', publication_artifact_id = v_artifact_id,
        publication_closed_at = clock_timestamp(), updated_at = clock_timestamp()
    WHERE id = p_operation_id RETURNING * INTO v_operation;
    RETURN NEXT v_operation;
END
$$;

CREATE FUNCTION public.commit_capture_discarded(
    p_credential_hash bytea,
    p_operation_id uuid,
    p_tombstone_version text
) RETURNS SETOF public.capture_operations
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_operation public.capture_operations%ROWTYPE;
BEGIN
    IF p_tombstone_version IS NULL
       OR octet_length(p_tombstone_version) NOT BETWEEN 1 AND 1024 THEN
        RAISE EXCEPTION 'capture tombstone identity is invalid' USING ERRCODE = '22023';
    END IF;
    SELECT operation.* INTO v_operation
    FROM public.capture_publication_operation(
        p_credential_hash, p_operation_id, true, true
    ) AS operation;
    IF NOT FOUND OR v_operation.state <> 'exited' THEN
        RETURN;
    END IF;
    IF v_operation.publication_state = 'canceling'
       AND NOT EXISTS (
           SELECT 1 FROM public.artifacts AS a
           WHERE a.object_key = v_operation.publication_object_key
       ) THEN
        UPDATE public.capture_operations
        SET publication_state = 'discarded',
            publication_tombstone_version = p_tombstone_version,
            publication_closed_at = clock_timestamp(),
            updated_at = clock_timestamp()
        WHERE id = p_operation_id
        RETURNING * INTO v_operation;
    ELSIF v_operation.publication_state <> 'discarded'
       OR v_operation.publication_tombstone_version IS DISTINCT FROM p_tombstone_version THEN
        RETURN;
    END IF;
    RETURN NEXT v_operation;
END
$$;

CREATE FUNCTION public.record_capture_spool_disposed(
    p_credential_hash bytea,
    p_operation_id uuid
) RETURNS SETOF public.capture_operations
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_operation public.capture_operations%ROWTYPE;
BEGIN
    SELECT operation.* INTO v_operation
    FROM public.capture_publication_operation(
        p_credential_hash, p_operation_id, true, true
    ) AS operation;
    IF NOT FOUND OR v_operation.state <> 'exited'
       OR v_operation.publication_state NOT IN ('published', 'discarded') THEN
        RETURN;
    END IF;
    IF v_operation.spool_disposed_at IS NULL THEN
        UPDATE public.capture_operations
        SET spool_disposed_at = clock_timestamp(), updated_at = clock_timestamp()
        WHERE id = p_operation_id
        RETURNING * INTO v_operation;
    END IF;
    RETURN NEXT v_operation;
END
$$;

REVOKE ALL ON TABLE public.capture_operations, public.capture_operation_cutoff FROM PUBLIC;
REVOKE ALL ON TABLE public.capture_operations, public.capture_operation_cutoff
FROM kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness;

REVOKE ALL ON FUNCTION
    public.claim_capture_publication_recovery(bytea, uuid),
    public.capture_publication_operation(bytea, uuid, boolean, boolean),
    public.begin_capture_publication(bytea, uuid, text),
    public.begin_cancel_capture_publication(bytea, uuid, text),
    public.record_capture_publication_version(bytea, uuid, text, text),
    public.record_capture_cleanup_version(bytea, uuid, text),
    public.commit_capture_published(bytea, uuid, jsonb, jsonb),
    public.commit_capture_discarded(bytea, uuid, text),
    public.record_capture_spool_disposed(bytea, uuid)
FROM PUBLIC, kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness;

GRANT EXECUTE ON FUNCTION
    public.claim_capture_publication_recovery(bytea, uuid),
    public.begin_capture_publication(bytea, uuid, text),
    public.begin_cancel_capture_publication(bytea, uuid, text),
    public.record_capture_publication_version(bytea, uuid, text, text),
    public.record_capture_cleanup_version(bytea, uuid, text),
    public.commit_capture_published(bytea, uuid, jsonb, jsonb),
    public.commit_capture_discarded(bytea, uuid, text),
    public.record_capture_spool_disposed(bytea, uuid)
TO kdive_worker;

GRANT SELECT (
    id, job_id, job_attempt, worker_incarnation, provider_kind, resource_id, system_id,
    domain_name, host_instance, boot_id, pid, start_ticks, state, exit_outcome, exit_code,
    process_absent, provider_quiescence, recovered_by, publication_state,
    publication_object_key, publication_etag, publication_artifact_id,
    cleanup_capture_version_id, publication_tombstone_version, publication_started_at,
    publication_closed_at, spool_disposed_at, created_at, identity_recorded_at, running_at,
    cancel_requested_at, exited_at, updated_at
) ON public.capture_operations TO kdive_reconciler;
