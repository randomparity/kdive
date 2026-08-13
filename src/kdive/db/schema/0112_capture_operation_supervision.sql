-- Durable supervised capture operations and unconditional protocol-3 cutover (#1951, ADR-0558).

-- The old registration function cannot take this new lock, so the table lock also fences an
-- already-started protocol-2 registration until the protocol floor trigger commits.
SELECT pg_advisory_xact_lock(hashtextextended('kdive:capture-protocol', 1951));
LOCK TABLE public.worker_incarnations IN SHARE ROW EXCLUSIVE MODE;

DO $$
DECLARE
    v_blockers text;
BEGIN
    SELECT string_agg(incarnation, ', ' ORDER BY incarnation) INTO v_blockers
    FROM public.worker_incarnations
    WHERE fence_protocol < 3
      AND (
          state <> 'terminated'
          OR terminated_at IS NULL
          OR outcome IS NULL
          OR jsonb_typeof(authority_binding) <> 'object'
          OR CASE authority_kind
              WHEN 'local' THEN
                  jsonb_typeof(authority_binding -> 'host') IS DISTINCT FROM 'string'
                  OR length(btrim(authority_binding ->> 'host')) = 0
              WHEN 'docker' THEN
                  jsonb_typeof(authority_binding -> 'container_id') IS DISTINCT FROM 'string'
                  OR (authority_binding ->> 'container_id') !~ '^[0-9a-f]{64}$'
              WHEN 'kubernetes' THEN
                  jsonb_typeof(authority_binding -> 'namespace') IS DISTINCT FROM 'string'
                  OR jsonb_typeof(authority_binding -> 'name') IS DISTINCT FROM 'string'
                  OR jsonb_typeof(authority_binding -> 'uid') IS DISTINCT FROM 'string'
              ELSE true
          END
      );
    IF v_blockers IS NOT NULL THEN
        RAISE EXCEPTION 'offline capture protocol cutover blocked by worker incarnations: %',
            left(v_blockers, 2048)
            USING ERRCODE = '23514';
    END IF;

    SELECT string_agg(j.id::text, ', ' ORDER BY j.id) INTO v_blockers
    FROM public.jobs AS j
    LEFT JOIN public.worker_incarnations AS w ON w.incarnation = j.worker_id
    WHERE j.kind = 'capture_traffic'
      AND j.state = 'running'
      AND (
          w.incarnation IS NULL
          OR w.state <> 'terminated'
          OR w.terminated_at IS NULL
          OR w.outcome IS NULL
      );
    IF v_blockers IS NOT NULL THEN
        RAISE EXCEPTION 'offline capture protocol cutover blocked by capture jobs: %',
            left(v_blockers, 2048)
            USING ERRCODE = '23514';
    END IF;
END
$$;

-- Positively terminated legacy work is not preserved across this pre-release cutover.
SELECT pg_advisory_xact_lock(hashtextextended('kdive:job:' || j.id::text, 1951))
FROM public.jobs AS j
JOIN public.worker_incarnations AS w ON w.incarnation = j.worker_id
WHERE j.kind = 'capture_traffic'
  AND j.state = 'running'
  AND w.state = 'terminated'
  AND w.terminated_at IS NOT NULL
  AND w.outcome IS NOT NULL
ORDER BY j.id;

UPDATE public.jobs AS j
SET state = 'canceled',
    worker_id = NULL,
    lease_expires_at = NULL,
    heartbeat_at = NULL,
    error_category = NULL,
    failure_context = '{"reason":"offline_capture_protocol_cutover"}'::jsonb
FROM public.worker_incarnations AS w
WHERE j.kind = 'capture_traffic'
  AND j.state = 'running'
  AND j.worker_id = w.incarnation
  AND w.state = 'terminated'
  AND w.terminated_at IS NOT NULL
  AND w.outcome IS NOT NULL;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM public.worker_incarnations
        WHERE fence_protocol < 3
          AND (state <> 'terminated' OR terminated_at IS NULL OR outcome IS NULL)
    ) OR EXISTS (
        SELECT 1 FROM public.jobs WHERE kind = 'capture_traffic' AND state = 'running'
    ) THEN
        RAISE EXCEPTION 'offline capture protocol cutover population changed during recheck'
            USING ERRCODE = '23514';
    END IF;
END
$$;

CREATE TABLE public.capture_operation_cutoff (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    protocol integer NOT NULL CHECK (protocol = 3),
    operation_quiescent boolean NOT NULL CHECK (operation_quiescent),
    cutoff_at timestamptz NOT NULL
);

INSERT INTO public.capture_operation_cutoff (
    singleton, protocol, operation_quiescent, cutoff_at
) VALUES (true, 3, true, clock_timestamp());

CREATE TABLE public.capture_operations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id uuid NOT NULL REFERENCES public.jobs(id) ON DELETE RESTRICT,
    job_attempt integer NOT NULL CHECK (job_attempt > 0),
    worker_incarnation text NOT NULL REFERENCES public.worker_incarnations(incarnation),
    provider_kind text NOT NULL CHECK (provider_kind IN ('local-libvirt', 'remote-libvirt')),
    resource_id uuid NOT NULL REFERENCES public.resources(id) ON DELETE RESTRICT,
    system_id uuid NOT NULL REFERENCES public.systems(id) ON DELETE RESTRICT,
    domain_name text NOT NULL CHECK (octet_length(domain_name) BETWEEN 1 AND 255),
    request_digest text NOT NULL CHECK (request_digest ~ '^[0-9a-f]{64}$'),
    launch_token text NOT NULL UNIQUE CHECK (launch_token ~ '^[0-9a-f]{64}$'),
    host_instance text NOT NULL CHECK (octet_length(host_instance) BETWEEN 1 AND 512),
    boot_id text,
    pid integer,
    start_ticks bigint,
    state text NOT NULL DEFAULT 'launching' CHECK (
        state IN ('launching', 'gated', 'running', 'cancel_requested', 'exited')
    ),
    exit_outcome text CHECK (exit_outcome IS NULL OR octet_length(exit_outcome) BETWEEN 1 AND 64),
    exit_code integer,
    process_absent boolean NOT NULL DEFAULT false,
    provider_quiescence jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (
        jsonb_typeof(provider_quiescence) = 'object'
        AND octet_length(provider_quiescence::text) <= 4096
    ),
    recovered_by text REFERENCES public.worker_incarnations(incarnation),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    identity_recorded_at timestamptz,
    running_at timestamptz,
    cancel_requested_at timestamptz,
    exited_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT capture_operations_job_attempt_key UNIQUE (job_id, job_attempt),
    CONSTRAINT capture_operations_identity_shape CHECK (
        (boot_id IS NULL AND pid IS NULL AND start_ticks IS NULL)
        OR (
            boot_id IS NOT NULL
            AND octet_length(boot_id) BETWEEN 1 AND 128
            AND pid > 0
            AND start_ticks >= 0
        )
    ),
    CONSTRAINT capture_operations_state_shape CHECK (
        (state = 'launching' AND boot_id IS NULL)
        OR (state IN ('gated', 'running', 'cancel_requested') AND boot_id IS NOT NULL)
        OR state = 'exited'
    ),
    CONSTRAINT capture_operations_exit_shape CHECK (
        (state <> 'exited' AND exited_at IS NULL AND exit_outcome IS NULL)
        OR (
            state = 'exited'
            AND exited_at IS NOT NULL
            AND exit_outcome IS NOT NULL
            AND process_absent
            AND provider_quiescence <> '{}'::jsonb
        )
    )
);

