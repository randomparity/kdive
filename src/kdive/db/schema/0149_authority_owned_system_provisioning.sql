-- ADR-0623: activation-free authority ownership for initial System provisioning (migration 0149).

CREATE TABLE public.authority_system_ownership (
    system_id uuid PRIMARY KEY REFERENCES public.systems (id) ON DELETE RESTRICT,
    allocation_id uuid NOT NULL REFERENCES public.allocations (id) ON DELETE RESTRICT,
    resource_id uuid NOT NULL REFERENCES public.resources (id) ON DELETE RESTRICT,
    provider_kind text NOT NULL CHECK (provider_kind IN ('local-libvirt', 'remote-libvirt')),
    resource_name text NOT NULL CHECK (
        octet_length(resource_name) BETWEEN 1 AND 255 AND length(btrim(resource_name)) > 0
    ),
    authority_instance text NOT NULL CHECK (
        octet_length(authority_instance) BETWEEN 1 AND 255
        AND length(btrim(authority_instance)) > 0
    ),
    profile_identity text NOT NULL CHECK (profile_identity ~ '^sha256:[0-9a-f]{64}$'),
    root_identity text NOT NULL CHECK (root_identity ~ '^sha256:[0-9a-f]{64}$'),
    bootstrap_identity text CHECK (bootstrap_identity ~ '^sha256:[0-9a-f]{64}$'),
    state text NOT NULL DEFAULT 'provisioning' CHECK (state IN (
        'provisioning', 'ready', 'activated', 'repair-required',
        'teardown-requested', 'torn-down'
    )),
    next_generation bigint NOT NULL DEFAULT 1 CHECK (next_generation > 0),
    current_attempt_id uuid,
    first_activation_id uuid REFERENCES public.external_boot_activations (id) ON DELETE RESTRICT,
    journal_sequence bigint NOT NULL DEFAULT 0 CHECK (journal_sequence >= 0),
    journal_digest text NOT NULL DEFAULT ('sha256:' || repeat('0', 64))
        CHECK (journal_digest ~ '^sha256:[0-9a-f]{64}$'),
    journal_phase text CHECK (journal_phase IN (
        'watermark-installed', 'takeover-superseded', 'takeover-acknowledged',
        'admitted', 'mutation-started', 'provider-returned', 'observed', 'terminal'
    )),
    journal_record jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT authority_system_ownership_journal_shape CHECK (
        (journal_sequence = 0 AND journal_digest = 'sha256:' || repeat('0', 64)
         AND journal_phase IS NULL AND journal_record IS NULL)
        OR (journal_sequence > 0 AND journal_phase IS NOT NULL
            AND jsonb_typeof(journal_record) = 'object')
    ),
    CONSTRAINT authority_system_ownership_activation_shape CHECK (
        (state = 'activated') = (first_activation_id IS NOT NULL)
        OR (state = 'torn-down' AND first_activation_id IS NOT NULL)
    )
);

CREATE TABLE public.authority_system_attempts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    system_id uuid NOT NULL REFERENCES public.authority_system_ownership (system_id)
        ON DELETE RESTRICT,
    generation bigint NOT NULL CHECK (generation > 0),
    operation text NOT NULL CHECK (operation IN ('provision', 'preactivation-teardown')),
    job_id uuid NOT NULL REFERENCES public.jobs (id) ON DELETE RESTRICT,
    job_attempt integer NOT NULL CHECK (job_attempt > 0),
    worker_incarnation text NOT NULL REFERENCES public.worker_incarnations (incarnation)
        ON DELETE RESTRICT,
    request_attempt_id uuid NOT NULL,
    operation_identity text NOT NULL CHECK (
        octet_length(operation_identity) BETWEEN 1 AND 255
        AND length(btrim(operation_identity)) > 0
    ),
    operation_digest text NOT NULL CHECK (operation_digest ~ '^sha256:[0-9a-f]{64}$'),
    state text NOT NULL DEFAULT 'allocating' CHECK (
        state IN ('allocating', 'current', 'superseded', 'terminal')
    ),
    ack_sequence bigint,
    ack_digest text,
    quiescence_digest text,
    acknowledged_at timestamptz,
    ack_head_sequence bigint,
    ack_head_digest text,
    terminal_head_sequence bigint,
    terminal_head_digest text,
    receipt_bytes bytea,
    receipt_digest text,
    receipt_disposition text,
    receipt_at timestamptz,
    consumed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    superseded_at timestamptz,
    UNIQUE (system_id, generation),
    UNIQUE (id, request_attempt_id),
    CONSTRAINT authority_system_attempts_ack_shape CHECK (
        (ack_sequence IS NULL AND ack_digest IS NULL AND quiescence_digest IS NULL
         AND acknowledged_at IS NULL AND ack_head_sequence IS NULL AND ack_head_digest IS NULL)
        OR (ack_sequence > 0 AND ack_digest ~ '^sha256:[0-9a-f]{64}$'
            AND quiescence_digest ~ '^sha256:[0-9a-f]{64}$'
            AND acknowledged_at IS NOT NULL AND ack_head_sequence >= 0
            AND ack_head_digest ~ '^sha256:[0-9a-f]{64}$')
    ),
    CONSTRAINT authority_system_attempts_receipt_shape CHECK (
        (receipt_bytes IS NULL AND receipt_digest IS NULL AND receipt_disposition IS NULL
         AND receipt_at IS NULL AND terminal_head_sequence IS NULL
         AND terminal_head_digest IS NULL AND consumed_at IS NULL)
        OR (octet_length(receipt_bytes) BETWEEN 1 AND 131072
            AND receipt_digest ~ '^sha256:[0-9a-f]{64}$'
            AND receipt_disposition IN (
                'provision-ready', 'preactivation-absent', 'retained-quarantine'
            )
            AND receipt_at IS NOT NULL AND terminal_head_sequence > 0
            AND terminal_head_digest ~ '^sha256:[0-9a-f]{64}$')
    ),
    CONSTRAINT authority_system_attempts_terminal_shape CHECK (
        (state = 'allocating' AND acknowledged_at IS NULL AND superseded_at IS NULL
         AND receipt_bytes IS NULL)
        OR (state = 'current' AND acknowledged_at IS NOT NULL
            AND superseded_at IS NULL AND receipt_bytes IS NULL)
        OR (state = 'superseded' AND superseded_at IS NOT NULL)
        OR (state = 'terminal' AND acknowledged_at IS NOT NULL
            AND receipt_bytes IS NOT NULL AND superseded_at IS NULL)
    )
);

ALTER TABLE public.authority_system_ownership
    ADD CONSTRAINT authority_system_ownership_current_attempt_fkey
    FOREIGN KEY (current_attempt_id) REFERENCES public.authority_system_attempts (id)
    ON DELETE RESTRICT;

CREATE UNIQUE INDEX authority_system_attempt_one_allocating
    ON public.authority_system_attempts (system_id) WHERE state = 'allocating';
CREATE UNIQUE INDEX authority_system_attempt_one_current
    ON public.authority_system_attempts (system_id) WHERE state = 'current';
CREATE INDEX authority_system_attempt_terminal_job_repair
    ON public.authority_system_attempts (job_id)
    WHERE state = 'terminal' AND consumed_at IS NULL;

CREATE FUNCTION public.reject_authority_system_binding_update() RETURNS trigger
LANGUAGE plpgsql SET search_path = '' AS $$
BEGIN
    IF (NEW.system_id, NEW.allocation_id, NEW.resource_id, NEW.provider_kind,
        NEW.resource_name, NEW.authority_instance, NEW.profile_identity,
        NEW.root_identity, NEW.created_at)
       IS DISTINCT FROM
       (OLD.system_id, OLD.allocation_id, OLD.resource_id, OLD.provider_kind,
        OLD.resource_name, OLD.authority_instance, OLD.profile_identity,
        OLD.root_identity, OLD.created_at) THEN
        RAISE EXCEPTION 'authority System ownership binding is immutable';
    END IF;
    IF OLD.bootstrap_identity IS NOT NULL
       AND NEW.bootstrap_identity IS DISTINCT FROM OLD.bootstrap_identity THEN
        RAISE EXCEPTION 'authority System bootstrap identity is immutable';
    END IF;
    IF OLD.first_activation_id IS NOT NULL
       AND NEW.first_activation_id IS DISTINCT FROM OLD.first_activation_id THEN
        RAISE EXCEPTION 'authority System first activation is immutable';
    END IF;
    NEW.updated_at := clock_timestamp();
    RETURN NEW;
END
$$;

CREATE TRIGGER authority_system_ownership_immutable
    BEFORE UPDATE ON public.authority_system_ownership
    FOR EACH ROW EXECUTE FUNCTION public.reject_authority_system_binding_update();

