-- Runtime paths over protected worker-fence evidence (ADR-0535, #1803).
-- Server diagnostics stay database-bounded, recovery is one evidence-checked transition, and
-- generation GC sees only the two columns that identify an exact pin.

CREATE FUNCTION list_investigation_build_uses(
    p_authorized_projects text[],
    p_after_created_at timestamptz,
    p_after_use_id uuid,
    p_limit integer
)
RETURNS TABLE (
    use_id uuid,
    investigation_id uuid,
    generation uuid,
    job_id uuid,
    attempt integer,
    holder_worker_id text,
    created_at timestamptz,
    page_truncated boolean
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_server', 'member')
       AND session_user <> current_user THEN
        RAISE EXCEPTION 'server authority is required' USING ERRCODE = '42501';
    END IF;
    IF p_authorized_projects IS NULL THEN
        RAISE EXCEPTION 'authorized project scope is required' USING ERRCODE = '22023';
    END IF;
    IF (p_after_created_at IS NULL) <> (p_after_use_id IS NULL) THEN
        RAISE EXCEPTION 'build-use cursor boundary is incomplete' USING ERRCODE = '22023';
    END IF;
    IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 100 THEN
        RAISE EXCEPTION 'build-use diagnostic limit must be between 1 and 100 rows'
            USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
    WITH page AS MATERIALIZED (
        SELECT
            u.use_id,
            u.investigation_id,
            u.generation,
            u.job_id,
            u.attempt,
            u.holder_worker_id,
            u.created_at
        FROM public.investigation_build_uses AS u
        JOIN public.investigation_builds AS b
          ON b.investigation_id = u.investigation_id AND b.generation = u.generation
        JOIN public.investigations AS i ON i.id = b.investigation_id
        WHERE i.project = ANY(p_authorized_projects)
          AND (
              p_after_created_at IS NULL
              OR (u.created_at, u.use_id) > (p_after_created_at, p_after_use_id)
          )
        ORDER BY u.created_at, u.use_id
        LIMIT p_limit + 1
    ), numbered AS (
        SELECT
            page.*,
            row_number() OVER (ORDER BY page.created_at, page.use_id) AS page_row,
            count(*) OVER () > p_limit AS page_truncated
        FROM page
    )
    SELECT
        numbered.use_id,
        numbered.investigation_id,
        numbered.generation,
        numbered.job_id,
        numbered.attempt,
        numbered.holder_worker_id,
        numbered.created_at,
        numbered.page_truncated
    FROM numbered
    WHERE numbered.page_row <= p_limit
    ORDER BY numbered.created_at, numbered.use_id;
END
$$;

CREATE INDEX investigation_build_uses_created_use_idx
    ON investigation_build_uses (created_at, use_id);

DROP FUNCTION recover_investigation_build_use(uuid, text, text, text);

CREATE FUNCTION recover_investigation_build_use(
    p_use_id uuid,
    p_authorized_projects text[],
    p_expected_holder text,
    p_recovered_by text,
    p_reason text
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_use public.investigation_build_uses%ROWTYPE;
    v_incarnation public.worker_incarnations%ROWTYPE;
    v_project text;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_server', 'member')
       AND NOT pg_has_role(session_user, 'kdive_reconciler', 'member')
       AND session_user <> current_user THEN
        RAISE EXCEPTION 'server or reconciler authority is required' USING ERRCODE = '42501';
    END IF;
    IF p_use_id IS NULL
       OR p_authorized_projects IS NULL
       OR p_expected_holder IS NULL
       OR octet_length(p_expected_holder) NOT BETWEEN 1 AND 512
       OR length(btrim(p_expected_holder)) = 0
       OR p_recovered_by IS NULL
       OR octet_length(p_recovered_by) NOT BETWEEN 1 AND 255
       OR length(btrim(p_recovered_by)) = 0
       OR p_reason IS NULL
       OR octet_length(p_reason) NOT BETWEEN 1 AND 512
       OR length(btrim(p_reason)) = 0 THEN
        RAISE EXCEPTION 'recovery facts are invalid' USING ERRCODE = '22023';
    END IF;
    PERFORM 1
    FROM public.investigation_build_uses AS u
    JOIN public.investigation_builds AS b
      ON b.investigation_id = u.investigation_id AND b.generation = u.generation
    JOIN public.investigations AS i ON i.id = b.investigation_id
    WHERE u.use_id = p_use_id
      AND u.holder_worker_id = p_expected_holder
      AND i.project = ANY(p_authorized_projects);
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended('kdive:worker-incarnation:' || p_expected_holder, 1803)
    );
    SELECT u.* INTO v_use
    FROM public.investigation_build_uses AS u
    JOIN public.investigation_builds AS b
      ON b.investigation_id = u.investigation_id AND b.generation = u.generation
    JOIN public.investigations AS i ON i.id = b.investigation_id
    WHERE u.use_id = p_use_id
      AND u.holder_worker_id = p_expected_holder
      AND i.project = ANY(p_authorized_projects)
    FOR UPDATE OF u, b, i;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    SELECT i.project INTO STRICT v_project
    FROM public.investigations AS i
    WHERE i.id = v_use.investigation_id;
    SELECT w.* INTO v_incarnation
    FROM public.worker_incarnations AS w
    WHERE w.incarnation = v_use.holder_worker_id AND w.state = 'terminated'
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    INSERT INTO public.investigation_build_use_recoveries (
        use_id, project, investigation_id, generation, job_id, attempt, holder_worker_id,
        recovered_by, evidence, reason, authority_kind, authority_binding,
        termination_outcome, terminated_at
    ) VALUES (
        v_use.use_id,
        v_project,
        v_use.investigation_id,
        v_use.generation,
        v_use.job_id,
        v_use.attempt,
        v_use.holder_worker_id,
        p_recovered_by,
        v_incarnation.authority_kind || ': durable exact-incarnation termination ('
            || v_incarnation.outcome || ')',
        p_reason,
        v_incarnation.authority_kind,
        v_incarnation.authority_binding,
        v_incarnation.outcome,
        v_incarnation.terminated_at
    );
    DELETE FROM public.investigation_build_uses
    WHERE use_id = v_use.use_id
      AND investigation_id = v_use.investigation_id
      AND generation = v_use.generation
      AND job_id = v_use.job_id
      AND attempt = v_use.attempt
      AND holder_worker_id = v_use.holder_worker_id;
    RETURN FOUND;
END
$$;

REVOKE ALL ON FUNCTION
    list_investigation_build_uses(text[], timestamptz, uuid, integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION recover_investigation_build_use(uuid, text[], text, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION
    list_investigation_build_uses(text[], timestamptz, uuid, integer),
    recover_investigation_build_use(uuid, text[], text, text, text)
FROM kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness;

GRANT EXECUTE ON FUNCTION list_investigation_build_uses(text[], timestamptz, uuid, integer)
    TO kdive_server;
GRANT EXECUTE ON FUNCTION recover_investigation_build_use(uuid, text[], text, text, text)
    TO kdive_server, kdive_reconciler;
GRANT SELECT (investigation_id, generation) ON public.investigation_build_uses
    TO kdive_reconciler;