CREATE INDEX capture_operations_state_created_idx
    ON public.capture_operations (state, created_at, id);
CREATE INDEX capture_operations_worker_state_idx
    ON public.capture_operations (worker_incarnation, state, id);

ALTER TABLE public.jobs
    ADD COLUMN current_capture_operation_id uuid,
    ADD CONSTRAINT jobs_current_capture_operation_id_fkey
        FOREIGN KEY (current_capture_operation_id)
        REFERENCES public.capture_operations(id) ON DELETE RESTRICT;

CREATE UNIQUE INDEX jobs_current_capture_operation_id_key
    ON public.jobs (current_capture_operation_id)
    WHERE current_capture_operation_id IS NOT NULL;

CREATE FUNCTION public.enforce_current_capture_operation_link()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
BEGIN
    IF NEW.current_capture_operation_id IS NOT NULL AND NOT EXISTS (
        SELECT 1
        FROM public.capture_operations AS o
        WHERE o.id = NEW.current_capture_operation_id
          AND o.job_id = NEW.id
          AND o.job_attempt = NEW.attempt
    ) THEN
        RAISE EXCEPTION 'current capture operation must match the exact job attempt'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER jobs_current_capture_operation_link
BEFORE INSERT OR UPDATE OF current_capture_operation_id, attempt ON public.jobs
FOR EACH ROW EXECUTE FUNCTION public.enforce_current_capture_operation_link();

CREATE FUNCTION public.capture_worker_host_instance(
    p_authority_kind text,
    p_authority_binding jsonb
) RETURNS text
LANGUAGE sql
IMMUTABLE
SET search_path = ''
RETURN CASE p_authority_kind
    WHEN 'local' THEN nullif(btrim(p_authority_binding ->> 'host'), '')
    WHEN 'docker' THEN nullif(btrim(p_authority_binding ->> 'container_id'), '')
    WHEN 'kubernetes' THEN nullif(btrim(p_authority_binding ->> 'uid'), '')
    ELSE NULL
END;

CREATE FUNCTION public.capture_worker_authority_scope(
    p_authority_kind text,
    p_authority_binding jsonb
) RETURNS text
LANGUAGE sql
IMMUTABLE
SET search_path = ''
RETURN CASE p_authority_kind
    WHEN 'local' THEN
        'local:' || nullif(btrim(p_authority_binding ->> 'host'), '')
    WHEN 'docker' THEN
        'docker:'
        || nullif(btrim(p_authority_binding ->> 'project'), '') || ':'
        || nullif(btrim(p_authority_binding ->> 'service'), '') || ':'
        || nullif(btrim(p_authority_binding ->> 'ordinal'), '')
    WHEN 'kubernetes' THEN
        'kubernetes:'
        || nullif(btrim(p_authority_binding ->> 'namespace'), '') || ':'
        || coalesce(
            nullif(btrim(p_authority_binding ->> 'statefulset'), ''),
            regexp_replace(p_authority_binding ->> 'name', '-[0-9]+$', '')
        ) || ':'
        || coalesce(
            nullif(btrim(p_authority_binding ->> 'ordinal'), ''),
            substring(p_authority_binding ->> 'name' from '-([0-9]+)$')
        )
    ELSE NULL
END;

CREATE FUNCTION public.capture_authenticated_worker(p_credential_hash bytea)
RETURNS SETOF public.worker_incarnations
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_incarnation text;
BEGIN
    SELECT w.incarnation INTO v_incarnation
    FROM public.worker_incarnations AS w
    WHERE w.credential_hash = p_credential_hash;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended('kdive:worker-incarnation:' || v_incarnation, 1803)
    );
    RETURN QUERY
    SELECT w.*
    FROM public.worker_incarnations AS w
    WHERE w.incarnation = v_incarnation
      AND w.credential_hash = p_credential_hash
      AND w.state = 'active'
      AND w.fence_protocol = 3
    FOR UPDATE;
END
$$;

CREATE FUNCTION public.capture_create_or_replay_operation(
    p_worker_incarnation text,
    p_job_id uuid,
    p_job_attempt integer,
    p_provider_kind text,
    p_resource_id uuid,
    p_system_id uuid,
    p_domain_name text,
    p_request_digest text,
    p_host_instance text
) RETURNS SETOF public.capture_operations
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_operation public.capture_operations%ROWTYPE;
BEGIN
    INSERT INTO public.capture_operations (
        job_id, job_attempt, worker_incarnation, provider_kind, resource_id, system_id,
        domain_name, request_digest, launch_token, host_instance
    ) VALUES (
        p_job_id, p_job_attempt, p_worker_incarnation, p_provider_kind, p_resource_id, p_system_id,
        p_domain_name, p_request_digest,
        encode(
            sha256(convert_to(gen_random_uuid()::text || gen_random_uuid()::text, 'UTF8')),
            'hex'
        ),
        p_host_instance
    ) ON CONFLICT (job_id, job_attempt) DO NOTHING
    RETURNING * INTO v_operation;
    IF NOT FOUND THEN
        SELECT o.* INTO v_operation
        FROM public.capture_operations AS o
        WHERE o.job_id = p_job_id
          AND o.job_attempt = p_job_attempt
          AND o.worker_incarnation = p_worker_incarnation
          AND o.provider_kind = p_provider_kind
          AND o.resource_id = p_resource_id
          AND o.system_id = p_system_id
          AND o.domain_name = p_domain_name
          AND o.request_digest = p_request_digest
        FOR UPDATE;
        IF NOT FOUND THEN
            RETURN;
        END IF;
    END IF;
    RETURN NEXT v_operation;
END
$$;

CREATE FUNCTION public.capture_launch_abort_evidence_valid(
    p_operation public.capture_operations,
    p_evidence jsonb
) RETURNS boolean
LANGUAGE sql
IMMUTABLE
SET search_path = ''
RETURN jsonb_typeof(p_evidence) = 'object'
   AND (p_evidence ->> 'evidence_kind') = 'closed_gate_boundary_token_scan_v1'
   AND (p_evidence -> 'gate_closed') = 'true'::jsonb
   AND (p_evidence -> 'boundary_scan_complete') = 'true'::jsonb
   AND (p_evidence -> 'boundary_processes_absent') = 'true'::jsonb
   AND jsonb_typeof(p_evidence -> 'host_instance') = 'string'
   AND (p_evidence ->> 'host_instance') = p_operation.host_instance
   AND jsonb_typeof(p_evidence -> 'launch_token') = 'string'
   AND (p_evidence ->> 'launch_token') = p_operation.launch_token
   AND (p_evidence -> 'launch_token_absent') = 'true'::jsonb;