CREATE FUNCTION public.reject_authority_system_attempt_rewrite() RETURNS trigger
LANGUAGE plpgsql SET search_path = '' AS $$
BEGIN
    IF (NEW.id, NEW.system_id, NEW.generation, NEW.operation, NEW.job_id,
        NEW.job_attempt, NEW.worker_incarnation, NEW.request_attempt_id,
        NEW.operation_identity, NEW.operation_digest, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.system_id, OLD.generation, OLD.operation, OLD.job_id,
        OLD.job_attempt, OLD.worker_incarnation, OLD.request_attempt_id,
        OLD.operation_identity, OLD.operation_digest, OLD.created_at) THEN
        RAISE EXCEPTION 'authority System attempt binding is immutable';
    END IF;
    IF OLD.acknowledged_at IS NOT NULL AND
       (NEW.ack_sequence, NEW.ack_digest, NEW.quiescence_digest,
        NEW.acknowledged_at, NEW.ack_head_sequence, NEW.ack_head_digest)
       IS DISTINCT FROM
       (OLD.ack_sequence, OLD.ack_digest, OLD.quiescence_digest,
        OLD.acknowledged_at, OLD.ack_head_sequence, OLD.ack_head_digest) THEN
        RAISE EXCEPTION 'authority System attempt acknowledgement is immutable';
    END IF;
    IF OLD.receipt_bytes IS NOT NULL AND
       (NEW.terminal_head_sequence, NEW.terminal_head_digest, NEW.receipt_bytes,
        NEW.receipt_digest, NEW.receipt_disposition, NEW.receipt_at)
       IS DISTINCT FROM
       (OLD.terminal_head_sequence, OLD.terminal_head_digest, OLD.receipt_bytes,
        OLD.receipt_digest, OLD.receipt_disposition, OLD.receipt_at) THEN
        RAISE EXCEPTION 'authority System attempt receipt is immutable';
    END IF;
    IF OLD.consumed_at IS NOT NULL AND NEW.consumed_at IS DISTINCT FROM OLD.consumed_at THEN
        RAISE EXCEPTION 'authority System attempt consumption is immutable';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER authority_system_attempts_immutable
    BEFORE UPDATE ON public.authority_system_attempts
    FOR EACH ROW EXECUTE FUNCTION public.reject_authority_system_attempt_rewrite();

CREATE FUNCTION public.register_authority_system_ownership(
    p_system_id uuid, p_provision_job_id uuid, p_provider_kind text,
    p_resource_name text, p_authority_instance text, p_profile_identity text,
    p_root_identity text
) RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_system public.systems%ROWTYPE;
    v_allocation public.allocations%ROWTYPE;
    v_resource public.resources%ROWTYPE;
    v_job public.jobs%ROWTYPE;
    v_existing public.authority_system_ownership%ROWTYPE;
    v_marker jsonb;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_server', 'member') THEN
        RAISE EXCEPTION 'server authority is required' USING ERRCODE = '42501';
    END IF;
    IF p_provider_kind NOT IN ('local-libvirt', 'remote-libvirt')
       OR octet_length(p_resource_name) NOT BETWEEN 1 AND 255
       OR octet_length(p_authority_instance) NOT BETWEEN 1 AND 255
       OR p_profile_identity !~ '^sha256:[0-9a-f]{64}$'
       OR p_root_identity !~ '^sha256:[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'authority System ownership binding is invalid' USING ERRCODE = '22023';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('kdive:system:' || p_system_id::text, 2125));
    SELECT * INTO v_system FROM public.systems WHERE id=p_system_id FOR UPDATE;
    SELECT * INTO v_allocation FROM public.allocations WHERE id=v_system.allocation_id FOR UPDATE;
    SELECT * INTO v_resource FROM public.resources WHERE id=v_allocation.resource_id;
    SELECT * INTO v_job FROM public.jobs WHERE id=p_provision_job_id FOR UPDATE;
    SELECT * INTO v_existing FROM public.authority_system_ownership WHERE system_id=p_system_id;
    IF v_existing.system_id IS NOT NULL THEN
        RETURN CASE WHEN v_existing.allocation_id=v_system.allocation_id
                     AND v_existing.resource_id=v_resource.id
                     AND v_existing.provider_kind=p_provider_kind
                     AND v_existing.resource_name=p_resource_name
                     AND v_existing.authority_instance=p_authority_instance
                     AND v_existing.profile_identity=p_profile_identity
                     AND v_existing.root_identity=p_root_identity
                    THEN 'replay' ELSE 'conflict' END;
    END IF;
    v_marker := v_job.payload->'authority_system_v1';
    IF v_system.id IS NULL OR v_system.state <> 'provisioning'
       OR v_allocation.id IS NULL OR v_allocation.state <> 'active'
       OR v_resource.id IS NULL OR v_resource.kind <> p_provider_kind
       OR v_resource.name IS DISTINCT FROM p_resource_name
       OR v_job.id IS NULL OR v_job.kind <> 'provision' OR v_job.state <> 'queued'
       OR jsonb_typeof(v_marker) IS DISTINCT FROM 'object'
       OR v_marker->>'system_id' IS DISTINCT FROM p_system_id::text
       OR v_marker->>'allocation_id' IS DISTINCT FROM v_allocation.id::text
       OR v_marker->>'resource_id' IS DISTINCT FROM v_resource.id::text
       OR v_marker->>'provider_kind' IS DISTINCT FROM p_provider_kind
       OR v_marker->>'resource_name' IS DISTINCT FROM p_resource_name
       OR v_marker->>'authority_instance' IS DISTINCT FROM p_authority_instance
       OR v_marker->>'profile_identity' IS DISTINCT FROM p_profile_identity
       OR v_marker->>'root_identity' IS DISTINCT FROM p_root_identity
       OR v_marker->>'operation' IS DISTINCT FROM 'provision'
       OR NOT EXISTS (
           SELECT 1 FROM public.system_root_provenance AS root
           WHERE root.system_id=p_system_id AND root.image_digest=p_root_identity
       ) OR EXISTS (
           SELECT 1 FROM public.external_boot_activations WHERE system_id=p_system_id
       ) THEN RETURN 'conflict'; END IF;
    INSERT INTO public.authority_system_ownership (
        system_id, allocation_id, resource_id, provider_kind, resource_name,
        authority_instance, profile_identity, root_identity
    ) VALUES (
        p_system_id, v_allocation.id, v_resource.id, p_provider_kind, p_resource_name,
        p_authority_instance, p_profile_identity, p_root_identity
    );
    RETURN 'applied';
END
$$;

CREATE FUNCTION public.request_authority_system_preactivation_teardown(
    p_system_id uuid, p_teardown_job_id uuid, p_operation_identity text
) RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_owner public.authority_system_ownership%ROWTYPE;
    v_job public.jobs%ROWTYPE;
BEGIN
    IF NOT (pg_has_role(session_user, 'kdive_server', 'member')
            OR pg_has_role(session_user, 'kdive_reconciler', 'member')) THEN
        RAISE EXCEPTION 'control authority is required' USING ERRCODE = '42501';
    END IF;
    IF octet_length(p_operation_identity) NOT BETWEEN 1 AND 255 THEN
        RAISE EXCEPTION 'authority System teardown identity is invalid' USING ERRCODE = '22023';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('kdive:system:' || p_system_id::text, 2125));
    SELECT * INTO v_owner FROM public.authority_system_ownership
    WHERE system_id=p_system_id FOR UPDATE;
    SELECT * INTO v_job FROM public.jobs WHERE id=p_teardown_job_id FOR UPDATE;
    IF v_owner.system_id IS NULL THEN RETURN 'conflict'; END IF;
    IF v_owner.first_activation_id IS NOT NULL OR v_owner.state='activated' THEN
        RETURN 'activated';
    END IF;
    IF v_owner.state='teardown-requested' THEN RETURN 'replay'; END IF;
    IF v_owner.state NOT IN ('provisioning','ready','repair-required')
       OR v_job.id IS NULL OR v_job.kind <> 'teardown' OR v_job.state <> 'queued'
       OR v_job.payload #>> '{authority_system_v1,system_id}' IS DISTINCT FROM p_system_id::text
       OR v_job.payload #>> '{authority_system_v1,operation}'
          IS DISTINCT FROM 'preactivation-teardown'
       OR v_job.payload #>> '{authority_system_v1,operation_identity}'
          IS DISTINCT FROM p_operation_identity THEN RETURN 'conflict'; END IF;
    UPDATE public.authority_system_ownership SET state='teardown-requested'
    WHERE system_id=p_system_id;
    RETURN 'applied';
END
$$;

CREATE FUNCTION public.resolve_authority_system_control_binding(p_system_id uuid)
RETURNS TABLE(
    system_id uuid, allocation_id uuid, resource_id uuid, provider_kind text,
    resource_name text, authority_instance text, profile_identity text,
    root_identity text, ownership_state text
) LANGUAGE sql SECURITY DEFINER SET search_path = '' STABLE AS $$
    SELECT owner.system_id,owner.allocation_id,owner.resource_id,owner.provider_kind,
           owner.resource_name,owner.authority_instance,owner.profile_identity,
           owner.root_identity,owner.state
    FROM public.authority_system_ownership AS owner
    WHERE (pg_has_role(session_user,'kdive_server','member')
           OR pg_has_role(session_user,'kdive_reconciler','member'))
      AND owner.system_id=p_system_id
$$;

CREATE FUNCTION public.claim_authority_system_first_activation(
    p_system_id uuid, p_activation_id uuid
) RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE v_owner public.authority_system_ownership%ROWTYPE;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_server', 'member') THEN
        RAISE EXCEPTION 'server authority is required' USING ERRCODE = '42501';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended('kdive:system:' || p_system_id::text, 2125));
    SELECT * INTO v_owner FROM public.authority_system_ownership
    WHERE system_id=p_system_id FOR UPDATE;
    IF v_owner.system_id IS NULL THEN RETURN 'ordinary'; END IF;
    IF v_owner.first_activation_id=p_activation_id AND v_owner.state='activated' THEN
        RETURN 'applied';
    END IF;
    IF v_owner.state <> 'ready' OR v_owner.first_activation_id IS NOT NULL
       OR NOT EXISTS (
           SELECT 1 FROM public.external_boot_activations
           WHERE id=p_activation_id AND system_id=p_system_id
       ) THEN RETURN 'blocked'; END IF;
    UPDATE public.authority_system_ownership
    SET state='activated', first_activation_id=p_activation_id WHERE system_id=p_system_id;
    RETURN 'applied';
END
$$;

CREATE FUNCTION public.allocate_authority_system_attempt(
    p_credential_hash bytea, p_job_id uuid, p_job_attempt integer, p_request_attempt_id uuid
) RETURNS TABLE(status text, authority_id uuid, generation bigint, operation_digest text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_worker text;
    v_job public.jobs%ROWTYPE;
    v_owner public.authority_system_ownership%ROWTYPE;
    v_existing public.authority_system_attempts%ROWTYPE;
    v_marker jsonb;
    v_bootstrap_identity text;
    v_id uuid := gen_random_uuid();
    v_generation bigint;
    v_digest text;
    v_system_id uuid;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_worker', 'member') THEN
        RAISE EXCEPTION 'worker authority is required' USING ERRCODE = '42501';
    END IF;
    SELECT (payload #>> '{authority_system_v1,system_id}')::uuid INTO v_system_id
    FROM public.jobs WHERE id=p_job_id;
    IF v_system_id IS NULL THEN
        RETURN QUERY SELECT 'superseded'::text,NULL::uuid,NULL::bigint,NULL::text; RETURN;
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'kdive:system:' || v_system_id::text, 2125));
    SELECT * INTO v_owner FROM public.authority_system_ownership
    WHERE system_id=v_system_id FOR UPDATE;
    SELECT * INTO v_job FROM public.jobs WHERE id=p_job_id FOR UPDATE;
    SELECT incarnation INTO v_worker FROM public.worker_incarnations
    WHERE credential_hash=p_credential_hash AND state='active' AND fence_protocol=4;
    v_marker := v_job.payload->'authority_system_v1';
    IF v_worker IS NULL OR v_job.id IS NULL OR v_job.state <> 'running'
       OR v_job.worker_id <> v_worker OR v_job.attempt <> p_job_attempt
       OR v_job.lease_expires_at <= clock_timestamp()
       OR jsonb_typeof(v_marker) IS DISTINCT FROM 'object'
       OR v_marker->>'system_id' IS DISTINCT FROM v_system_id::text THEN
        RETURN QUERY SELECT 'superseded'::text,NULL::uuid,NULL::bigint,NULL::text; RETURN;
    END IF;
    SELECT 'sha256:' || encode(sha256(convert_to(public_key,'UTF8')),'hex')
    INTO v_bootstrap_identity FROM public.system_bootstrap_keys
    WHERE system_id=v_owner.system_id;
    IF v_owner.system_id IS NULL OR v_bootstrap_identity IS NULL
       OR v_marker->>'allocation_id' IS DISTINCT FROM v_owner.allocation_id::text
       OR v_marker->>'resource_id' IS DISTINCT FROM v_owner.resource_id::text
       OR v_marker->>'provider_kind' IS DISTINCT FROM v_owner.provider_kind
       OR v_marker->>'resource_name' IS DISTINCT FROM v_owner.resource_name
       OR v_marker->>'authority_instance' IS DISTINCT FROM v_owner.authority_instance
       OR v_marker->>'profile_identity' IS DISTINCT FROM v_owner.profile_identity
       OR v_marker->>'root_identity' IS DISTINCT FROM v_owner.root_identity
       OR NOT (
           (v_marker->>'operation'='provision' AND v_job.kind='provision'
            AND v_owner.state IN ('provisioning','repair-required'))
           OR (v_marker->>'operation'='preactivation-teardown' AND v_job.kind='teardown'
               AND v_owner.state='teardown-requested')
       ) THEN
        RETURN QUERY SELECT 'superseded'::text,NULL::uuid,NULL::bigint,NULL::text; RETURN;
    END IF;
    IF v_owner.bootstrap_identity IS NOT NULL
       AND v_owner.bootstrap_identity <> v_bootstrap_identity THEN
        RETURN QUERY SELECT 'conflict'::text,NULL::uuid,NULL::bigint,NULL::text; RETURN;
    END IF;
    SELECT * INTO v_existing FROM public.authority_system_attempts
    WHERE job_id=p_job_id AND job_attempt=p_job_attempt AND request_attempt_id=p_request_attempt_id;
    IF v_existing.id IS NOT NULL THEN
        RETURN QUERY SELECT CASE WHEN v_existing.state='allocating' THEN 'allocated' ELSE 'replay' END,
            v_existing.id,v_existing.generation,v_existing.operation_digest; RETURN;
    END IF;
    SELECT * INTO v_existing FROM public.authority_system_attempts
    WHERE system_id=v_owner.system_id AND state='allocating' FOR UPDATE;
    IF v_existing.id IS NOT NULL THEN
        IF EXISTS (
            SELECT 1 FROM public.jobs AS incumbent_job
            JOIN public.worker_incarnations AS incumbent_worker
              ON incumbent_worker.incarnation=v_existing.worker_incarnation
            WHERE incumbent_job.id=v_existing.job_id
              AND incumbent_job.state='running'
              AND incumbent_job.attempt=v_existing.job_attempt
              AND incumbent_job.worker_id=v_existing.worker_incarnation
              AND incumbent_job.lease_expires_at>clock_timestamp()
              AND incumbent_worker.state='active'
              AND incumbent_worker.fence_protocol=4
        ) THEN
            RETURN QUERY SELECT 'busy'::text,NULL::uuid,NULL::bigint,NULL::text; RETURN;
        END IF;
        UPDATE public.authority_system_attempts SET state='superseded',
            superseded_at=clock_timestamp() WHERE id=v_existing.id AND state='allocating';
    END IF;
    v_generation := v_owner.next_generation;
    IF v_generation = 9223372036854775807 THEN
        RAISE EXCEPTION 'authority System generation overflow' USING ERRCODE='22003';
    END IF;
    v_digest := 'sha256:' || encode(sha256(convert_to(concat_ws(E'\x1f',
        v_owner.system_id::text,v_owner.allocation_id::text,v_owner.resource_id::text,
        v_owner.provider_kind,v_owner.resource_name,v_owner.authority_instance,
        v_owner.profile_identity,v_owner.root_identity,v_bootstrap_identity,
        v_marker->>'operation',v_marker->>'operation_identity',p_job_id::text,
        p_job_attempt::text,v_worker,p_request_attempt_id::text,v_generation::text),'UTF8')),'hex');
    UPDATE public.authority_system_ownership SET bootstrap_identity=v_bootstrap_identity,
        next_generation=v_generation+1 WHERE system_id=v_owner.system_id;
    INSERT INTO public.authority_system_attempts (
        id,system_id,generation,operation,job_id,job_attempt,worker_incarnation,
        request_attempt_id,operation_identity,operation_digest
    ) VALUES (v_id,v_owner.system_id,v_generation,v_marker->>'operation',p_job_id,
        p_job_attempt,v_worker,p_request_attempt_id,v_marker->>'operation_identity',v_digest);
    RETURN QUERY SELECT 'allocated'::text,v_id,v_generation,v_digest;
END
$$;