CREATE FUNCTION public.capture_recovery_authorized(
    p_owner public.worker_incarnations,
    p_replacement public.worker_incarnations
) RETURNS boolean
LANGUAGE sql
IMMUTABLE
SET search_path = ''
RETURN p_owner.state = 'terminated'
   AND p_owner.terminated_at IS NOT NULL
   AND p_owner.outcome IS NOT NULL
   AND p_owner.authority_kind = p_replacement.authority_kind
   AND (
       public.capture_worker_host_instance(
           p_owner.authority_kind, p_owner.authority_binding
       ) = public.capture_worker_host_instance(
           p_replacement.authority_kind, p_replacement.authority_binding
       )
       OR (
           public.capture_worker_authority_scope(
               p_owner.authority_kind, p_owner.authority_binding
           ) IS NOT NULL
           AND public.capture_worker_authority_scope(
               p_owner.authority_kind, p_owner.authority_binding
           ) = public.capture_worker_authority_scope(
               p_replacement.authority_kind, p_replacement.authority_binding
           )
       )
   );

CREATE FUNCTION public.capture_recovery_context(
    p_credential_hash bytea,
    p_operation_id uuid
) RETURNS TABLE (
    owner public.worker_incarnations,
    replacement public.worker_incarnations,
    operation public.capture_operations
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_owner_id text;
    v_replacement_id text;
    v_owner public.worker_incarnations%ROWTYPE;
    v_replacement public.worker_incarnations%ROWTYPE;
    v_operation public.capture_operations%ROWTYPE;
BEGIN
    SELECT w.incarnation INTO v_replacement_id
    FROM public.worker_incarnations AS w
    WHERE w.credential_hash = p_credential_hash;
    SELECT o.worker_incarnation INTO v_owner_id
    FROM public.capture_operations AS o
    WHERE o.id = p_operation_id;
    IF v_replacement_id IS NULL OR v_owner_id IS NULL THEN
        RETURN;
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended(
            'kdive:worker-incarnation:' || least(v_owner_id, v_replacement_id), 1803
        )
    );
    IF v_owner_id <> v_replacement_id THEN
        PERFORM pg_advisory_xact_lock(
            hashtextextended(
                'kdive:worker-incarnation:' || greatest(v_owner_id, v_replacement_id), 1803
            )
        );
    END IF;
    SELECT w.* INTO v_replacement
    FROM public.worker_incarnations AS w
    WHERE w.incarnation = v_replacement_id
      AND w.credential_hash = p_credential_hash
      AND w.state = 'active'
      AND w.fence_protocol = 3
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    SELECT w.* INTO v_owner FROM public.worker_incarnations AS w
    WHERE w.incarnation = v_owner_id
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended('kdive:capture-operation:' || p_operation_id::text, 1951)
    );
    SELECT o.* INTO v_operation FROM public.capture_operations AS o
    WHERE o.id = p_operation_id AND o.worker_incarnation = v_owner.incarnation
    FOR UPDATE;
    IF FOUND THEN
        RETURN QUERY SELECT v_owner, v_replacement, v_operation;
    END IF;
END
$$;

CREATE FUNCTION public.list_capture_recovery_candidates(p_credential_hash bytea)
RETURNS TABLE (
    id uuid,
    job_id uuid,
    job_attempt integer,
    worker_incarnation text,
    provider_kind text,
    resource_id uuid,
    system_id uuid,
    domain_name text,
    launch_token text,
    host_instance text,
    boot_id text,
    pid integer,
    start_ticks bigint,
    state text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_operation public.capture_operations%ROWTYPE;
    v_owner public.worker_incarnations%ROWTYPE;
    v_replacement public.worker_incarnations%ROWTYPE;
    v_replacement_id text;
    v_worker_id text;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_worker', 'member') THEN
        RAISE EXCEPTION 'worker authority is required' USING ERRCODE = '42501';
    END IF;
    IF p_credential_hash IS NULL OR octet_length(p_credential_hash) <> 32 THEN
        RETURN;
    END IF;
    SELECT w.* INTO v_replacement
    FROM public.worker_incarnations AS w
    WHERE w.credential_hash = p_credential_hash
      AND w.state = 'active'
      AND w.fence_protocol = 3;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    v_replacement_id := v_replacement.incarnation;
    FOR v_worker_id IN
        SELECT candidate.incarnation
        FROM (
            SELECT v_replacement_id AS incarnation
            UNION
            SELECT o.worker_incarnation
            FROM public.capture_operations AS o
            JOIN public.worker_incarnations AS owner
              ON owner.incarnation = o.worker_incarnation
            WHERE o.state <> 'exited'
              AND public.capture_recovery_authorized(owner, v_replacement)
        ) AS candidate
        ORDER BY candidate.incarnation
    LOOP
        PERFORM pg_advisory_xact_lock(
            hashtextextended('kdive:worker-incarnation:' || v_worker_id, 1803)
        );
    END LOOP;
    SELECT w.* INTO v_replacement
    FROM public.worker_incarnations AS w
    WHERE w.incarnation = v_replacement_id
      AND w.credential_hash = p_credential_hash
      AND w.state = 'active'
      AND w.fence_protocol = 3
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    FOR v_operation IN
        SELECT o.*
        FROM public.capture_operations AS o
        JOIN public.worker_incarnations AS owner ON owner.incarnation = o.worker_incarnation
        WHERE o.state <> 'exited'
          AND public.capture_recovery_authorized(owner, v_replacement)
        ORDER BY o.id
    LOOP
        PERFORM pg_advisory_xact_lock(
            hashtextextended('kdive:capture-operation:' || v_operation.id::text, 1951)
        );
        SELECT o.* INTO v_operation
        FROM public.capture_operations AS o
        WHERE o.id = v_operation.id
        FOR UPDATE;
        SELECT w.* INTO v_owner
        FROM public.worker_incarnations AS w
        WHERE w.incarnation = v_operation.worker_incarnation
        FOR UPDATE;
        IF v_operation.state <> 'exited'
           AND coalesce(public.capture_recovery_authorized(v_owner, v_replacement), false) THEN
            RETURN QUERY SELECT
                v_operation.id,
                v_operation.job_id,
                v_operation.job_attempt,
                v_operation.worker_incarnation,
                v_operation.provider_kind,
                v_operation.resource_id,
                v_operation.system_id,
                v_operation.domain_name,
                CASE WHEN v_operation.state = 'launching' THEN v_operation.launch_token END,
                v_operation.host_instance,
                v_operation.boot_id,
                v_operation.pid,
                v_operation.start_ticks,
                v_operation.state;
        END IF;
    END LOOP;
END
$$;

CREATE FUNCTION public.enforce_capture_protocol_floor()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
BEGIN
    IF NEW.state = 'active' AND NEW.fence_protocol < 3 THEN
        RAISE EXCEPTION 'worker fence protocol 3 is required after capture cutover'
            USING ERRCODE = '22023';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER worker_incarnations_capture_protocol_floor
BEFORE INSERT OR UPDATE ON public.worker_incarnations
FOR EACH ROW EXECUTE FUNCTION public.enforce_capture_protocol_floor();

CREATE OR REPLACE FUNCTION public.register_worker_incarnation(
    p_incarnation text,
    p_authority_kind text,
    p_authority_binding jsonb,
    p_credential_hash bytea,
    p_fence_protocol integer
) RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_lifecycle_witness', 'member') THEN
        RAISE EXCEPTION 'lifecycle witness authority is required' USING ERRCODE = '42501';
    END IF;
    IF p_incarnation IS NULL
       OR octet_length(p_incarnation) NOT BETWEEN 1 AND 512
       OR p_authority_kind NOT IN ('local', 'docker', 'kubernetes')
       OR p_authority_binding IS NULL
       OR jsonb_typeof(p_authority_binding) <> 'object'
       OR octet_length(p_authority_binding::text) > 4096
       OR p_credential_hash IS NULL
       OR octet_length(p_credential_hash) <> 32 THEN
        RAISE EXCEPTION 'worker fence protocol 3 is required after capture cutover'
            USING ERRCODE = '22023';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('kdive:capture-protocol', 1951));
    PERFORM pg_advisory_xact_lock(
        hashtextextended('kdive:worker-incarnation:' || p_incarnation, 1803)
    );
    IF p_fence_protocol IS DISTINCT FROM 3 THEN
        IF EXISTS (
            SELECT 1 FROM public.worker_incarnations WHERE incarnation = p_incarnation
        ) THEN
            RAISE EXCEPTION 'worker incarnation conflicts with durable facts'
                USING ERRCODE = '23505';
        END IF;
        RAISE EXCEPTION 'worker fence protocol 3 is required after capture cutover'
            USING ERRCODE = '22023';
    END IF;
    INSERT INTO public.worker_incarnations (
        incarnation, authority_kind, authority_binding, credential_hash, fence_protocol
    ) VALUES (
        p_incarnation, p_authority_kind, p_authority_binding, p_credential_hash, p_fence_protocol
    ) ON CONFLICT (incarnation) DO NOTHING;
    IF NOT FOUND AND NOT EXISTS (
        SELECT 1 FROM public.worker_incarnations
        WHERE incarnation = p_incarnation
          AND authority_kind = p_authority_kind
          AND authority_binding = p_authority_binding
          AND credential_hash = p_credential_hash
          AND fence_protocol = 3
          AND state = 'active'
        FOR UPDATE
    ) THEN
        RAISE EXCEPTION 'worker incarnation conflicts with durable facts' USING ERRCODE = '23505';
    END IF;