CREATE FUNCTION public.acknowledge_authority_system_attempt(
    p_credential_hash bytea, p_job_id uuid, p_job_attempt integer, p_authority_id uuid,
    p_generation bigint, p_request_attempt_id uuid, p_ack_sequence bigint,
    p_ack_digest text, p_quiescence_digest text
) RETURNS TABLE(status text, acknowledged_at timestamptz)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_worker text;
    v_attempt public.authority_system_attempts%ROWTYPE;
    v_owner public.authority_system_ownership%ROWTYPE;
    v_job public.jobs%ROWTYPE;
    v_system_id uuid;
    v_now timestamptz := clock_timestamp();
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_worker', 'member') THEN
        RAISE EXCEPTION 'worker authority is required' USING ERRCODE='42501';
    END IF;
    IF p_ack_sequence < 1 OR p_ack_digest !~ '^sha256:[0-9a-f]{64}$'
       OR p_quiescence_digest !~ '^sha256:[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'authority System acknowledgement is invalid' USING ERRCODE='22023';
    END IF;
    SELECT system_id INTO v_system_id FROM public.authority_system_attempts
    WHERE id=p_authority_id AND generation=p_generation;
    IF v_system_id IS NULL THEN
        RETURN QUERY SELECT 'superseded'::text,NULL::timestamptz; RETURN;
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'kdive:system:' || v_system_id::text,2125));
    SELECT * INTO v_owner FROM public.authority_system_ownership
    WHERE system_id=v_system_id FOR UPDATE;
    SELECT * INTO v_attempt FROM public.authority_system_attempts
    WHERE id=p_authority_id AND generation=p_generation FOR UPDATE;
    SELECT * INTO v_job FROM public.jobs WHERE id=p_job_id FOR UPDATE;
    SELECT incarnation INTO v_worker FROM public.worker_incarnations
    WHERE credential_hash=p_credential_hash AND state='active' AND fence_protocol=4;
    IF v_attempt.id IS NULL OR v_worker IS NULL
       OR v_attempt.worker_incarnation <> v_worker OR v_attempt.job_id <> p_job_id
       OR v_attempt.job_attempt <> p_job_attempt
       OR v_attempt.request_attempt_id <> p_request_attempt_id THEN
        RETURN QUERY SELECT 'superseded'::text,NULL::timestamptz; RETURN;
    END IF;
    IF v_attempt.state IN ('current','terminal') THEN
        RETURN QUERY SELECT CASE WHEN v_attempt.ack_sequence=p_ack_sequence
                                   AND v_attempt.ack_digest=p_ack_digest
                                   AND v_attempt.quiescence_digest=p_quiescence_digest
                                  THEN 'replay' ELSE 'conflict' END,
            v_attempt.acknowledged_at; RETURN;
    END IF;
    IF v_attempt.state <> 'allocating' OR p_ack_sequence <> v_owner.journal_sequence
       OR p_ack_digest <> v_owner.journal_digest
       OR v_owner.journal_phase <> 'takeover-acknowledged'
       OR (v_owner.journal_record->>'authority_id')::uuid <> v_attempt.id
       OR (v_owner.journal_record->>'generation')::bigint <> v_attempt.generation
       OR v_job.state <> 'running' OR v_job.worker_id <> v_worker
       OR v_job.attempt <> p_job_attempt
       OR v_job.lease_expires_at <= clock_timestamp()
       THEN RETURN QUERY SELECT 'superseded'::text,NULL::timestamptz; RETURN; END IF;
    UPDATE public.authority_system_attempts SET state='superseded',superseded_at=v_now
    WHERE id=v_owner.current_attempt_id AND state='current';
    UPDATE public.authority_system_attempts SET state='current',ack_sequence=p_ack_sequence,
        ack_digest=p_ack_digest,quiescence_digest=p_quiescence_digest,acknowledged_at=v_now,
        ack_head_sequence=p_ack_sequence,ack_head_digest=p_ack_digest
    WHERE id=v_attempt.id;
    UPDATE public.authority_system_ownership SET current_attempt_id=v_attempt.id
    WHERE system_id=v_attempt.system_id;
    RETURN QUERY SELECT 'acknowledged'::text,v_now;
END
$$;

CREATE FUNCTION public.resolve_allocating_authority_system_attempt(
    p_peer_incarnation text, p_authority_id uuid, p_generation bigint
) RETURNS TABLE (
    authority_id uuid, generation bigint, system_id uuid, allocation_id uuid,
    resource_id uuid, provider_kind text, resource_name text, authority_instance text,
    profile_identity text, root_identity text, bootstrap_identity text,
    operation text, operation_identity text, operation_digest text, state text,
    journal_sequence bigint, journal_digest text, journal_phase text,
    journal_record jsonb, project text, provisioning_profile jsonb,
    source_image_id uuid, root_architecture text, root_spec jsonb,
    bootstrap_public_key text, receipt_bytes bytea
)
LANGUAGE sql SECURITY DEFINER SET search_path = '' STABLE AS $$
    SELECT attempt.id,attempt.generation,owner.system_id,owner.allocation_id,owner.resource_id,
           owner.provider_kind,owner.resource_name,owner.authority_instance,
           owner.profile_identity,owner.root_identity,owner.bootstrap_identity,
           attempt.operation,attempt.operation_identity,attempt.operation_digest,attempt.state,
           owner.journal_sequence,owner.journal_digest,owner.journal_phase,owner.journal_record,
           system.project,system.provisioning_profile,root.source_image_id,root.architecture,
           root.root_spec,bootstrap.public_key,attempt.receipt_bytes
    FROM public.authority_system_attempts AS attempt
    JOIN public.authority_system_ownership AS owner ON owner.system_id=attempt.system_id
    JOIN public.worker_incarnations AS worker
      ON worker.incarnation=attempt.worker_incarnation
    JOIN public.jobs AS job ON job.id=attempt.job_id
    JOIN public.systems AS system ON system.id=owner.system_id
    JOIN public.system_root_provenance AS root ON root.system_id=owner.system_id
    JOIN public.system_bootstrap_keys AS bootstrap ON bootstrap.system_id=owner.system_id
    WHERE pg_has_role(session_user,'kdive_provider_authority','member')
      AND attempt.id=p_authority_id AND attempt.generation=p_generation
      AND attempt.worker_incarnation=p_peer_incarnation AND attempt.state='allocating'
      AND worker.state='active' AND worker.fence_protocol=4
      AND job.state='running' AND job.worker_id=worker.incarnation
      AND job.attempt=attempt.job_attempt AND job.lease_expires_at>clock_timestamp()
$$;

CREATE FUNCTION public.resolve_current_authority_system_attempt(
    p_peer_incarnation text, p_authority_id uuid, p_generation bigint,
    p_ack_sequence bigint, p_ack_digest text
) RETURNS TABLE (
    authority_id uuid, generation bigint, system_id uuid, allocation_id uuid,
    resource_id uuid, provider_kind text, resource_name text, authority_instance text,
    profile_identity text, root_identity text, bootstrap_identity text,
    operation text, operation_identity text, operation_digest text,
    state text, journal_sequence bigint, journal_digest text, journal_phase text,
    journal_record jsonb, project text, provisioning_profile jsonb,
    source_image_id uuid, root_architecture text, root_spec jsonb,
    bootstrap_public_key text, receipt_bytes bytea
) LANGUAGE sql SECURITY DEFINER SET search_path = '' STABLE AS $$
    SELECT attempt.id,attempt.generation,owner.system_id,owner.allocation_id,owner.resource_id,
           owner.provider_kind,owner.resource_name,owner.authority_instance,
           owner.profile_identity,owner.root_identity,owner.bootstrap_identity,
           attempt.operation,attempt.operation_identity,attempt.operation_digest,
           attempt.state,owner.journal_sequence,owner.journal_digest,
           owner.journal_phase,owner.journal_record,
           system.project,system.provisioning_profile,root.source_image_id,root.architecture,
           root.root_spec,bootstrap.public_key,attempt.receipt_bytes
    FROM public.authority_system_attempts AS attempt
    JOIN public.authority_system_ownership AS owner
      ON owner.current_attempt_id=attempt.id
    JOIN public.worker_incarnations AS worker
      ON worker.incarnation=attempt.worker_incarnation
    JOIN public.systems AS system ON system.id=owner.system_id
    JOIN public.system_root_provenance AS root ON root.system_id=owner.system_id
    JOIN public.system_bootstrap_keys AS bootstrap ON bootstrap.system_id=owner.system_id
    WHERE pg_has_role(session_user,'kdive_provider_authority','member')
      AND attempt.id=p_authority_id AND attempt.generation=p_generation
      AND attempt.worker_incarnation=p_peer_incarnation
      AND attempt.state IN ('current','terminal')
      AND attempt.ack_sequence=p_ack_sequence AND attempt.ack_digest=p_ack_digest
      AND worker.state='active' AND worker.fence_protocol=4
$$;

CREATE FUNCTION public.read_authority_system_journal_head(
    p_peer_incarnation text, p_authority_id uuid, p_generation bigint,
    p_authority_instance text
) RETURNS TABLE(system_id uuid, sequence bigint, digest text, phase text, record jsonb)
LANGUAGE sql SECURITY DEFINER SET search_path = '' STABLE AS $$
    SELECT owner.system_id,owner.journal_sequence,owner.journal_digest,
           owner.journal_phase,owner.journal_record
    FROM public.authority_system_attempts AS attempt
    JOIN public.authority_system_ownership AS owner ON owner.system_id=attempt.system_id
    JOIN public.worker_incarnations AS worker ON worker.incarnation=attempt.worker_incarnation
    WHERE pg_has_role(session_user,'kdive_provider_authority','member')
      AND attempt.id=p_authority_id AND attempt.generation=p_generation
      AND attempt.worker_incarnation=p_peer_incarnation
      AND owner.authority_instance=p_authority_instance
      AND worker.state='active' AND worker.fence_protocol=4
$$;