END
$$;

CREATE OR REPLACE FUNCTION public.authenticate_worker_incarnation(p_credential_hash bytea)
RETURNS TABLE (
    incarnation text,
    authority_kind text,
    authority_binding jsonb,
    fence_protocol integer
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_incarnation text;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_worker', 'member') THEN
        RAISE EXCEPTION 'worker authority is required' USING ERRCODE = '42501';
    END IF;
    IF p_credential_hash IS NULL OR octet_length(p_credential_hash) <> 32 THEN
        RAISE EXCEPTION 'worker credential hash is invalid' USING ERRCODE = '22023';
    END IF;
    SELECT w.incarnation INTO v_incarnation
    FROM public.worker_incarnations AS w
    WHERE w.credential_hash = p_credential_hash;
    IF v_incarnation IS NULL THEN
        RETURN;
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended('kdive:worker-incarnation:' || v_incarnation, 1803)
    );
    RETURN QUERY
    SELECT w.incarnation, w.authority_kind, w.authority_binding, w.fence_protocol
    FROM public.worker_incarnations AS w
    WHERE w.incarnation = v_incarnation
      AND w.credential_hash = p_credential_hash
      AND w.state = 'active'
      AND w.fence_protocol = 3
    FOR UPDATE;
END
$$;

CREATE FUNCTION public.create_capture_operation(
    p_credential_hash bytea,
    p_job_id uuid,
    p_job_attempt integer,
    p_provider_kind text,
    p_resource_id uuid,
    p_system_id uuid,
    p_domain_name text,
    p_request_digest text
) RETURNS SETOF public.capture_operations
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_worker public.worker_incarnations%ROWTYPE;
    v_host_instance text;
    v_operation public.capture_operations%ROWTYPE;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_worker', 'member') THEN
        RAISE EXCEPTION 'worker authority is required' USING ERRCODE = '42501';
    END IF;
    IF p_credential_hash IS NULL
       OR octet_length(p_credential_hash) <> 32
       OR p_job_id IS NULL
       OR p_job_attempt IS NULL
       OR p_job_attempt <= 0
       OR p_provider_kind NOT IN ('local-libvirt', 'remote-libvirt')
       OR p_resource_id IS NULL
       OR p_system_id IS NULL
       OR p_domain_name IS NULL
       OR octet_length(p_domain_name) NOT BETWEEN 1 AND 255
       OR p_request_digest !~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'capture operation launch facts are invalid' USING ERRCODE = '22023';
    END IF;
    SELECT w.* INTO v_worker
    FROM public.capture_authenticated_worker(p_credential_hash) AS w;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    v_host_instance := public.capture_worker_host_instance(
        v_worker.authority_kind, v_worker.authority_binding
    );
    IF v_host_instance IS NULL THEN
        RAISE EXCEPTION 'worker incarnation has no durable capture host binding'
            USING ERRCODE = '22023';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('kdive:job:' || p_job_id::text, 1951));
    PERFORM 1
    FROM public.jobs AS j
    JOIN public.runs AS r ON r.id::text = j.payload ->> 'run_id'
    JOIN public.systems AS s ON s.id = r.system_id
    JOIN public.allocations AS a ON a.id = s.allocation_id
    JOIN public.resources AS resource ON resource.id = a.resource_id
    WHERE j.id = p_job_id
      AND j.kind = 'capture_traffic'
      AND j.state = 'running'
      AND j.worker_id = v_worker.incarnation
      AND j.attempt = p_job_attempt
      AND r.target_kind = p_provider_kind
      AND s.id = p_system_id
      AND a.resource_id = p_resource_id
      AND resource.kind = p_provider_kind
      AND s.domain_name = p_domain_name
    FOR UPDATE OF j, r, s, a, resource;
    IF NOT FOUND THEN
        RETURN;
    END IF;

    SELECT o.* INTO v_operation
    FROM public.capture_create_or_replay_operation(
        v_worker.incarnation, p_job_id, p_job_attempt, p_provider_kind, p_resource_id,
        p_system_id, p_domain_name, p_request_digest, v_host_instance
    ) AS o;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    UPDATE public.jobs
    SET current_capture_operation_id = v_operation.id
    WHERE id = p_job_id
      AND attempt = p_job_attempt
      AND worker_id = v_worker.incarnation
      AND (
          current_capture_operation_id IS NULL
          OR current_capture_operation_id = v_operation.id
      );
    IF NOT FOUND THEN
        RETURN;
    END IF;
    RETURN NEXT v_operation;
END
$$;