CREATE FUNCTION public.advance_authority_system_journal_head(
    p_peer_incarnation text, p_authority_id uuid, p_generation bigint,
    p_expected_sequence bigint, p_expected_digest text, p_record jsonb,
    p_receipt_bytes bytea
) RETURNS TABLE(status text, sequence bigint, digest text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_attempt public.authority_system_attempts%ROWTYPE;
    v_owner public.authority_system_ownership%ROWTYPE;
    v_system_id uuid;
    v_sequence bigint;
    v_digest text;
    v_phase text;
    v_disposition text;
BEGIN
    IF NOT pg_has_role(session_user,'kdive_provider_authority','member') THEN
        RAISE EXCEPTION 'provider authority is required' USING ERRCODE='42501';
    END IF;
    IF p_expected_sequence < 0 OR p_expected_digest !~ '^sha256:[0-9a-f]{64}$'
       OR jsonb_typeof(p_record) IS DISTINCT FROM 'object'
       OR pg_column_size(p_record)>1048576
       OR (SELECT count(*) FROM jsonb_object_keys(p_record))<>22
       OR NOT p_record ?& ARRAY[
           'schema','authority_id','generation','system_id','allocation_id','resource_id',
           'provider_kind','resource_name','authority_instance','profile_identity',
           'root_identity','bootstrap_identity','operation','operation_identity',
           'operation_digest','attempt_id','sequence','previous_digest','phase',
           'observation','outcome','canonical_record'
       ] THEN
        RAISE EXCEPTION 'authority System journal record is invalid' USING ERRCODE='22023';
    END IF;
    SELECT system_id INTO v_system_id FROM public.authority_system_attempts
    WHERE id=p_authority_id AND generation=p_generation;
    IF v_system_id IS NULL THEN
        RETURN QUERY SELECT 'superseded'::text,NULL::bigint,NULL::text; RETURN;
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'kdive:system:' || v_system_id::text,2125));
    SELECT * INTO v_owner FROM public.authority_system_ownership
    WHERE system_id=v_system_id FOR UPDATE;
    SELECT * INTO v_attempt FROM public.authority_system_attempts
    WHERE id=p_authority_id AND generation=p_generation FOR UPDATE;
    IF v_attempt.id IS NULL OR v_attempt.worker_incarnation<>p_peer_incarnation
       OR v_attempt.state NOT IN ('allocating','current') OR NOT EXISTS (
           SELECT 1 FROM public.worker_incarnations WHERE incarnation=p_peer_incarnation
           AND state='active' AND fence_protocol=4
       ) THEN RETURN QUERY SELECT 'superseded'::text,NULL::bigint,NULL::text; RETURN; END IF;
    IF v_owner.journal_sequence<>p_expected_sequence
       OR v_owner.journal_digest<>p_expected_digest THEN
        RETURN QUERY SELECT 'conflict'::text,v_owner.journal_sequence,v_owner.journal_digest; RETURN;
    END IF;
    v_phase := p_record->>'phase';
    v_sequence := p_expected_sequence+1;
    IF (v_attempt.state='allocating' AND v_phase NOT IN
          ('watermark-installed','takeover-superseded','takeover-acknowledged'))
       OR (v_attempt.state='current' AND
          (v_owner.current_attempt_id<>v_attempt.id OR v_phase IN
             ('watermark-installed','takeover-superseded','takeover-acknowledged'))) THEN
        RETURN QUERY SELECT 'superseded'::text,NULL::bigint,NULL::text; RETURN;
    END IF;
    IF NOT (
        (v_phase='watermark-installed' AND v_attempt.state='allocating')
        OR (v_phase='takeover-superseded' AND v_owner.journal_phase='watermark-installed')
        OR (v_phase='takeover-acknowledged' AND
            v_owner.journal_phase IN ('watermark-installed','takeover-superseded'))
        OR (v_phase='admitted' AND v_owner.journal_phase IN
            ('takeover-acknowledged','terminal'))
        OR (v_phase='mutation-started' AND v_owner.journal_phase='admitted')
        OR (v_phase='provider-returned' AND v_owner.journal_phase='mutation-started')
        OR (v_phase='observed' AND v_owner.journal_phase='provider-returned')
        OR (v_phase='terminal' AND v_owner.journal_phase IN ('admitted','observed'))
    ) THEN
        RAISE EXCEPTION 'authority System journal phase transition is invalid'
        USING ERRCODE='22023';
    END IF;
    IF v_phase NOT IN ('watermark-installed','takeover-superseded','takeover-acknowledged',
        'admitted','mutation-started','provider-returned','observed','terminal')
       OR p_record->>'schema'<>'authority-system-journal-v1'
       OR (p_record->>'authority_id')::uuid<>p_authority_id
       OR (p_record->>'generation')::bigint<>p_generation
       OR (p_record->>'system_id')::uuid<>v_attempt.system_id
       OR (p_record->>'allocation_id')::uuid<>v_owner.allocation_id
       OR (p_record->>'resource_id')::uuid<>v_owner.resource_id
       OR p_record->>'provider_kind'<>v_owner.provider_kind
       OR p_record->>'resource_name'<>v_owner.resource_name
       OR p_record->>'authority_instance'<>v_owner.authority_instance
       OR p_record->>'profile_identity'<>v_owner.profile_identity
       OR p_record->>'root_identity'<>v_owner.root_identity
       OR p_record->>'bootstrap_identity'<>v_owner.bootstrap_identity
       OR p_record->>'operation'<>v_attempt.operation
       OR p_record->>'operation_identity'<>v_attempt.operation_identity
       OR (p_record->>'attempt_id')::uuid<>v_attempt.request_attempt_id
       OR (p_record->>'sequence')::bigint<>v_sequence
       OR p_record->>'previous_digest'<>p_expected_digest
       OR p_record->>'operation_digest'<>v_attempt.operation_digest THEN
        RAISE EXCEPTION 'authority System journal binding is invalid' USING ERRCODE='22023';
    END IF;
    IF v_phase<>'watermark-installed' AND (
        (v_owner.journal_record->>'authority_id')::uuid<>v_attempt.id
        OR (v_owner.journal_record->>'generation')::bigint<>v_attempt.generation
    ) THEN
        RAISE EXCEPTION 'authority System journal predecessor binding is invalid'
        USING ERRCODE='22023';
    END IF;
    IF (v_phase='terminal')<>(p_receipt_bytes IS NOT NULL) THEN
        RAISE EXCEPTION 'authority System terminal receipt shape is invalid' USING ERRCODE='22023';
    END IF;
    IF (v_phase IN ('watermark-installed','takeover-superseded',
                    'takeover-acknowledged','admitted','mutation-started','provider-returned')
        AND (p_record->'observation'<>'null'::jsonb OR p_record->'outcome'<>'null'::jsonb))
       OR (v_phase='observed' AND
           (jsonb_typeof(p_record->'observation') IS DISTINCT FROM 'object'
            OR p_record->'outcome'<>'null'::jsonb))
       OR (v_phase='terminal' AND jsonb_typeof(p_record->'outcome')<>'string') THEN
        RAISE EXCEPTION 'authority System journal evidence shape is invalid'
        USING ERRCODE='22023';
    END IF;
    IF jsonb_typeof(p_record->'canonical_record') IS DISTINCT FROM 'string'
       OR octet_length(p_record->>'canonical_record') NOT BETWEEN 2 AND 1048576
       OR (p_record->>'canonical_record')::jsonb IS DISTINCT FROM
          (p_record-'canonical_record') THEN
        RAISE EXCEPTION 'authority System canonical journal record is invalid'
        USING ERRCODE='22023';
    END IF;
    v_digest := 'sha256:' || encode(sha256(
        convert_to('kdive-authority-system-journal-v1','UTF8') || decode('00','hex') ||
        convert_to(p_record->>'canonical_record','UTF8')),'hex');
    IF v_phase='terminal' THEN
        IF octet_length(p_receipt_bytes) NOT BETWEEN 1 AND 131072 THEN
            RAISE EXCEPTION 'authority System receipt size is invalid' USING ERRCODE='22023';
        END IF;
        v_disposition := convert_from(p_receipt_bytes,'UTF8')::jsonb->>'disposition';
        IF v_disposition NOT IN
           ('provision-ready','preactivation-absent','retained-quarantine') THEN
            RAISE EXCEPTION 'authority System receipt disposition is invalid' USING ERRCODE='22023';
        END IF;
        IF p_record #>> '{observation,composite_state}' IS DISTINCT FROM
           'sha256:' || encode(sha256(
               convert_to('kdive-authority-system-proof-v1','UTF8') || decode('00','hex') ||
               p_receipt_bytes),'hex') THEN
            RAISE EXCEPTION 'authority System receipt does not match observation'
            USING ERRCODE='22023';
        END IF;
    END IF;
    UPDATE public.authority_system_ownership SET journal_sequence=v_sequence,
        journal_digest=v_digest,journal_phase=v_phase,journal_record=p_record
    WHERE system_id=v_attempt.system_id;
    IF v_phase='terminal' THEN
        UPDATE public.authority_system_attempts SET state='terminal',
            terminal_head_sequence=v_sequence,terminal_head_digest=v_digest,
            receipt_bytes=p_receipt_bytes,
            receipt_digest='sha256:' || encode(sha256(
                convert_to('kdive-authority-system-proof-v1','UTF8') || decode('00','hex') ||
                p_receipt_bytes),'hex'),receipt_disposition=v_disposition,
            receipt_at=clock_timestamp() WHERE id=v_attempt.id;
    END IF;
    RETURN QUERY SELECT 'advanced'::text,v_sequence,v_digest;