CREATE FUNCTION public.record_capture_operation_identity(
    p_credential_hash bytea,
    p_operation_id uuid,
    p_host_instance text,
    p_boot_id text,
    p_pid integer,
    p_start_ticks bigint
) RETURNS SETOF public.capture_operations
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_worker text;
    v_operation public.capture_operations%ROWTYPE;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_worker', 'member') THEN
        RAISE EXCEPTION 'worker authority is required' USING ERRCODE = '42501';
    END IF;
    IF p_operation_id IS NULL
       OR p_host_instance IS NULL
       OR octet_length(p_host_instance) NOT BETWEEN 1 AND 512
       OR p_boot_id IS NULL
       OR octet_length(p_boot_id) NOT BETWEEN 1 AND 128
       OR p_pid IS NULL
       OR p_pid <= 0
       OR p_start_ticks IS NULL
       OR p_start_ticks < 0 THEN
        RAISE EXCEPTION 'capture operation identity facts are invalid' USING ERRCODE = '22023';
    END IF;
    SELECT incarnation INTO v_worker FROM public.worker_incarnations
    WHERE credential_hash = p_credential_hash;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended('kdive:worker-incarnation:' || v_worker, 1803)
    );
    PERFORM 1 FROM public.worker_incarnations
    WHERE incarnation = v_worker
      AND credential_hash = p_credential_hash
      AND state = 'active'
      AND fence_protocol = 3
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended('kdive:capture-operation:' || p_operation_id::text, 1951)
    );
    SELECT * INTO v_operation FROM public.capture_operations
    WHERE id = p_operation_id AND worker_incarnation = v_worker
    FOR UPDATE;
    IF NOT FOUND OR v_operation.host_instance <> p_host_instance THEN
        RETURN;
    END IF;
    IF v_operation.state = 'launching' THEN
        UPDATE public.capture_operations
        SET state = 'gated',
            boot_id = p_boot_id,
            pid = p_pid,
            start_ticks = p_start_ticks,
            identity_recorded_at = clock_timestamp(),
            updated_at = clock_timestamp()
        WHERE id = p_operation_id
        RETURNING * INTO v_operation;
    ELSIF v_operation.state <> 'gated'
       OR (v_operation.boot_id, v_operation.pid, v_operation.start_ticks)
          IS DISTINCT FROM (p_boot_id, p_pid, p_start_ticks) THEN
        RETURN;
    END IF;
    RETURN NEXT v_operation;
END
$$;

CREATE FUNCTION public.mark_capture_operation_running(
    p_credential_hash bytea,
    p_operation_id uuid
) RETURNS SETOF public.capture_operations
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_worker text;
    v_operation public.capture_operations%ROWTYPE;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_worker', 'member') THEN
        RAISE EXCEPTION 'worker authority is required' USING ERRCODE = '42501';
    END IF;
    SELECT incarnation INTO v_worker FROM public.worker_incarnations
    WHERE credential_hash = p_credential_hash;
    IF p_operation_id IS NULL OR NOT FOUND THEN
        RETURN;
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended('kdive:worker-incarnation:' || v_worker, 1803)
    );
    PERFORM 1 FROM public.worker_incarnations
    WHERE incarnation = v_worker
      AND credential_hash = p_credential_hash
      AND state = 'active'
      AND fence_protocol = 3
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended('kdive:capture-operation:' || p_operation_id::text, 1951)
    );
    SELECT * INTO v_operation FROM public.capture_operations
    WHERE id = p_operation_id AND worker_incarnation = v_worker
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    IF v_operation.state = 'gated' THEN
        UPDATE public.capture_operations
        SET state = 'running', running_at = clock_timestamp(), updated_at = clock_timestamp()
        WHERE id = p_operation_id
        RETURNING * INTO v_operation;
    ELSIF v_operation.state <> 'running' THEN
        RETURN;
    END IF;
    RETURN NEXT v_operation;
END
$$;

CREATE FUNCTION public.request_capture_operation_cancel(
    p_credential_hash bytea,
    p_operation_id uuid
) RETURNS SETOF public.capture_operations
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_worker text;
    v_operation public.capture_operations%ROWTYPE;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_worker', 'member') THEN
        RAISE EXCEPTION 'worker authority is required' USING ERRCODE = '42501';
    END IF;
    SELECT incarnation INTO v_worker FROM public.worker_incarnations
    WHERE credential_hash = p_credential_hash;
    IF p_operation_id IS NULL OR NOT FOUND THEN
        RETURN;
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended('kdive:worker-incarnation:' || v_worker, 1803)
    );
    PERFORM 1 FROM public.worker_incarnations
    WHERE incarnation = v_worker
      AND credential_hash = p_credential_hash
      AND state = 'active'
      AND fence_protocol = 3
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended('kdive:capture-operation:' || p_operation_id::text, 1951)
    );
    SELECT * INTO v_operation FROM public.capture_operations
    WHERE id = p_operation_id AND worker_incarnation = v_worker
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    IF v_operation.state IN ('gated', 'running') THEN
        UPDATE public.capture_operations
        SET state = 'cancel_requested',
            cancel_requested_at = clock_timestamp(),
            updated_at = clock_timestamp()
        WHERE id = p_operation_id
        RETURNING * INTO v_operation;
    ELSIF v_operation.state <> 'cancel_requested' THEN
        RETURN;
    END IF;
    RETURN NEXT v_operation;
END
$$;

CREATE FUNCTION public.acknowledge_capture_operation_exit(
    p_credential_hash bytea,
    p_operation_id uuid,
    p_process_absent boolean,
    p_provider_quiescence jsonb,
    p_exit_outcome text,
    p_exit_code integer
) RETURNS SETOF public.capture_operations
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_worker text;
    v_operation public.capture_operations%ROWTYPE;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_worker', 'member') THEN
        RAISE EXCEPTION 'worker authority is required' USING ERRCODE = '42501';
    END IF;
    IF p_operation_id IS NULL
       OR p_process_absent IS DISTINCT FROM true
       OR p_provider_quiescence IS NULL
       OR jsonb_typeof(p_provider_quiescence) <> 'object'
       OR p_provider_quiescence = '{}'::jsonb
       OR octet_length(p_provider_quiescence::text) > 4096
       OR p_exit_outcome IS NULL
       OR octet_length(p_exit_outcome) NOT BETWEEN 1 AND 64 THEN
        RAISE EXCEPTION 'capture operation exit evidence is incomplete' USING ERRCODE = '22023';
    END IF;
    SELECT incarnation INTO v_worker FROM public.worker_incarnations
    WHERE credential_hash = p_credential_hash;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended('kdive:worker-incarnation:' || v_worker, 1803)
    );
    PERFORM 1 FROM public.worker_incarnations
    WHERE incarnation = v_worker
      AND credential_hash = p_credential_hash
      AND state = 'active'
      AND fence_protocol = 3
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended('kdive:capture-operation:' || p_operation_id::text, 1951)
    );
    SELECT * INTO v_operation FROM public.capture_operations
    WHERE id = p_operation_id AND worker_incarnation = v_worker
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    IF v_operation.state = 'launching' THEN
        IF p_exit_outcome = 'aborted_before_identity'
           AND NOT coalesce(public.capture_launch_abort_evidence_valid(
               v_operation, p_provider_quiescence
           ), false) THEN
            RETURN;
        ELSIF p_exit_outcome NOT IN ('aborted_before_spawn', 'aborted_before_identity') THEN
            RETURN;
        END IF;
    ELSIF v_operation.state NOT IN (
        'launching', 'gated', 'running', 'cancel_requested', 'exited'
    ) THEN
        RETURN;
    END IF;
    IF v_operation.state = 'exited' THEN
        IF (v_operation.process_absent, v_operation.provider_quiescence,
            v_operation.exit_outcome, v_operation.exit_code)
           IS DISTINCT FROM (p_process_absent, p_provider_quiescence,
               p_exit_outcome, p_exit_code) THEN
            RETURN;
        END IF;
    ELSE
        UPDATE public.capture_operations
        SET state = 'exited',
            process_absent = p_process_absent,
            provider_quiescence = p_provider_quiescence,
            exit_outcome = p_exit_outcome,
            exit_code = p_exit_code,
            exited_at = clock_timestamp(),
            updated_at = clock_timestamp()
        WHERE id = p_operation_id
        RETURNING * INTO v_operation;
    END IF;
    RETURN NEXT v_operation;
END
$$;

CREATE FUNCTION public.recover_capture_operation(
    p_credential_hash bytea,
    p_operation_id uuid,
    p_process_absent boolean,
    p_provider_quiescence jsonb,
    p_exit_outcome text,
    p_exit_code integer
) RETURNS SETOF public.capture_operations
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_context record;
    v_replacement public.worker_incarnations%ROWTYPE;
    v_owner public.worker_incarnations%ROWTYPE;
    v_operation public.capture_operations%ROWTYPE;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_worker', 'member') THEN
        RAISE EXCEPTION 'worker authority is required' USING ERRCODE = '42501';
    END IF;
    IF p_operation_id IS NULL
       OR p_process_absent IS DISTINCT FROM true
       OR p_provider_quiescence IS NULL
       OR jsonb_typeof(p_provider_quiescence) <> 'object'
       OR p_provider_quiescence = '{}'::jsonb
       OR octet_length(p_provider_quiescence::text) > 4096
       OR p_exit_outcome IS NULL
       OR octet_length(p_exit_outcome) NOT BETWEEN 1 AND 64 THEN
        RAISE EXCEPTION 'capture operation recovery evidence is incomplete'
            USING ERRCODE = '22023';
    END IF;
    SELECT context.owner, context.replacement, context.operation INTO v_context
    FROM public.capture_recovery_context(p_credential_hash, p_operation_id) AS context;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    v_owner := v_context.owner;
    v_replacement := v_context.replacement;
    v_operation := v_context.operation;
    IF NOT coalesce(public.capture_recovery_authorized(v_owner, v_replacement), false) THEN
        RETURN;
    END IF;
    IF v_operation.state = 'launching' AND (
        p_exit_outcome <> 'aborted_before_identity'
        OR NOT coalesce(public.capture_launch_abort_evidence_valid(
            v_operation, p_provider_quiescence
        ), false)
    ) THEN
        RETURN;
    END IF;
    IF v_operation.state = 'exited' THEN
        IF v_operation.recovered_by IS DISTINCT FROM v_replacement.incarnation
           OR (v_operation.process_absent, v_operation.provider_quiescence,
               v_operation.exit_outcome, v_operation.exit_code)
              IS DISTINCT FROM (p_process_absent, p_provider_quiescence,
                  p_exit_outcome, p_exit_code) THEN
            RETURN;
        END IF;
    ELSE
        UPDATE public.capture_operations
        SET state = 'exited',
            process_absent = p_process_absent,
            provider_quiescence = p_provider_quiescence,
            exit_outcome = p_exit_outcome,
            exit_code = p_exit_code,
            recovered_by = v_replacement.incarnation,
            exited_at = clock_timestamp(),
            updated_at = clock_timestamp()
        WHERE id = p_operation_id
        RETURNING * INTO v_operation;
    END IF;
    RETURN NEXT v_operation;
END
$$;