END
$$;

CREATE FUNCTION public.list_authority_system_journal_heads(p_authority_instance text)
RETURNS TABLE(authority_instance text, system_id uuid, sequence bigint, digest text)
LANGUAGE sql SECURITY DEFINER SET search_path = '' STABLE AS $$
    SELECT owner.authority_instance,owner.system_id,owner.journal_sequence,owner.journal_digest
    FROM public.authority_system_ownership AS owner
    WHERE pg_has_role(session_user,'kdive_provider_authority','member')
      AND owner.authority_instance=p_authority_instance
    ORDER BY owner.system_id
$$;

CREATE FUNCTION public.finalize_authority_system_attempt(
    p_credential_hash bytea, p_job_id uuid, p_job_attempt integer,
    p_authority_id uuid, p_generation bigint, p_journal_sequence bigint,
    p_journal_digest text, p_receipt_bytes bytea
) RETURNS TABLE(status text, job_state text, system_state text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_worker text;
    v_attempt public.authority_system_attempts%ROWTYPE;
    v_owner public.authority_system_ownership%ROWTYPE;
    v_job public.jobs%ROWTYPE;
    v_system public.systems%ROWTYPE;
    v_system_id uuid;
BEGIN
    IF NOT pg_has_role(session_user,'kdive_worker','member') THEN
        RAISE EXCEPTION 'worker authority is required' USING ERRCODE='42501';
    END IF;
    SELECT system_id INTO v_system_id FROM public.authority_system_attempts
    WHERE id=p_authority_id AND generation=p_generation;
    IF v_system_id IS NULL THEN
        RETURN QUERY SELECT 'superseded'::text,NULL::text,NULL::text; RETURN;
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'kdive:system:' || v_system_id::text,2125));
    SELECT * INTO v_owner FROM public.authority_system_ownership
    WHERE system_id=v_system_id FOR UPDATE;
    SELECT * INTO v_attempt FROM public.authority_system_attempts
    WHERE id=p_authority_id AND generation=p_generation FOR UPDATE;
    SELECT * INTO v_job FROM public.jobs WHERE id=p_job_id FOR UPDATE;
    SELECT * INTO v_system FROM public.systems WHERE id=v_system_id FOR UPDATE;
    SELECT incarnation INTO v_worker FROM public.worker_incarnations
    WHERE credential_hash=p_credential_hash AND state='active' AND fence_protocol=4;
    IF v_attempt.id IS NULL OR v_worker IS NULL
       OR v_attempt.worker_incarnation<>v_worker OR v_attempt.job_id<>p_job_id
       OR v_attempt.job_attempt<>p_job_attempt THEN
        RETURN QUERY SELECT 'superseded'::text,v_job.state,v_system.state; RETURN;
    END IF;
    IF v_attempt.consumed_at IS NOT NULL THEN
        RETURN QUERY SELECT CASE WHEN v_attempt.receipt_bytes=p_receipt_bytes
                                  AND v_attempt.terminal_head_sequence=p_journal_sequence
                                  AND v_attempt.terminal_head_digest=p_journal_digest
                                 THEN CASE
                                     WHEN v_attempt.receipt_disposition='retained-quarantine'
                                     THEN 'retained' ELSE 'applied'
                                 END
                                 ELSE 'conflict' END,
            v_job.state,v_system.state; RETURN;
    END IF;
    IF v_owner.current_attempt_id<>v_attempt.id OR v_attempt.state<>'terminal'
       OR v_attempt.receipt_bytes<>p_receipt_bytes
       OR v_attempt.terminal_head_sequence<>p_journal_sequence
       OR v_attempt.terminal_head_digest<>p_journal_digest
       OR v_owner.journal_sequence<>p_journal_sequence
       OR v_owner.journal_digest<>p_journal_digest
       OR v_job.state<>'running' OR v_job.worker_id<>v_worker
       OR v_job.attempt<>p_job_attempt OR v_job.lease_expires_at<=clock_timestamp() THEN
        RETURN QUERY SELECT 'superseded'::text,v_job.state,v_system.state; RETURN;
    END IF;
    IF v_attempt.receipt_disposition='retained-quarantine' THEN
        UPDATE public.authority_system_attempts SET state='superseded',
            superseded_at=clock_timestamp(),consumed_at=clock_timestamp()
        WHERE id=v_attempt.id;
        IF v_attempt.operation='provision' AND v_owner.state='teardown-requested' THEN
            UPDATE public.jobs SET state='canceled',worker_id=NULL,lease_expires_at=NULL,
                heartbeat_at=NULL,error_category=NULL,failure_context='{}'::jsonb
            WHERE id=p_job_id AND state='running';
            RETURN QUERY SELECT 'retained'::text,'canceled'::text,v_system.state; RETURN;
        END IF;
        IF NOT (
            (v_attempt.operation='provision'
             AND v_owner.state IN ('provisioning','repair-required'))
            OR (v_attempt.operation='preactivation-teardown'
                AND v_owner.state='teardown-requested')
        ) THEN
            RAISE EXCEPTION 'authority System retained retry state is invalid'
            USING ERRCODE='22023';
        END IF;
        IF v_job.attempt>=v_job.max_attempts AND v_job.max_attempts=2147483647 THEN
            RAISE EXCEPTION 'authority System retained retry attempt limit overflow'
            USING ERRCODE='22003';
        END IF;
        IF v_attempt.operation='provision' THEN
            UPDATE public.authority_system_ownership SET state='repair-required'
            WHERE system_id=v_owner.system_id;
        END IF;
        UPDATE public.jobs SET state='queued',worker_id=NULL,lease_expires_at=NULL,
            heartbeat_at=NULL,error_category=NULL,failure_context='{}'::jsonb,
            max_attempts=CASE WHEN attempt>=max_attempts THEN max_attempts+1 ELSE max_attempts END
        WHERE id=p_job_id AND state='running';
        RETURN QUERY SELECT 'retained'::text,'queued'::text,v_system.state; RETURN;
    ELSIF v_attempt.receipt_disposition='provision-ready'
          AND v_owner.state='provisioning' THEN
        UPDATE public.authority_system_ownership SET state='ready' WHERE system_id=v_owner.system_id;
        UPDATE public.systems SET state='ready' WHERE id=v_owner.system_id AND state='provisioning';
        UPDATE public.allocations SET active_started_at=now()
        WHERE id=v_owner.allocation_id AND active_started_at IS NULL;
        INSERT INTO public.audit_log (
            principal,agent_session,project,tool,object_kind,object_id,transition,args_digest
        ) VALUES (
            v_job.authorizing->>'principal',v_job.authorizing->>'agent_session',v_system.project,
            'systems.provision','systems',v_system.id,'provisioning->ready',
            encode(sha256(convert_to(
                '{"system_id":"' || v_system.id::text || '"}','UTF8'
            )),'hex')
        );
        UPDATE public.jobs SET state='succeeded',result_ref=v_attempt.receipt_digest
        WHERE id=p_job_id AND state='running';
    ELSIF v_attempt.receipt_disposition='preactivation-absent'
          AND v_owner.state='teardown-requested' THEN
        INSERT INTO public.audit_log (
            principal,agent_session,project,tool,object_kind,object_id,transition,args_digest
        ) VALUES (
            v_job.authorizing->>'principal',v_job.authorizing->>'agent_session',v_system.project,
            'systems.teardown','systems',v_system.id,v_system.state || '->torn_down',
            encode(sha256(convert_to(
                '{"system_id":"' || v_system.id::text || '"}','UTF8'
            )),'hex')
        );
        UPDATE public.authority_system_ownership SET state='torn-down'
        WHERE system_id=v_owner.system_id;
        UPDATE public.systems SET state='torn_down' WHERE id=v_owner.system_id
        AND state IN ('provisioning','ready','failed');
        UPDATE public.jobs SET state='succeeded',result_ref=v_attempt.receipt_digest
        WHERE id=p_job_id AND state='running';
    ELSE
        RETURN QUERY SELECT 'conflict'::text,v_job.state,v_system.state; RETURN;
    END IF;
    UPDATE public.authority_system_attempts SET consumed_at=clock_timestamp()
    WHERE id=v_attempt.id;
    SELECT * INTO v_job FROM public.jobs WHERE id=p_job_id;
    SELECT * INTO v_system FROM public.systems WHERE id=v_attempt.system_id;
    RETURN QUERY SELECT 'applied'::text,v_job.state,v_system.state;
END
$$;

CREATE FUNCTION public.repair_terminal_authority_system_attempts(p_limit integer)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_attempt public.authority_system_attempts%ROWTYPE;
    v_owner public.authority_system_ownership%ROWTYPE;
    v_job public.jobs%ROWTYPE;
    v_system public.systems%ROWTYPE;
    v_count integer := 0;