CREATE OR REPLACE FUNCTION public.register_kubernetes_worker_incarnation(
    p_incarnation text,
    p_authority_binding jsonb,
    p_credential_hash bytea,
    p_credential_envelope bytea,
    p_fence_protocol integer
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_deadline timestamptz := clock_timestamp() + interval '5 seconds';
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_lifecycle_witness', 'member') THEN
        RAISE EXCEPTION 'lifecycle witness authority is required' USING ERRCODE = '42501';
    END IF;
    IF p_incarnation IS NULL
       OR octet_length(p_incarnation) NOT BETWEEN 1 AND 512
       OR p_authority_binding IS NULL
       OR jsonb_typeof(p_authority_binding) <> 'object'
       OR octet_length(p_authority_binding::text) > 4096
       OR jsonb_typeof(p_authority_binding -> 'namespace') IS DISTINCT FROM 'string'
       OR jsonb_typeof(p_authority_binding -> 'name') IS DISTINCT FROM 'string'
       OR jsonb_typeof(p_authority_binding -> 'uid') IS DISTINCT FROM 'string'
       OR octet_length(p_authority_binding ->> 'namespace') NOT BETWEEN 1 AND 253
       OR octet_length(p_authority_binding ->> 'name') NOT BETWEEN 1 AND 253
       OR octet_length(p_authority_binding ->> 'uid') NOT BETWEEN 1 AND 253
       OR p_incarnation <> (
           'kubernetes:' || (p_authority_binding ->> 'namespace') || ':'
           || (p_authority_binding ->> 'name') || ':' || (p_authority_binding ->> 'uid')
       )
       OR p_credential_hash IS NULL
       OR octet_length(p_credential_hash) <> 32
       OR p_credential_envelope IS NULL
       OR octet_length(p_credential_envelope) NOT BETWEEN 1 AND 4096
       OR p_fence_protocol IS DISTINCT FROM 3 THEN
        RAISE EXCEPTION 'worker fence protocol 3 is required after capture cutover'
            USING ERRCODE = '22023';
    END IF;
    PERFORM set_config('lock_timeout', '5s', true);
    PERFORM set_config('statement_timeout', '5s', true);
    PERFORM pg_advisory_xact_lock(hashtextextended('kdive:capture-protocol', 1951));
    PERFORM pg_advisory_xact_lock(
        hashtextextended('kdive:worker-incarnation:' || p_incarnation, 1803)
    );
    IF clock_timestamp() >= v_deadline THEN
        RAISE EXCEPTION 'Kubernetes credential operation timed out' USING ERRCODE = '57014';
    END IF;
    INSERT INTO public.worker_incarnations (
        incarnation, authority_kind, authority_binding, credential_hash, credential_envelope,
        fence_protocol
    ) VALUES (
        p_incarnation, 'kubernetes', p_authority_binding, p_credential_hash, p_credential_envelope,
        p_fence_protocol
    ) ON CONFLICT (incarnation) DO NOTHING;
    IF FOUND THEN
        RETURN true;
    END IF;
    RETURN EXISTS (
        SELECT 1 FROM public.worker_incarnations
        WHERE incarnation = p_incarnation
          AND authority_kind = 'kubernetes'
          AND authority_binding = p_authority_binding
          AND fence_protocol = 3
          AND state = 'active'
        FOR UPDATE
    );
END
$$;

CREATE OR REPLACE FUNCTION public.enforce_current_worker_fence_protocol()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
BEGIN
    IF NEW.state = 'running'
       AND (
           OLD.state IS DISTINCT FROM 'running'
           OR NEW.worker_id IS DISTINCT FROM OLD.worker_id
           OR NEW.attempt IS DISTINCT FROM OLD.attempt
       )
       AND (
           NEW.worker_id IS NULL
           OR NOT EXISTS (
               SELECT 1 FROM public.worker_incarnations AS w
               WHERE w.incarnation = NEW.worker_id
                 AND w.state = 'active'
                 AND w.fence_protocol = 3
           )
       ) THEN
        RAISE EXCEPTION 'current active worker fence protocol is required for job claim'
            USING ERRCODE = '42501';
    END IF;
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION public.claim_worker_job(
    p_worker_id text,
    p_credential_hash bytea,
    p_lease interval,
    p_accepted_lanes text[]
) RETURNS SETOF public.jobs
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_incarnation text;
    v_lease_deadline timestamptz;
    v_server_time timestamptz;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_worker', 'member') THEN
        RAISE EXCEPTION 'worker authority is required' USING ERRCODE = '42501';
    END IF;
    IF p_worker_id IS NULL
       OR octet_length(p_worker_id) NOT BETWEEN 1 AND 512
       OR p_credential_hash IS NULL
       OR octet_length(p_credential_hash) <> 32
       OR p_accepted_lanes IS NULL THEN
        RAISE EXCEPTION 'worker claim facts are invalid' USING ERRCODE = '22023';
    END IF;
    SELECT w.incarnation INTO v_incarnation
    FROM public.worker_incarnations AS w
    WHERE w.credential_hash = p_credential_hash;
    IF v_incarnation IS NULL OR v_incarnation <> p_worker_id THEN
        RETURN;
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended('kdive:worker-incarnation:' || v_incarnation, 1803)
    );
    PERFORM 1 FROM public.worker_incarnations AS w
    WHERE w.incarnation = v_incarnation
      AND w.credential_hash = p_credential_hash
      AND w.state = 'active'
      AND w.fence_protocol = 3
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    v_server_time := clock_timestamp();
    BEGIN
        v_lease_deadline := v_server_time + p_lease;
    EXCEPTION WHEN datetime_field_overflow THEN
        RAISE EXCEPTION
            'worker claim lease deadline must be after server time and at most 1 hour later; '
            'retry with a valid lease'
            USING ERRCODE = '22023';
    END;
    IF p_lease IS NULL
       OR v_lease_deadline <= v_server_time
       OR v_lease_deadline > v_server_time + interval '1 hour' THEN
        RAISE EXCEPTION
            'worker claim lease deadline must be after server time and at most 1 hour later; '
            'retry with a valid lease'
            USING ERRCODE = '22023';
    END IF;
    RETURN QUERY
    UPDATE public.jobs
    SET state = 'running',
        worker_id = v_incarnation,
        attempt = attempt + 1,
        lease_expires_at = v_lease_deadline,
        heartbeat_at = v_server_time,
        current_capture_operation_id = CASE
            WHEN kind = 'capture_traffic' THEN NULL ELSE current_capture_operation_id
        END
    WHERE id = (
        SELECT j.id
        FROM public.jobs AS j
        WHERE (
            j.state = 'queued'
            OR (j.state = 'running' AND j.lease_expires_at < v_server_time)
        )
          AND j.attempt < j.max_attempts
          AND j.dispatch_lane = ANY(p_accepted_lanes)
          AND (
              j.kind <> 'capture_traffic'
              OR NOT EXISTS (
                  SELECT 1
                  FROM public.capture_operations AS o
                  WHERE o.job_id = j.id
                    AND (
                        o.state <> 'exited'
                        OR NOT o.process_absent
                        OR o.provider_quiescence = '{}'::jsonb
                    )
              )
          )
        ORDER BY j.created_at
        FOR UPDATE OF j SKIP LOCKED
        LIMIT 1
    )
    RETURNING *;
END
$$;

CREATE OR REPLACE FUNCTION public.heartbeat_worker_job(
    p_job_id uuid,
    p_credential_hash bytea,
    p_attempt integer,
    p_lease interval
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_incarnation text;
    v_lease_deadline timestamptz;
    v_server_time timestamptz;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_worker', 'member') THEN
        RAISE EXCEPTION 'worker authority is required' USING ERRCODE = '42501';
    END IF;
    IF p_job_id IS NULL
       OR p_credential_hash IS NULL
       OR octet_length(p_credential_hash) <> 32
       OR p_attempt IS NULL
       OR p_attempt < 1 THEN
        RAISE EXCEPTION 'worker heartbeat facts are invalid' USING ERRCODE = '22023';
    END IF;
    SELECT w.incarnation INTO v_incarnation
    FROM public.worker_incarnations AS w
    WHERE w.credential_hash = p_credential_hash;
    IF v_incarnation IS NULL THEN
        RETURN false;
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended('kdive:worker-incarnation:' || v_incarnation, 1803)
    );
    PERFORM 1 FROM public.worker_incarnations AS w
    WHERE w.incarnation = v_incarnation
      AND w.credential_hash = p_credential_hash
      AND w.state = 'active'
      AND w.fence_protocol = 3
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    PERFORM 1 FROM public.jobs
    WHERE id = p_job_id
      AND worker_id = v_incarnation
      AND attempt = p_attempt
      AND state = 'running'
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    v_server_time := clock_timestamp();
    BEGIN
        v_lease_deadline := v_server_time + p_lease;
    EXCEPTION WHEN datetime_field_overflow THEN
        RAISE EXCEPTION
            'worker heartbeat lease deadline must be after server time and at most 1 hour later; '
            'retry with a valid lease'
            USING ERRCODE = '22023';
    END;
    IF p_lease IS NULL
       OR v_lease_deadline <= v_server_time
       OR v_lease_deadline > v_server_time + interval '1 hour' THEN
        RAISE EXCEPTION
            'worker heartbeat lease deadline must be after server time and at most 1 hour later; '
            'retry with a valid lease'
            USING ERRCODE = '22023';
    END IF;
    UPDATE public.jobs
    SET heartbeat_at = v_server_time, lease_expires_at = v_lease_deadline
    WHERE id = p_job_id
      AND worker_id = v_incarnation
      AND attempt = p_attempt
      AND state = 'running'
    RETURNING lease_expires_at INTO v_lease_deadline;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    UPDATE public.investigation_build_uses
    SET lease_expires_at = v_lease_deadline
    WHERE job_id = p_job_id
      AND holder_worker_id = v_incarnation
      AND attempt = p_attempt;
    RETURN true;
END
$$;

CREATE OR REPLACE FUNCTION public.complete_worker_job(
    p_job_id uuid,
    p_credential_hash bytea,
    p_attempt integer,
    p_result_ref text
) RETURNS SETOF public.jobs
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_incarnation text;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_worker', 'member') THEN
        RAISE EXCEPTION 'worker authority is required' USING ERRCODE = '42501';
    END IF;
    IF p_job_id IS NULL
       OR p_credential_hash IS NULL
       OR octet_length(p_credential_hash) <> 32
       OR p_attempt IS NULL
       OR p_attempt < 1 THEN
        RAISE EXCEPTION 'worker completion facts are invalid' USING ERRCODE = '22023';
    END IF;
    SELECT w.incarnation INTO v_incarnation
    FROM public.worker_incarnations AS w
    WHERE w.credential_hash = p_credential_hash;
    IF v_incarnation IS NULL THEN
        RETURN;
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended('kdive:worker-incarnation:' || v_incarnation, 1803)
    );
    PERFORM 1 FROM public.worker_incarnations AS w
    WHERE w.incarnation = v_incarnation
      AND w.credential_hash = p_credential_hash
      AND w.state = 'active'
      AND w.fence_protocol = 3
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    RETURN QUERY
    UPDATE public.jobs
    SET state = 'succeeded', result_ref = p_result_ref
    WHERE id = p_job_id
      AND worker_id = v_incarnation
      AND attempt = p_attempt
      AND state = 'running'
    RETURNING *;
END
$$;

CREATE OR REPLACE FUNCTION public.fail_worker_job(
    p_job_id uuid,
    p_credential_hash bytea,
    p_attempt integer,
    p_error_category text,
    p_failure_context jsonb,
    p_terminal boolean
) RETURNS SETOF public.jobs
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_incarnation text;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_worker', 'member') THEN
        RAISE EXCEPTION 'worker authority is required' USING ERRCODE = '42501';
    END IF;
    IF p_job_id IS NULL
       OR p_credential_hash IS NULL
       OR octet_length(p_credential_hash) <> 32
       OR p_attempt IS NULL
       OR p_attempt < 1
       OR p_error_category IS NULL
       OR p_failure_context IS NULL
       OR jsonb_typeof(p_failure_context) <> 'object'
       OR p_terminal IS NULL THEN
        RAISE EXCEPTION 'worker failure facts are invalid' USING ERRCODE = '22023';
    END IF;
    SELECT w.incarnation INTO v_incarnation
    FROM public.worker_incarnations AS w
    WHERE w.credential_hash = p_credential_hash;
    IF v_incarnation IS NULL THEN
        RETURN;
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended('kdive:worker-incarnation:' || v_incarnation, 1803)
    );
    PERFORM 1 FROM public.worker_incarnations AS w
    WHERE w.incarnation = v_incarnation
      AND w.credential_hash = p_credential_hash
      AND w.state = 'active'
      AND w.fence_protocol = 3
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN;
    END IF;
    RETURN QUERY
    UPDATE public.jobs
    SET state = CASE WHEN p_terminal OR attempt >= max_attempts THEN 'failed' ELSE 'queued' END,
        error_category = CASE
            WHEN p_terminal OR attempt >= max_attempts THEN p_error_category ELSE NULL
        END,
        failure_context = CASE
            WHEN p_terminal OR attempt >= max_attempts THEN p_failure_context ELSE '{}'::jsonb
        END,
        worker_id = CASE WHEN p_terminal OR attempt >= max_attempts THEN worker_id ELSE NULL END,
        lease_expires_at = CASE
            WHEN p_terminal OR attempt >= max_attempts THEN lease_expires_at ELSE NULL
        END,
        heartbeat_at = CASE
            WHEN p_terminal OR attempt >= max_attempts THEN heartbeat_at ELSE NULL
        END
    WHERE id = p_job_id
      AND worker_id = v_incarnation
      AND attempt = p_attempt
      AND state = 'running'
    RETURNING *;
END
$$;

REVOKE ALL ON TABLE public.capture_operations, public.capture_operation_cutoff FROM PUBLIC;
REVOKE ALL ON TABLE public.capture_operations, public.capture_operation_cutoff
FROM kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness;

REVOKE ALL ON FUNCTION
    public.capture_worker_host_instance(text, jsonb),
    public.capture_worker_authority_scope(text, jsonb),
    public.capture_authenticated_worker(bytea),
    public.capture_create_or_replay_operation(
        text, uuid, integer, text, uuid, uuid, text, text, text
    ),
    public.capture_launch_abort_evidence_valid(public.capture_operations, jsonb),
    public.capture_recovery_authorized(
        public.worker_incarnations, public.worker_incarnations
    ),
    public.capture_recovery_context(bytea, uuid),
    public.list_capture_recovery_candidates(bytea),
    public.enforce_current_capture_operation_link(),
    public.enforce_capture_protocol_floor(),
    public.create_capture_operation(bytea, uuid, integer, text, uuid, uuid, text, text),
    public.record_capture_operation_identity(bytea, uuid, text, text, integer, bigint),
    public.mark_capture_operation_running(bytea, uuid),
    public.request_capture_operation_cancel(bytea, uuid),
    public.acknowledge_capture_operation_exit(bytea, uuid, boolean, jsonb, text, integer),
    public.recover_capture_operation(bytea, uuid, boolean, jsonb, text, integer)
FROM PUBLIC, kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness;

GRANT EXECUTE ON FUNCTION
    public.create_capture_operation(bytea, uuid, integer, text, uuid, uuid, text, text),
    public.record_capture_operation_identity(bytea, uuid, text, text, integer, bigint),
    public.mark_capture_operation_running(bytea, uuid),
    public.request_capture_operation_cancel(bytea, uuid),
    public.acknowledge_capture_operation_exit(bytea, uuid, boolean, jsonb, text, integer),
    public.recover_capture_operation(bytea, uuid, boolean, jsonb, text, integer),
    public.list_capture_recovery_candidates(bytea)
TO kdive_worker;

GRANT SELECT (
    id, job_id, job_attempt, worker_incarnation, provider_kind, resource_id, system_id,
    domain_name, host_instance, boot_id, pid, start_ticks, state, exit_outcome, exit_code,
    process_absent, provider_quiescence, recovered_by, created_at, identity_recorded_at,
    running_at, cancel_requested_at, exited_at, updated_at
) ON public.capture_operations TO kdive_reconciler;

REVOKE ALL ON FUNCTION public.register_worker_incarnation(text, text, jsonb, bytea, integer)
FROM PUBLIC, kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness;
REVOKE ALL ON FUNCTION public.authenticate_worker_incarnation(bytea)
FROM PUBLIC, kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness;
REVOKE ALL ON FUNCTION
    public.register_kubernetes_worker_incarnation(text, jsonb, bytea, bytea, integer)
FROM PUBLIC, kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness;
GRANT EXECUTE ON FUNCTION public.register_worker_incarnation(text, text, jsonb, bytea, integer)
TO kdive_lifecycle_witness;
GRANT EXECUTE ON FUNCTION public.authenticate_worker_incarnation(bytea) TO kdive_worker;
GRANT EXECUTE ON FUNCTION
    public.register_kubernetes_worker_incarnation(text, jsonb, bytea, bytea, integer)
TO kdive_lifecycle_witness;