BEGIN
    IF NOT pg_has_role(session_user,'kdive_reconciler','member') THEN
        RAISE EXCEPTION 'reconciler authority is required' USING ERRCODE='42501';
    END IF;
    IF p_limit NOT BETWEEN 1 AND 100 THEN
        RAISE EXCEPTION 'repair limit must be between 1 and 100' USING ERRCODE='22023';
    END IF;
    FOR v_attempt IN
        SELECT attempt.* FROM public.authority_system_attempts AS attempt
        JOIN public.authority_system_ownership AS ownership
          ON ownership.current_attempt_id = attempt.id
        JOIN public.jobs AS job ON job.id=attempt.job_id
        WHERE attempt.state = 'terminal' AND attempt.consumed_at IS NULL
          AND attempt.receipt_disposition='provision-ready'
          AND (job.state<>'running' OR job.lease_expires_at<=clock_timestamp()
               OR NOT EXISTS (
                   SELECT 1 FROM public.worker_incarnations AS worker
                   WHERE worker.incarnation=attempt.worker_incarnation AND worker.state='active'
               ))
        ORDER BY attempt.receipt_at,attempt.id LIMIT p_limit
    LOOP
        PERFORM pg_advisory_xact_lock(hashtextextended(
            'kdive:system:' || v_attempt.system_id::text,2125));
        SELECT * INTO v_owner FROM public.authority_system_ownership
        WHERE system_id=v_attempt.system_id FOR UPDATE;
        SELECT * INTO v_attempt FROM public.authority_system_attempts
        WHERE id=v_attempt.id FOR UPDATE;
        SELECT * INTO v_job FROM public.jobs WHERE id=v_attempt.job_id FOR UPDATE;
        SELECT * INTO v_system FROM public.systems WHERE id=v_attempt.system_id FOR UPDATE;
        IF v_attempt.id IS NULL OR v_attempt.state<>'terminal'
           OR v_attempt.consumed_at IS NOT NULL
           OR v_owner.current_attempt_id<>v_attempt.id
           OR v_owner.journal_sequence<>v_attempt.terminal_head_sequence
           OR v_owner.journal_digest<>v_attempt.terminal_head_digest
           OR v_attempt.receipt_disposition='retained-quarantine'
           OR v_job.id IS NULL
           OR NOT (v_job.state<>'running' OR v_job.lease_expires_at<=clock_timestamp()
                   OR NOT EXISTS (
                       SELECT 1 FROM public.worker_incarnations AS worker
                       WHERE worker.incarnation=v_attempt.worker_incarnation
                       AND worker.state='active'
                   )) THEN
            CONTINUE;
        ELSIF v_attempt.receipt_disposition='provision-ready'
              AND v_owner.state='provisioning' THEN
            UPDATE public.authority_system_ownership SET state='ready'
            WHERE system_id=v_attempt.system_id;
            UPDATE public.systems SET state='ready'
            WHERE id=v_attempt.system_id AND state='provisioning';
            UPDATE public.allocations SET active_started_at=now()
            WHERE id=v_owner.allocation_id AND active_started_at IS NULL;
            INSERT INTO public.audit_log (
                principal,agent_session,project,tool,object_kind,object_id,transition,args_digest
            ) VALUES (
                v_job.authorizing->>'principal',v_job.authorizing->>'agent_session',
                v_system.project,'systems.provision','systems',v_system.id,
                'provisioning->ready',encode(sha256(convert_to(
                    '{"system_id":"' || v_system.id::text || '"}','UTF8'
                )),'hex')
            );
            UPDATE public.jobs SET state='succeeded',result_ref=v_attempt.receipt_digest
            WHERE id=v_attempt.job_id AND state='running';
        ELSIF v_attempt.receipt_disposition='provision-ready'
              AND v_owner.state='teardown-requested' THEN
            NULL; -- Consume the physical fact without reopening a canceled System.
        ELSE
            CONTINUE;
        END IF;
        UPDATE public.authority_system_attempts SET consumed_at=clock_timestamp()
        WHERE id=v_attempt.id AND consumed_at IS NULL;
        IF FOUND THEN v_count := v_count + 1; END IF;
    END LOOP;
    RETURN v_count;
END
$$;

CREATE FUNCTION public.list_authority_system_teardown_repairs(p_limit integer)
RETURNS TABLE(
    authority_id uuid, generation bigint, system_id uuid, journal_sequence bigint,
    journal_digest text, receipt_digest text
) LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' STABLE AS $$
BEGIN
    IF NOT pg_has_role(session_user,'kdive_reconciler','member') THEN
        RAISE EXCEPTION 'reconciler authority is required' USING ERRCODE='42501';
    END IF;
    IF p_limit NOT BETWEEN 1 AND 100 THEN
        RAISE EXCEPTION 'repair limit must be between 1 and 100' USING ERRCODE='22023';
    END IF;
    RETURN QUERY
    SELECT attempt.id,attempt.generation,attempt.system_id,attempt.terminal_head_sequence,
           attempt.terminal_head_digest,attempt.receipt_digest
    FROM public.authority_system_attempts AS attempt
    JOIN public.authority_system_ownership AS ownership
      ON ownership.current_attempt_id=attempt.id
    JOIN public.jobs AS job ON job.id=attempt.job_id
    WHERE attempt.state='terminal' AND attempt.consumed_at IS NULL
      AND attempt.receipt_disposition='preactivation-absent'
      AND ownership.state='teardown-requested'
      AND ownership.journal_sequence=attempt.terminal_head_sequence
      AND ownership.journal_digest=attempt.terminal_head_digest
      AND (
          job.state<>'running' OR job.lease_expires_at<=clock_timestamp()
          OR NOT EXISTS (
              SELECT 1 FROM public.worker_incarnations AS worker
              WHERE worker.incarnation=attempt.worker_incarnation AND worker.state='active'
          )
      )
    ORDER BY attempt.receipt_at,attempt.id
    LIMIT p_limit;
END
$$;

CREATE FUNCTION public.finalize_authority_system_teardown_repair(
    p_authority_id uuid, p_generation bigint, p_system_id uuid, p_journal_sequence bigint,
    p_journal_digest text, p_receipt_digest text
) RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_attempt public.authority_system_attempts%ROWTYPE;
    v_owner public.authority_system_ownership%ROWTYPE;
    v_job public.jobs%ROWTYPE;
    v_system public.systems%ROWTYPE;
BEGIN
    IF NOT pg_has_role(session_user,'kdive_reconciler','member') THEN
        RAISE EXCEPTION 'reconciler authority is required' USING ERRCODE='42501';
    END IF;
    IF p_authority_id IS NULL OR p_system_id IS NULL
       OR p_generation IS NULL OR p_generation<1
       OR p_journal_sequence IS NULL OR p_journal_sequence<1
       OR p_journal_digest IS NULL OR p_receipt_digest IS NULL
       OR p_journal_digest!~'^sha256:[0-9a-f]{64}$'
       OR p_receipt_digest!~'^sha256:[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'authority System teardown repair identity is invalid'
        USING ERRCODE='22023';
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'kdive:system:' || p_system_id::text,2125));
    SELECT * INTO v_owner FROM public.authority_system_ownership
    WHERE system_id=p_system_id FOR UPDATE;
    SELECT * INTO v_attempt FROM public.authority_system_attempts
    WHERE id=p_authority_id AND generation=p_generation AND system_id=p_system_id FOR UPDATE;
    IF v_attempt.id IS NULL THEN RETURN 'superseded'; END IF;
    SELECT * INTO v_job FROM public.jobs WHERE id=v_attempt.job_id FOR UPDATE;
    SELECT * INTO v_system FROM public.systems WHERE id=p_system_id FOR UPDATE;
    IF v_attempt.consumed_at IS NOT NULL THEN
        IF v_attempt.terminal_head_sequence=p_journal_sequence
           AND v_attempt.terminal_head_digest=p_journal_digest
           AND v_attempt.receipt_digest=p_receipt_digest
           AND v_owner.state='torn-down' AND v_system.state='torn_down' THEN
            RETURN 'applied';
        END IF;
        RETURN 'conflict';
    END IF;
    IF v_owner.current_attempt_id<>v_attempt.id OR v_attempt.state<>'terminal'
       OR v_attempt.receipt_disposition<>'preactivation-absent'
       OR v_attempt.terminal_head_sequence<>p_journal_sequence
       OR v_attempt.terminal_head_digest<>p_journal_digest
       OR v_attempt.receipt_digest<>p_receipt_digest
       OR v_owner.journal_sequence<>p_journal_sequence
       OR v_owner.journal_digest<>p_journal_digest
       OR v_owner.state<>'teardown-requested'
       OR v_system.state NOT IN ('provisioning','ready','failed')
       OR v_job.id IS NULL
       OR NOT (
           v_job.state<>'running' OR v_job.lease_expires_at<=clock_timestamp()
           OR NOT EXISTS (
               SELECT 1 FROM public.worker_incarnations AS worker
               WHERE worker.incarnation=v_attempt.worker_incarnation AND worker.state='active'
           )
       ) THEN
        RETURN 'superseded';
    END IF;
    IF EXISTS (SELECT 1 FROM public.snapshots WHERE system_id=p_system_id)
       OR EXISTS (SELECT 1 FROM public.system_bootstrap_keys WHERE system_id=p_system_id)
       OR EXISTS (
           SELECT 1 FROM public.remote_module_attempt_obligations
           WHERE system_id=p_system_id AND mutation_discharged_at IS NULL
       ) THEN
        RETURN 'cleanup-required';
    END IF;
    INSERT INTO public.audit_log (
        principal,agent_session,project,tool,object_kind,object_id,transition,args_digest
    ) VALUES (
        v_job.authorizing->>'principal',v_job.authorizing->>'agent_session',v_system.project,
        'systems.teardown','systems',v_system.id,v_system.state || '->torn_down',
        encode(sha256(convert_to(
            '{"system_id":"' || v_system.id::text || '"}','UTF8'
        )),'hex')
    );
    UPDATE public.authority_system_ownership SET state='torn-down'
    WHERE system_id=p_system_id;
    UPDATE public.systems SET state='torn_down' WHERE id=p_system_id
    AND state IN ('provisioning','ready','failed');
    UPDATE public.jobs SET state='succeeded',result_ref=v_attempt.receipt_digest
    WHERE id=v_attempt.job_id AND state='running';
    UPDATE public.authority_system_attempts SET consumed_at=clock_timestamp()
    WHERE id=v_attempt.id AND consumed_at IS NULL;
    RETURN 'applied';
END
$$;

-- A completed authority receipt is the durable handoff to repair.  Keep its job out of generic
-- worker reclaim until either the worker's still-live exact lease consumes it or the reconciler
-- completes the cleanup-gated repair above.
DO $$
DECLARE
    v_definition text;
    v_old constant text := 'AND j.dispatch_lane = ANY(p_accepted_lanes)';
    v_new constant text := E'AND j.dispatch_lane = ANY(p_accepted_lanes)\n' ||
        E'          AND NOT EXISTS (\n' ||
        E'              SELECT 1 FROM public.authority_system_attempts AS authority_attempt\n' ||
        E'              WHERE authority_attempt.job_id=j.id\n' ||
        E'                AND authority_attempt.state=''terminal''\n' ||
        E'                AND authority_attempt.consumed_at IS NULL\n' ||
        E'          )';
BEGIN
    SELECT pg_get_functiondef(
        'public.claim_worker_job(text,bytea,interval,text[])'::regprocedure
    ) INTO v_definition;
    IF strpos(v_definition,v_old)=0 THEN
        RAISE EXCEPTION 'worker claim authority System fence source changed';
    END IF;
    v_definition := replace(v_definition,v_old,v_new);
    IF strpos(v_definition,'authority_attempt.consumed_at IS NULL')=0 THEN
        RAISE EXCEPTION 'worker claim authority System fence was not installed';
    END IF;
    EXECUTE v_definition;
END
$$;

-- Migration 0147 predates activation-free System ownership.  Extend its teardown finalizer
-- without editing the accepted migration so an activated owner reaches the same terminal fact as
-- its System in the receipt-consumption transaction.
DO $$
DECLARE
    v_definition text;
    v_old constant text :=
        'UPDATE public.systems SET state = ''torn_down'' WHERE id = v_authority.system_id;';
    v_new constant text :=
        E'UPDATE public.authority_system_ownership SET state = ''torn-down''\n' ||
        E'    WHERE system_id = v_authority.system_id AND state = ''activated'';\n' ||
        '    UPDATE public.systems SET state = ''torn_down'' WHERE id = v_authority.system_id;';
BEGIN
    SELECT pg_get_functiondef(
        'public.finalize_external_boot_authority_teardown('
        'bytea,uuid,integer,uuid,bigint,bigint,text,bytea)'::regprocedure
    ) INTO v_definition;
    IF strpos(v_definition,v_old)=0 THEN
        RAISE EXCEPTION 'external boot System teardown finalizer shape changed';
    END IF;
    v_definition := replace(v_definition,v_old,v_new);
    IF strpos(v_definition,'UPDATE public.authority_system_ownership SET state = ''torn-down''')=0
    THEN
        RAISE EXCEPTION 'authority System teardown transition was not installed';
    END IF;
    EXECUTE v_definition;
END
$$;

-- Generic worker finalizers must never race this receipt-owned lane.
DO $$
DECLARE v_function regprocedure; v_definition text; v_marker constant text := 'AND state = ''running''';
BEGIN
    FOREACH v_function IN ARRAY ARRAY[
        'public.complete_worker_job(uuid,bytea,integer,text)'::regprocedure,
        'public.fail_worker_job(uuid,bytea,integer,text,jsonb,boolean)'::regprocedure
    ] LOOP
        v_definition := pg_get_functiondef(v_function);
        IF position('authority_system_v1' in v_definition)=0 THEN
            IF position(v_marker in v_definition)=0 THEN
                RAISE EXCEPTION 'authority System finalizer fence source changed for %',v_function;
            END IF;
            v_definition := replace(v_definition,v_marker,
                v_marker || E'\n      AND NOT (payload ? ''authority_system_v1'')');
            EXECUTE v_definition;
        END IF;
    END LOOP;
END
$$;

REVOKE ALL ON TABLE public.authority_system_ownership, public.authority_system_attempts
FROM PUBLIC, kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness,
     kdive_provider_authority;

REVOKE ALL ON FUNCTION
    public.register_authority_system_ownership(uuid,uuid,text,text,text,text,text),
    public.request_authority_system_preactivation_teardown(uuid,uuid,text),
    public.claim_authority_system_first_activation(uuid,uuid)
FROM PUBLIC, kdive_worker, kdive_reconciler, kdive_lifecycle_witness, kdive_provider_authority;
GRANT EXECUTE ON FUNCTION
    public.register_authority_system_ownership(uuid,uuid,text,text,text,text,text),
    public.request_authority_system_preactivation_teardown(uuid,uuid,text),
    public.claim_authority_system_first_activation(uuid,uuid)
TO kdive_server;
GRANT EXECUTE ON FUNCTION public.request_authority_system_preactivation_teardown(uuid,uuid,text)
TO kdive_reconciler;

REVOKE ALL ON FUNCTION public.resolve_authority_system_control_binding(uuid)
FROM PUBLIC, kdive_worker, kdive_lifecycle_witness, kdive_provider_authority;
GRANT EXECUTE ON FUNCTION public.resolve_authority_system_control_binding(uuid)
TO kdive_server, kdive_reconciler;

REVOKE ALL ON FUNCTION
    public.allocate_authority_system_attempt(bytea,uuid,integer,uuid),
    public.acknowledge_authority_system_attempt(bytea,uuid,integer,uuid,bigint,uuid,bigint,text,text),
    public.finalize_authority_system_attempt(bytea,uuid,integer,uuid,bigint,bigint,text,bytea)
FROM PUBLIC, kdive_server, kdive_reconciler, kdive_lifecycle_witness, kdive_provider_authority;
GRANT EXECUTE ON FUNCTION
    public.allocate_authority_system_attempt(bytea,uuid,integer,uuid),
    public.acknowledge_authority_system_attempt(bytea,uuid,integer,uuid,bigint,uuid,bigint,text,text),
    public.finalize_authority_system_attempt(bytea,uuid,integer,uuid,bigint,bigint,text,bytea)
TO kdive_worker;

REVOKE ALL ON FUNCTION
    public.resolve_allocating_authority_system_attempt(text,uuid,bigint),
    public.resolve_current_authority_system_attempt(text,uuid,bigint,bigint,text),
    public.read_authority_system_journal_head(text,uuid,bigint,text),
    public.advance_authority_system_journal_head(text,uuid,bigint,bigint,text,jsonb,bytea),
    public.list_authority_system_journal_heads(text)
FROM PUBLIC, kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness;
GRANT EXECUTE ON FUNCTION
    public.resolve_allocating_authority_system_attempt(text,uuid,bigint),
    public.resolve_current_authority_system_attempt(text,uuid,bigint,bigint,text),
    public.read_authority_system_journal_head(text,uuid,bigint,text),
    public.advance_authority_system_journal_head(text,uuid,bigint,bigint,text,jsonb,bytea),
    public.list_authority_system_journal_heads(text)
TO kdive_provider_authority;

REVOKE ALL ON FUNCTION
    public.repair_terminal_authority_system_attempts(integer),
    public.list_authority_system_teardown_repairs(integer),
    public.finalize_authority_system_teardown_repair(uuid,bigint,uuid,bigint,text,text)
FROM PUBLIC, kdive_server, kdive_worker, kdive_lifecycle_witness, kdive_provider_authority;
GRANT EXECUTE ON FUNCTION
    public.repair_terminal_authority_system_attempts(integer),
    public.list_authority_system_teardown_repairs(integer),
    public.finalize_authority_system_teardown_repair(uuid,bigint,uuid,bigint,text,text)
TO kdive_reconciler;

REVOKE ALL ON FUNCTION
    public.reject_authority_system_binding_update(),
    public.reject_authority_system_attempt_rewrite()
FROM PUBLIC, kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness,
     kdive_provider_authority;
