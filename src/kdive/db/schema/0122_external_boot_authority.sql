-- External-boot provider-host authority and core-result fences (ADR-0584, #2125).

-- The provider authority is a capability role, never a login or inheritance boundary.
DO $$
DECLARE
    v_attributes_match boolean;
BEGIN
    SELECT
        NOT r.rolcanlogin
        AND NOT r.rolinherit
        AND NOT r.rolsuper
        AND NOT r.rolcreaterole
        AND NOT r.rolcreatedb
        AND NOT r.rolreplication
        AND NOT r.rolbypassrls
        AND NOT EXISTS (
            SELECT 1 FROM pg_catalog.pg_auth_members AS membership
            WHERE membership.member = r.oid
        )
    INTO v_attributes_match
    FROM pg_catalog.pg_roles AS r
    WHERE r.rolname = 'kdive_provider_authority';

    IF NOT FOUND THEN
        BEGIN
            CREATE ROLE kdive_provider_authority
                NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE
                NOREPLICATION NOBYPASSRLS;
        EXCEPTION
            WHEN unique_violation OR duplicate_object THEN
                NULL;
        END;
        SELECT
            NOT r.rolcanlogin
            AND NOT r.rolinherit
            AND NOT r.rolsuper
            AND NOT r.rolcreaterole
            AND NOT r.rolcreatedb
            AND NOT r.rolreplication
            AND NOT r.rolbypassrls
            AND NOT EXISTS (
                SELECT 1 FROM pg_catalog.pg_auth_members AS membership
                WHERE membership.member = r.oid
            )
        INTO v_attributes_match
        FROM pg_catalog.pg_roles AS r
        WHERE r.rolname = 'kdive_provider_authority';
    END IF;

    IF NOT FOUND OR NOT coalesce(v_attributes_match, false) THEN
        RAISE EXCEPTION
            'runtime role kdive_provider_authority has incompatible attributes or memberships';
    END IF;
    GRANT USAGE ON SCHEMA public TO kdive_provider_authority;
END
$$;

CREATE TABLE public.external_boot_authority_counters (
    system_id       uuid PRIMARY KEY REFERENCES public.systems (id) ON DELETE RESTRICT,
    last_generation bigint NOT NULL DEFAULT 0
        CONSTRAINT external_boot_authority_counter_nonnegative CHECK (last_generation >= 0),
    updated_at      timestamptz NOT NULL DEFAULT clock_timestamp()
);

INSERT INTO public.external_boot_authority_counters (system_id, last_generation)
SELECT
    system_row.id,
    coalesce(max(existing.generation), 0)
FROM public.systems AS system_row
LEFT JOIN (
    SELECT activation.system_id, activation.authority_generation AS generation
    FROM public.external_boot_activations AS activation
    UNION ALL
    SELECT activation.system_id, attempt.authority_generation AS generation
    FROM public.external_boot_recovery_attempts AS attempt
    JOIN public.external_boot_activations AS activation
      ON activation.id = attempt.activation_id
) AS existing ON existing.system_id = system_row.id
GROUP BY system_row.id;

CREATE TABLE public.external_boot_authorities (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    system_id            uuid NOT NULL REFERENCES public.systems (id) ON DELETE RESTRICT,
    allocation_id        uuid NOT NULL REFERENCES public.allocations (id) ON DELETE RESTRICT,
    activation_id        uuid NOT NULL REFERENCES public.external_boot_activations (id)
                             ON DELETE RESTRICT,
    run_id               uuid NOT NULL REFERENCES public.runs (id) ON DELETE RESTRICT,
    plan_identity        text NOT NULL,
    job_id               uuid NOT NULL REFERENCES public.jobs (id) ON DELETE RESTRICT,
    job_attempt          integer NOT NULL,
    purpose              text NOT NULL,
    provider_kind        text NOT NULL,
    authority_instance   text NOT NULL,
    worker_incarnation   text NOT NULL REFERENCES public.worker_incarnations (incarnation)
                             ON DELETE RESTRICT,
    operation            text NOT NULL,
    operation_identity   text NOT NULL,
    operation_digest     text NOT NULL,
    generation           bigint NOT NULL,
    state                text NOT NULL DEFAULT 'allocating',
    created_at           timestamptz NOT NULL DEFAULT clock_timestamp(),
    acknowledged_at      timestamptz,
    superseded_at        timestamptz,
    retired_at           timestamptz,
    CONSTRAINT external_boot_authority_system_generation_key UNIQUE (system_id, generation),
    CONSTRAINT external_boot_authority_id_system_generation_key
        UNIQUE (id, system_id, generation),
    CONSTRAINT external_boot_authority_plan_digest
        CHECK (plan_identity ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT external_boot_authority_job_attempt CHECK (job_attempt > 0),
    CONSTRAINT external_boot_authority_purpose CHECK (
        purpose IN ('activate', 'recover', 'resolve-conflict', 'release', 'teardown')
    ),
    CONSTRAINT external_boot_authority_provider CHECK (
        provider_kind IN ('local-libvirt', 'remote-libvirt')
    ),
    CONSTRAINT external_boot_authority_instance_bounded
        CHECK (octet_length(authority_instance) BETWEEN 1 AND 255),
    CONSTRAINT external_boot_authority_operation CHECK (
        operation IN (
            'activate', 'recover', 'resolve-conflict', 'release', 'cleanup', 'teardown',
            'deadline', 'recovery-attempt', 'fail'
        )
    ),
    CONSTRAINT external_boot_authority_operation_identity_bounded
        CHECK (octet_length(operation_identity) BETWEEN 1 AND 255),
    CONSTRAINT external_boot_authority_operation_digest
        CHECK (operation_digest ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT external_boot_authority_generation CHECK (generation > 0),
    CONSTRAINT external_boot_authority_state
        CHECK (state IN ('allocating', 'current', 'superseded', 'retired')),
    CONSTRAINT external_boot_authority_state_timestamps CHECK (
        (state = 'allocating'
         AND acknowledged_at IS NULL AND superseded_at IS NULL AND retired_at IS NULL)
        OR (state = 'current'
            AND acknowledged_at IS NOT NULL AND superseded_at IS NULL AND retired_at IS NULL)
        OR (state = 'superseded' AND superseded_at IS NOT NULL AND retired_at IS NULL)
        OR (state = 'retired' AND acknowledged_at IS NOT NULL AND retired_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX external_boot_authorities_one_current_per_system
    ON public.external_boot_authorities (system_id) WHERE state = 'current';
CREATE INDEX external_boot_authorities_activation_created_idx
    ON public.external_boot_authorities (activation_id, created_at);
CREATE INDEX external_boot_authorities_job_attempt_idx
    ON public.external_boot_authorities (job_id, job_attempt);

CREATE TABLE public.external_boot_authority_acknowledgements (
    authority_id                uuid PRIMARY KEY,
    system_id                   uuid NOT NULL,
    generation                  bigint NOT NULL,
    authority_instance          text NOT NULL,
    operation_identity          text NOT NULL,
    operation_digest            text NOT NULL,
    journal_sequence            bigint NOT NULL,
    journal_digest              text NOT NULL,
    positive_quiescence_digest  text NOT NULL,
    acknowledged_at             timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT external_boot_authority_ack_binding_fk
        FOREIGN KEY (authority_id, system_id, generation)
        REFERENCES public.external_boot_authorities (id, system_id, generation)
        ON DELETE RESTRICT,
    CONSTRAINT external_boot_authority_ack_generation CHECK (generation > 0),
    CONSTRAINT external_boot_authority_ack_instance_bounded
        CHECK (octet_length(authority_instance) BETWEEN 1 AND 255),
    CONSTRAINT external_boot_authority_ack_operation_identity_bounded
        CHECK (octet_length(operation_identity) BETWEEN 1 AND 255),
    CONSTRAINT external_boot_authority_ack_operation_digest
        CHECK (operation_digest ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT external_boot_authority_ack_journal_sequence CHECK (journal_sequence > 0),
    CONSTRAINT external_boot_authority_ack_journal_digest
        CHECK (journal_digest ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT external_boot_authority_ack_quiescence_digest
        CHECK (positive_quiescence_digest ~ '^sha256:[0-9a-f]{64}$')
);

CREATE TABLE public.external_boot_authority_audit (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    authority_id        uuid NOT NULL REFERENCES public.external_boot_authorities (id)
                            ON DELETE RESTRICT,
    system_id           uuid NOT NULL REFERENCES public.systems (id) ON DELETE RESTRICT,
    allocation_id       uuid NOT NULL REFERENCES public.allocations (id) ON DELETE RESTRICT,
    activation_id       uuid NOT NULL REFERENCES public.external_boot_activations (id)
                            ON DELETE RESTRICT,
    run_id              uuid NOT NULL REFERENCES public.runs (id) ON DELETE RESTRICT,
    plan_identity       text NOT NULL,
    job_id              uuid NOT NULL REFERENCES public.jobs (id) ON DELETE RESTRICT,
    job_attempt         integer NOT NULL,
    worker_incarnation  text NOT NULL REFERENCES public.worker_incarnations (incarnation)
                            ON DELETE RESTRICT,
    prior_generation    bigint,
    generation          bigint NOT NULL,
    purpose             text NOT NULL,
    provider_kind       text NOT NULL,
    authority_instance  text NOT NULL,
    operation           text NOT NULL,
    operation_identity  text NOT NULL,
    operation_digest    text NOT NULL,
    journal_sequence    bigint,
    journal_digest      text,
    outcome             text NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT external_boot_authority_audit_attempt CHECK (job_attempt > 0),
    CONSTRAINT external_boot_authority_audit_prior_generation
        CHECK (prior_generation IS NULL OR prior_generation > 0),
    CONSTRAINT external_boot_authority_audit_generation CHECK (generation > 0),
    CONSTRAINT external_boot_authority_audit_plan_digest
        CHECK (plan_identity ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT external_boot_authority_audit_purpose CHECK (
        purpose IN ('activate', 'recover', 'resolve-conflict', 'release', 'teardown')
    ),
    CONSTRAINT external_boot_authority_audit_provider CHECK (
        provider_kind IN ('local-libvirt', 'remote-libvirt')
    ),
    CONSTRAINT external_boot_authority_audit_instance_bounded
        CHECK (octet_length(authority_instance) BETWEEN 1 AND 255),
    CONSTRAINT external_boot_authority_audit_operation_identity_bounded
        CHECK (octet_length(operation_identity) BETWEEN 1 AND 255),
    CONSTRAINT external_boot_authority_audit_operation_digest
        CHECK (operation_digest ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT external_boot_authority_audit_journal CHECK (
        (journal_sequence IS NULL AND journal_digest IS NULL)
        OR (journal_sequence > 0 AND journal_digest ~ '^sha256:[0-9a-f]{64}$')
    ),
    CONSTRAINT external_boot_authority_audit_outcome CHECK (
        outcome IN (
            'takeover_allocated', 'authority_acknowledged', 'result_committed',
            'result_failed', 'result_requeued'
        )
    )
);

CREATE INDEX external_boot_authority_audit_activation_created_idx
    ON public.external_boot_authority_audit (activation_id, created_at, id);

CREATE FUNCTION public.reject_external_boot_authority_binding_change() RETURNS trigger
LANGUAGE plpgsql SET search_path = '' AS $$
BEGIN
    IF (
        NEW.id, NEW.system_id, NEW.allocation_id, NEW.activation_id, NEW.run_id,
        NEW.plan_identity, NEW.job_id, NEW.job_attempt, NEW.purpose, NEW.provider_kind,
        NEW.authority_instance, NEW.worker_incarnation, NEW.operation,
        NEW.operation_identity, NEW.operation_digest, NEW.generation, NEW.created_at
    ) IS DISTINCT FROM (
        OLD.id, OLD.system_id, OLD.allocation_id, OLD.activation_id, OLD.run_id,
        OLD.plan_identity, OLD.job_id, OLD.job_attempt, OLD.purpose, OLD.provider_kind,
        OLD.authority_instance, OLD.worker_incarnation, OLD.operation,
        OLD.operation_identity, OLD.operation_digest, OLD.generation, OLD.created_at
    ) THEN
        RAISE EXCEPTION 'external boot authority binding is immutable';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER external_boot_authority_binding_immutable
    BEFORE UPDATE ON public.external_boot_authorities
    FOR EACH ROW EXECUTE FUNCTION public.reject_external_boot_authority_binding_change();

CREATE FUNCTION public.reject_external_boot_authority_immutable_row() RETURNS trigger
LANGUAGE plpgsql SET search_path = '' AS $$
BEGIN
    RAISE EXCEPTION 'external boot authority evidence is immutable';
END
$$;

CREATE TRIGGER external_boot_authority_acknowledgements_immutable
    BEFORE UPDATE OR DELETE ON public.external_boot_authority_acknowledgements
    FOR EACH ROW EXECUTE FUNCTION public.reject_external_boot_authority_immutable_row();
CREATE TRIGGER external_boot_authority_audit_immutable
    BEFORE UPDATE OR DELETE ON public.external_boot_authority_audit
    FOR EACH ROW EXECUTE FUNCTION public.reject_external_boot_authority_immutable_row();

-- Marked work is installed but deliberately not enabled for claim or generic finalization.
DO $$
DECLARE
    v_function regprocedure;
    v_definition text;
    v_marker text;
BEGIN
    FOREACH v_function IN ARRAY ARRAY[
        'public.claim_worker_job(text,bytea,interval,text[])'::regprocedure,
        'public.count_claimable_worker_jobs(text[])'::regprocedure,
        'public.complete_worker_job(uuid,bytea,integer,text)'::regprocedure,
        'public.fail_worker_job(uuid,bytea,integer,text,jsonb,boolean)'::regprocedure
    ] LOOP
        v_definition := pg_get_functiondef(v_function);
        IF v_definition LIKE '%external_boot_authority_v1%' THEN
            RAISE EXCEPTION 'external-boot generic fence replacement is already present for %',
                v_function;
        END IF;
        IF v_function = 'public.claim_worker_job(text,bytea,interval,text[])'::regprocedure
           OR v_function = 'public.count_claimable_worker_jobs(text[])'::regprocedure THEN
            v_marker := 'AND j.attempt < j.max_attempts';
            IF v_definition NOT LIKE '%' || v_marker || '%' THEN
                RAISE EXCEPTION 'external-boot claim replacement has an unexpected source shape';
            END IF;
            v_definition := replace(
                v_definition,
                v_marker,
                E'AND NOT (j.payload ? ''external_boot_authority_v1'')\n          ' || v_marker
            );
        ELSE
            v_marker := 'AND state = ''running''';
            IF v_definition NOT LIKE '%' || v_marker || '%' THEN
                RAISE EXCEPTION
                    'external-boot finalization replacement has an unexpected source shape for %',
                    v_function;
            END IF;
            v_definition := replace(
                v_definition,
                v_marker,
                v_marker || E'\n      AND NOT (payload ? ''external_boot_authority_v1'')'
            );
        END IF;
        EXECUTE v_definition;
    END LOOP;
END
$$;

CREATE FUNCTION public.allocate_external_boot_authority(
    p_credential_hash bytea,
    p_job_id uuid,
    p_attempt integer,
    p_activation_id uuid,
    p_run_id uuid,
    p_system_id uuid,
    p_plan_identity text,
    p_purpose text,
    p_provider_kind text,
    p_authority_instance text,
    p_operation_identity text
) RETURNS TABLE (
    status text,
    authority_id uuid,
    generation bigint,
    operation_digest text
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_activation public.external_boot_activations%ROWTYPE;
    v_allocation public.allocations%ROWTYPE;
    v_authority_id uuid := gen_random_uuid();
    v_generation bigint;
    v_incarnation text;
    v_job public.jobs%ROWTYPE;
    v_marker jsonb;
    v_operation text;
    v_operation_digest text;
    v_prior_generation bigint;
    v_project text;
    v_run public.runs%ROWTYPE;
    v_system public.systems%ROWTYPE;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_worker', 'member') THEN
        RAISE EXCEPTION 'worker authority is required' USING ERRCODE = '42501';
    END IF;
    IF p_credential_hash IS NULL OR octet_length(p_credential_hash) <> 32
       OR p_job_id IS NULL OR p_attempt IS NULL OR p_attempt <= 0
       OR p_activation_id IS NULL OR p_run_id IS NULL OR p_system_id IS NULL
       OR p_plan_identity IS NULL OR p_plan_identity !~ '^sha256:[0-9a-f]{64}$'
       OR p_purpose IS NULL
       OR p_purpose NOT IN ('activate', 'recover', 'resolve-conflict', 'release', 'teardown')
       OR p_provider_kind IS NULL
       OR p_provider_kind NOT IN ('local-libvirt', 'remote-libvirt')
       OR p_authority_instance IS NULL
       OR octet_length(p_authority_instance) NOT BETWEEN 1 AND 255
       OR length(btrim(p_authority_instance)) = 0
       OR p_operation_identity IS NULL
       OR octet_length(p_operation_identity) NOT BETWEEN 1 AND 255
       OR length(btrim(p_operation_identity)) = 0 THEN
        RAISE EXCEPTION 'external boot authority allocation facts are invalid'
            USING ERRCODE = '22023';
    END IF;

    SELECT w.incarnation INTO v_incarnation
    FROM public.worker_incarnations AS w
    WHERE w.credential_hash = p_credential_hash;
    IF v_incarnation IS NULL THEN
        RETURN QUERY SELECT 'superseded'::text, NULL::uuid, NULL::bigint, NULL::text;
        RETURN;
    END IF;

    SELECT a.project INTO v_project
    FROM public.external_boot_activations AS e
    JOIN public.systems AS s ON s.id = e.system_id
    JOIN public.allocations AS a ON a.id = s.allocation_id
    WHERE e.id = p_activation_id;
    IF NOT FOUND THEN
        RETURN QUERY SELECT 'superseded'::text, NULL::uuid, NULL::bigint, NULL::text;
        RETURN;
    END IF;

    PERFORM pg_advisory_xact_lock(hashtextextended('kdive:project:' || v_project, 2125));
    SELECT e.* INTO v_activation
    FROM public.external_boot_activations AS e
    WHERE e.id = p_activation_id;
    IF NOT FOUND THEN
        RETURN QUERY SELECT 'superseded'::text, NULL::uuid, NULL::bigint, NULL::text;
        RETURN;
    END IF;
    SELECT s.* INTO v_system FROM public.systems AS s WHERE s.id = v_activation.system_id;
    SELECT a.* INTO v_allocation
    FROM public.allocations AS a WHERE a.id = v_system.allocation_id;
    PERFORM pg_advisory_xact_lock(
        hashtextextended('kdive:allocation:' || v_allocation.id::text, 2125)
    );
    PERFORM pg_advisory_xact_lock(
        hashtextextended('kdive:system:' || p_system_id::text, 2125)
    );
    PERFORM pg_advisory_xact_lock(hashtextextended('kdive:run:' || p_run_id::text, 2125));
    PERFORM pg_advisory_xact_lock(
        hashtextextended('kdive:worker-incarnation:' || v_incarnation, 1803)
    );

    SELECT w.incarnation INTO v_incarnation
    FROM public.worker_incarnations AS w
    WHERE w.incarnation = v_incarnation
      AND w.credential_hash = p_credential_hash
      AND w.state = 'active'
      AND w.fence_protocol = 4
    FOR UPDATE;
    SELECT e.* INTO v_activation
    FROM public.external_boot_activations AS e
    WHERE e.id = p_activation_id
    FOR UPDATE;
    SELECT s.* INTO v_system
    FROM public.systems AS s WHERE s.id = p_system_id FOR UPDATE;
    SELECT a.* INTO v_allocation
    FROM public.allocations AS a WHERE a.id = v_system.allocation_id FOR UPDATE;
    SELECT r.* INTO v_run FROM public.runs AS r WHERE r.id = p_run_id FOR UPDATE;
    SELECT j.* INTO v_job FROM public.jobs AS j WHERE j.id = p_job_id FOR UPDATE;
    IF v_incarnation IS NULL OR v_activation.id IS NULL OR v_system.id IS NULL
       OR v_allocation.id IS NULL OR v_run.id IS NULL OR v_job.id IS NULL THEN
        RETURN QUERY SELECT 'superseded'::text, NULL::uuid, NULL::bigint, NULL::text;
        RETURN;
    END IF;

    v_marker := v_job.payload -> 'external_boot_authority_v1';
    v_operation := v_marker ->> 'operation';
    IF v_operation IS NULL THEN
        RAISE EXCEPTION 'external boot authority marker operation is invalid'
            USING ERRCODE = '22023';
    END IF;
    IF jsonb_typeof(v_marker) IS DISTINCT FROM 'object'
       OR v_operation NOT IN (
           'activate', 'recover', 'resolve-conflict', 'release', 'cleanup', 'teardown',
           'deadline', 'recovery-attempt', 'fail'
       )
       OR v_allocation.state <> 'active'
       OR v_allocation.project <> v_project
       OR v_system.id <> p_system_id OR v_system.allocation_id <> v_allocation.id
       OR v_run.id <> p_run_id OR v_run.system_id <> p_system_id
       OR v_run.target_kind <> p_provider_kind
       OR v_activation.id <> p_activation_id
       OR v_activation.system_id <> p_system_id OR v_activation.run_id <> p_run_id
       OR v_activation.plan_identity <> p_plan_identity
       OR v_job.state <> 'running' OR v_job.worker_id <> v_incarnation
       OR v_job.attempt <> p_attempt
       OR v_job.lease_expires_at IS NULL OR v_job.lease_expires_at <= clock_timestamp()
       OR v_job.kind <> (CASE WHEN p_purpose = 'teardown' THEN 'teardown' ELSE 'boot' END)
       OR v_job.authorizing ->> 'project' IS DISTINCT FROM v_project
       OR v_marker ->> 'activation_id' IS DISTINCT FROM p_activation_id::text
       OR v_marker ->> 'run_id' IS DISTINCT FROM p_run_id::text
       OR v_marker ->> 'system_id' IS DISTINCT FROM p_system_id::text
       OR v_marker ->> 'plan_identity' IS DISTINCT FROM p_plan_identity
       OR v_marker ->> 'purpose' IS DISTINCT FROM p_purpose
       OR v_marker ->> 'provider_kind' IS DISTINCT FROM p_provider_kind
       OR v_marker ->> 'authority_instance' IS DISTINCT FROM p_authority_instance
       OR v_marker ->> 'operation_identity' IS DISTINCT FROM p_operation_identity
       OR (p_purpose = 'activate' AND v_operation NOT IN ('activate', 'deadline', 'fail'))
       OR (p_purpose = 'recover'
           AND v_operation NOT IN ('recover', 'deadline', 'recovery-attempt', 'fail'))
       OR (p_purpose = 'resolve-conflict'
           AND v_operation NOT IN ('resolve-conflict', 'deadline', 'fail'))
       OR (p_purpose = 'release' AND v_operation NOT IN ('release', 'cleanup', 'fail'))
       OR (p_purpose = 'teardown' AND v_operation NOT IN ('teardown', 'fail'))
       OR (p_purpose = 'activate' AND (
           v_system.state <> 'ready' OR v_run.state <> 'succeeded'
           OR v_activation.state NOT IN ('prepared', 'activating')
       ))
       OR (p_purpose = 'recover' AND (
           v_system.state NOT IN ('ready', 'crashed') OR v_run.state <> 'succeeded'
           OR v_activation.state NOT IN ('active', 'recovering')
       ))
       OR (p_purpose = 'resolve-conflict' AND (
           v_system.state NOT IN ('ready', 'crashed', 'failed')
           OR v_activation.state <> 'recovery_conflict'
       ))
       OR (p_purpose = 'release' AND v_activation.state NOT IN (
           'active', 'recovered', 'abandoned', 'recovery_conflict', 'recovery_failed'
       ))
       OR (p_purpose = 'teardown' AND (
           v_system.state NOT IN ('ready', 'crashed', 'failed')
           OR v_activation.state NOT IN ('recovery_conflict', 'recovery_failed')
       )) THEN
        RETURN QUERY SELECT 'superseded'::text, NULL::uuid, NULL::bigint, NULL::text;
        RETURN;
    END IF;

    SELECT max(a.generation) INTO v_prior_generation
    FROM public.external_boot_authorities AS a
    WHERE a.system_id = p_system_id AND a.state IN ('allocating', 'current');
    INSERT INTO public.external_boot_authority_counters (system_id, last_generation)
    VALUES (p_system_id, 0) ON CONFLICT (system_id) DO NOTHING;
    UPDATE public.external_boot_authority_counters
    SET last_generation = last_generation + 1, updated_at = clock_timestamp()
    WHERE system_id = p_system_id AND last_generation < 9223372036854775807
    RETURNING last_generation INTO v_generation;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'external boot authority generation overflow' USING ERRCODE = '22003';
    END IF;

    v_operation_digest := 'sha256:' || encode(sha256(convert_to(concat_ws(
        E'\x1f', p_system_id::text, v_allocation.id::text, p_activation_id::text,
        p_run_id::text, p_plan_identity, p_job_id::text, p_attempt::text, p_purpose,
        p_provider_kind, p_authority_instance, v_incarnation, v_operation,
        p_operation_identity, v_generation::text
    ), 'UTF8')), 'hex');

    UPDATE public.external_boot_authorities
    SET state = 'superseded', superseded_at = clock_timestamp()
    WHERE system_id = p_system_id AND state IN ('allocating', 'current');
    INSERT INTO public.external_boot_authorities (
        id, system_id, allocation_id, activation_id, run_id, plan_identity, job_id,
        job_attempt, purpose, provider_kind, authority_instance, worker_incarnation,
        operation, operation_identity, operation_digest, generation
    ) VALUES (
        v_authority_id, p_system_id, v_allocation.id, p_activation_id, p_run_id,
        p_plan_identity, p_job_id, p_attempt, p_purpose, p_provider_kind,
        p_authority_instance, v_incarnation, v_operation, p_operation_identity,
        v_operation_digest, v_generation
    );
    INSERT INTO public.external_boot_authority_audit (
        authority_id, system_id, allocation_id, activation_id, run_id, plan_identity, job_id,
        job_attempt, worker_incarnation, prior_generation, generation, purpose,
        provider_kind, authority_instance, operation, operation_identity,
        operation_digest, outcome
    ) VALUES (
        v_authority_id, p_system_id, v_allocation.id, p_activation_id, p_run_id,
        p_plan_identity, p_job_id, p_attempt, v_incarnation, v_prior_generation,
        v_generation, p_purpose,
        p_provider_kind, p_authority_instance, v_operation, p_operation_identity,
        v_operation_digest, 'takeover_allocated'
    );
    RETURN QUERY SELECT 'allocated'::text, v_authority_id, v_generation, v_operation_digest;
END
$$;

CREATE FUNCTION public.acknowledge_external_boot_authority(
    p_authority_id uuid,
    p_generation bigint,
    p_allocation_id uuid,
    p_activation_id uuid,
    p_run_id uuid,
    p_system_id uuid,
    p_plan_identity text,
    p_job_id uuid,
    p_job_attempt integer,
    p_purpose text,
    p_provider_kind text,
    p_authority_instance text,
    p_worker_incarnation text,
    p_operation text,
    p_operation_identity text,
    p_operation_digest text,
    p_journal_sequence bigint,
    p_journal_digest text,
    p_positive_quiescence_digest text
) RETURNS TABLE (
    status text,
    journal_sequence bigint,
    journal_digest text,
    positive_quiescence_digest text,
    acknowledged_at timestamptz
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_authority public.external_boot_authorities%ROWTYPE;
    v_existing public.external_boot_authority_acknowledgements%ROWTYPE;
    v_acknowledged_at timestamptz;
    v_latest bigint;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_provider_authority', 'member') THEN
        RAISE EXCEPTION 'provider authority is required' USING ERRCODE = '42501';
    END IF;
    IF p_authority_id IS NULL OR p_generation IS NULL OR p_generation <= 0
       OR p_allocation_id IS NULL OR p_activation_id IS NULL OR p_run_id IS NULL
       OR p_system_id IS NULL
       OR p_plan_identity IS NULL OR p_plan_identity !~ '^sha256:[0-9a-f]{64}$'
       OR p_job_id IS NULL OR p_job_attempt IS NULL OR p_job_attempt <= 0
       OR p_purpose IS NULL
       OR p_purpose NOT IN ('activate', 'recover', 'resolve-conflict', 'release', 'teardown')
       OR p_provider_kind IS NULL
       OR p_provider_kind NOT IN ('local-libvirt', 'remote-libvirt')
       OR p_authority_instance IS NULL
       OR octet_length(p_authority_instance) NOT BETWEEN 1 AND 255
       OR length(btrim(p_authority_instance)) = 0
       OR p_worker_incarnation IS NULL
       OR octet_length(p_worker_incarnation) NOT BETWEEN 1 AND 512
       OR length(btrim(p_worker_incarnation)) = 0
       OR p_operation IS NULL OR p_operation NOT IN (
           'activate', 'recover', 'resolve-conflict', 'release', 'cleanup', 'teardown',
           'deadline', 'recovery-attempt', 'fail'
       )
       OR p_operation_identity IS NULL
       OR octet_length(p_operation_identity) NOT BETWEEN 1 AND 255
       OR length(btrim(p_operation_identity)) = 0
       OR p_operation_digest IS NULL
       OR p_operation_digest !~ '^sha256:[0-9a-f]{64}$'
       OR p_journal_sequence IS NULL OR p_journal_sequence <= 0
       OR p_journal_digest IS NULL OR p_journal_digest !~ '^sha256:[0-9a-f]{64}$'
       OR p_positive_quiescence_digest IS NULL
       OR p_positive_quiescence_digest !~ '^sha256:[0-9a-f]{64}$' THEN
        RAISE EXCEPTION 'external boot authority acknowledgement facts are invalid'
            USING ERRCODE = '22023';
    END IF;

    PERFORM pg_advisory_xact_lock(
        hashtextextended('kdive:system:' || p_system_id::text, 2125)
    );
    SELECT c.last_generation INTO v_latest
    FROM public.external_boot_authority_counters AS c
    WHERE c.system_id = p_system_id
    FOR UPDATE;
    SELECT a.* INTO v_authority
    FROM public.external_boot_authorities AS a
    WHERE a.id = p_authority_id
    FOR UPDATE;
    SELECT ack.* INTO v_existing
    FROM public.external_boot_authority_acknowledgements AS ack
    WHERE ack.authority_id = p_authority_id
    FOR UPDATE;

    IF v_latest IS NULL OR v_authority.id IS NULL
       OR v_latest <> p_generation
       OR v_authority.generation <> p_generation
       OR v_authority.allocation_id <> p_allocation_id
       OR v_authority.activation_id <> p_activation_id
       OR v_authority.run_id <> p_run_id OR v_authority.system_id <> p_system_id
       OR v_authority.plan_identity <> p_plan_identity
       OR v_authority.job_id <> p_job_id OR v_authority.job_attempt <> p_job_attempt
       OR v_authority.purpose <> p_purpose OR v_authority.provider_kind <> p_provider_kind
       OR v_authority.authority_instance <> p_authority_instance
       OR v_authority.worker_incarnation <> p_worker_incarnation
       OR v_authority.operation <> p_operation
       OR v_authority.operation_identity <> p_operation_identity
       OR v_authority.operation_digest <> p_operation_digest
       OR v_authority.state NOT IN ('allocating', 'current') THEN
        RETURN QUERY SELECT
            'superseded'::text, NULL::bigint, NULL::text, NULL::text, NULL::timestamptz;
        RETURN;
    END IF;

    IF v_existing.authority_id IS NOT NULL THEN
        IF v_authority.state = 'current'
           AND v_existing.system_id = p_system_id
           AND v_existing.generation = p_generation
           AND v_existing.authority_instance = p_authority_instance
           AND v_existing.operation_identity = p_operation_identity
           AND v_existing.operation_digest = p_operation_digest
           AND v_existing.journal_sequence = p_journal_sequence
           AND v_existing.journal_digest = p_journal_digest
           AND v_existing.positive_quiescence_digest = p_positive_quiescence_digest THEN
            RETURN QUERY SELECT
                'applied'::text,
                v_existing.journal_sequence,
                v_existing.journal_digest,
                v_existing.positive_quiescence_digest,
                v_existing.acknowledged_at;
            RETURN;
        END IF;
        RETURN QUERY SELECT
            'superseded'::text, NULL::bigint, NULL::text, NULL::text, NULL::timestamptz;
        RETURN;
    END IF;

    IF v_authority.state <> 'allocating' THEN
        RETURN QUERY SELECT
            'superseded'::text, NULL::bigint, NULL::text, NULL::text, NULL::timestamptz;
        RETURN;
    END IF;
    v_acknowledged_at := clock_timestamp();
    INSERT INTO public.external_boot_authority_acknowledgements (
        authority_id, system_id, generation, authority_instance, operation_identity,
        operation_digest, journal_sequence, journal_digest, positive_quiescence_digest,
        acknowledged_at
    ) VALUES (
        p_authority_id, p_system_id, p_generation, p_authority_instance,
        p_operation_identity, p_operation_digest, p_journal_sequence, p_journal_digest,
        p_positive_quiescence_digest, v_acknowledged_at
    );
    UPDATE public.external_boot_authorities
    SET state = 'current', acknowledged_at = v_acknowledged_at
    WHERE id = p_authority_id AND state = 'allocating';
    INSERT INTO public.external_boot_authority_audit (
        authority_id, system_id, allocation_id, activation_id, run_id, plan_identity, job_id,
        job_attempt, worker_incarnation, generation, purpose, provider_kind,
        authority_instance, operation, operation_identity, operation_digest,
        journal_sequence, journal_digest, outcome
    ) VALUES (
        p_authority_id, p_system_id, p_allocation_id, p_activation_id, p_run_id,
        p_plan_identity, p_job_id, p_job_attempt, p_worker_incarnation, p_generation, p_purpose,
        p_provider_kind, p_authority_instance, p_operation, p_operation_identity,
        p_operation_digest, p_journal_sequence, p_journal_digest,
        'authority_acknowledged'
    );
    RETURN QUERY SELECT
        'applied'::text, p_journal_sequence, p_journal_digest,
        p_positive_quiescence_digest, v_acknowledged_at;
END
$$;

CREATE FUNCTION public.commit_external_boot_authority_result(
    p_credential_hash bytea,
    p_job_id uuid,
    p_attempt integer,
    p_authority_id uuid,
    p_generation bigint,
    p_activation_id uuid,
    p_run_id uuid,
    p_system_id uuid,
    p_plan_identity text,
    p_purpose text,
    p_provider_kind text,
    p_authority_instance text,
    p_operation_identity text,
    p_operation_digest text,
    p_journal_sequence bigint,
    p_journal_digest text,
    p_result jsonb
) RETURNS TABLE (status text, job_state text)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_ack public.external_boot_authority_acknowledgements%ROWTYPE;
    v_activation public.external_boot_activations%ROWTYPE;
    v_attempt public.external_boot_recovery_attempts%ROWTYPE;
    v_authority public.external_boot_authorities%ROWTYPE;
    v_deadline timestamptz;
    v_evidence jsonb;
    v_failure_context jsonb;
    v_has_forbidden_key boolean;
    v_has_unknown_ref boolean;
    v_incarnation text;
    v_job public.jobs%ROWTYPE;
    v_marker jsonb;
    v_operation text;
    v_outcome text := 'result_committed';
    v_release public.external_boot_reservation_releases%ROWTYPE;
    v_result_ref text;
    v_run public.runs%ROWTYPE;
    v_system public.systems%ROWTYPE;
    v_terminal boolean;
    v_timestamp_text text;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_worker', 'member') THEN
        RAISE EXCEPTION 'worker authority is required' USING ERRCODE = '42501';
    END IF;
    IF p_credential_hash IS NULL OR octet_length(p_credential_hash) <> 32
       OR p_job_id IS NULL OR p_attempt IS NULL OR p_attempt <= 0
       OR p_authority_id IS NULL OR p_generation IS NULL OR p_generation <= 0
       OR p_activation_id IS NULL OR p_run_id IS NULL OR p_system_id IS NULL
       OR p_plan_identity IS NULL OR p_plan_identity !~ '^sha256:[0-9a-f]{64}$'
       OR p_purpose IS NULL
       OR p_purpose NOT IN ('activate', 'recover', 'resolve-conflict', 'release', 'teardown')
       OR p_provider_kind IS NULL
       OR p_provider_kind NOT IN ('local-libvirt', 'remote-libvirt')
       OR p_authority_instance IS NULL
       OR octet_length(p_authority_instance) NOT BETWEEN 1 AND 255
       OR length(btrim(p_authority_instance)) = 0
       OR p_operation_identity IS NULL
       OR octet_length(p_operation_identity) NOT BETWEEN 1 AND 255
       OR length(btrim(p_operation_identity)) = 0
       OR p_operation_digest IS NULL
       OR p_operation_digest !~ '^sha256:[0-9a-f]{64}$'
       OR p_journal_sequence IS NULL OR p_journal_sequence <= 0
       OR p_journal_digest IS NULL OR p_journal_digest !~ '^sha256:[0-9a-f]{64}$'
       OR p_result IS NULL OR jsonb_typeof(p_result) <> 'object'
       OR pg_column_size(p_result) > 131072
       OR p_result ->> 'schema' IS DISTINCT FROM 'external-boot-authority-result-v1'
       OR jsonb_typeof(p_result -> 'operation') IS DISTINCT FROM 'string'
       OR p_result ->> 'operation' NOT IN (
           'activate', 'recover', 'resolve-conflict', 'release', 'cleanup', 'teardown',
           'deadline', 'recovery-attempt', 'fail'
       ) THEN
        RAISE EXCEPTION 'external boot authority result facts are invalid'
            USING ERRCODE = '22023';
    END IF;
    v_operation := p_result ->> 'operation';
    v_result_ref := p_result ->> 'result_ref';
    IF (p_result ? 'result_ref'
        AND jsonb_typeof(p_result -> 'result_ref') NOT IN ('string', 'null'))
       OR (v_result_ref IS NOT NULL AND (
           octet_length(v_result_ref) NOT BETWEEN 1 AND 2048
           OR (
               v_result_ref !~ '^sha256:[0-9a-f]{64}$'
               AND v_result_ref
                   !~ '^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$'
           )
       ))
       OR (p_result ? 'evidence' AND (
           jsonb_typeof(p_result -> 'evidence') <> 'object'
           OR pg_column_size(p_result -> 'evidence') > 65536
       ))
       OR (p_result ? 'teardown_evidence' AND (
           jsonb_typeof(p_result -> 'teardown_evidence') <> 'object'
           OR pg_column_size(p_result -> 'teardown_evidence') > 65536
       ))
       OR (p_result ? 'cleanup_evidence' AND (
           jsonb_typeof(p_result -> 'cleanup_evidence') <> 'object'
           OR pg_column_size(p_result -> 'cleanup_evidence') > 65536
       )) THEN
        RAISE EXCEPTION 'external boot authority result payload is invalid'
            USING ERRCODE = '22023';
    END IF;

    SELECT w.incarnation INTO v_incarnation
    FROM public.worker_incarnations AS w
    WHERE w.credential_hash = p_credential_hash;
    IF v_incarnation IS NULL THEN
        RETURN QUERY SELECT 'superseded'::text, NULL::text;
        RETURN;
    END IF;
    PERFORM pg_advisory_xact_lock(
        hashtextextended('kdive:system:' || p_system_id::text, 2125)
    );
    PERFORM pg_advisory_xact_lock(hashtextextended('kdive:run:' || p_run_id::text, 2125));
    PERFORM pg_advisory_xact_lock(
        hashtextextended('kdive:worker-incarnation:' || v_incarnation, 1803)
    );
    SELECT w.incarnation INTO v_incarnation
    FROM public.worker_incarnations AS w
    WHERE w.incarnation = v_incarnation
      AND w.credential_hash = p_credential_hash
      AND w.state = 'active'
      AND w.fence_protocol = 4
    FOR UPDATE;
    SELECT s.* INTO v_system
    FROM public.systems AS s WHERE s.id = p_system_id FOR UPDATE;
    SELECT r.* INTO v_run
    FROM public.runs AS r WHERE r.id = p_run_id FOR UPDATE;
    SELECT j.* INTO v_job FROM public.jobs AS j WHERE j.id = p_job_id FOR UPDATE;
    SELECT a.* INTO v_authority
    FROM public.external_boot_authorities AS a WHERE a.id = p_authority_id FOR UPDATE;
    SELECT e.* INTO v_activation
    FROM public.external_boot_activations AS e WHERE e.id = p_activation_id FOR UPDATE;
    IF v_activation.current_attempt_id IS NOT NULL THEN
        SELECT ra.* INTO v_attempt
        FROM public.external_boot_recovery_attempts AS ra
        WHERE ra.activation_id = p_activation_id
          AND ra.attempt_id = v_activation.current_attempt_id
        FOR UPDATE;
    END IF;
    SELECT ack.* INTO v_ack
    FROM public.external_boot_authority_acknowledgements AS ack
    WHERE ack.authority_id = p_authority_id
    FOR UPDATE;
    SELECT rel.* INTO v_release
    FROM public.external_boot_reservation_releases AS rel
    WHERE rel.activation_id = p_activation_id
    FOR UPDATE;
    v_marker := v_job.payload -> 'external_boot_authority_v1';

    IF v_incarnation IS NULL OR v_system.id IS NULL OR v_run.id IS NULL
       OR v_job.id IS NULL OR v_authority.id IS NULL
       OR v_activation.id IS NULL OR v_ack.authority_id IS NULL
       OR v_system.id <> p_system_id
       OR v_system.allocation_id <> v_authority.allocation_id
       OR v_run.id <> p_run_id OR v_run.system_id <> p_system_id
       OR v_job.state <> 'running' OR v_job.worker_id <> v_incarnation
       OR v_job.attempt <> p_attempt
       OR v_authority.state <> 'current'
       OR v_authority.generation <> p_generation
       OR v_authority.system_id <> p_system_id
       OR v_authority.activation_id <> p_activation_id
       OR v_authority.run_id <> p_run_id
       OR v_authority.plan_identity <> p_plan_identity
       OR v_authority.job_id <> p_job_id OR v_authority.job_attempt <> p_attempt
       OR v_authority.purpose <> p_purpose OR v_authority.provider_kind <> p_provider_kind
       OR v_authority.authority_instance <> p_authority_instance
       OR v_authority.worker_incarnation <> v_incarnation
       OR v_authority.operation <> v_operation
       OR v_authority.operation_identity <> p_operation_identity
       OR v_authority.operation_digest <> p_operation_digest
       OR v_activation.system_id <> p_system_id OR v_activation.run_id <> p_run_id
       OR v_activation.plan_identity <> p_plan_identity
       OR v_marker ->> 'activation_id' IS DISTINCT FROM p_activation_id::text
       OR v_marker ->> 'run_id' IS DISTINCT FROM p_run_id::text
       OR v_marker ->> 'system_id' IS DISTINCT FROM p_system_id::text
       OR v_marker ->> 'plan_identity' IS DISTINCT FROM p_plan_identity
       OR v_marker ->> 'purpose' IS DISTINCT FROM p_purpose
       OR v_marker ->> 'provider_kind' IS DISTINCT FROM p_provider_kind
       OR v_marker ->> 'authority_instance' IS DISTINCT FROM p_authority_instance
       OR v_marker ->> 'operation_identity' IS DISTINCT FROM p_operation_identity
       OR v_ack.system_id <> p_system_id OR v_ack.generation <> p_generation
       OR v_ack.authority_instance <> p_authority_instance
       OR v_ack.operation_identity <> p_operation_identity
       OR v_ack.operation_digest <> p_operation_digest
       OR v_ack.journal_sequence <> p_journal_sequence
       OR v_ack.journal_digest <> p_journal_digest
       OR NOT EXISTS (
           SELECT 1 FROM public.external_boot_authority_counters AS c
           WHERE c.system_id = p_system_id AND c.last_generation = p_generation
           FOR UPDATE
       ) THEN
        RETURN QUERY SELECT 'superseded'::text, NULL::text;
        RETURN;
    END IF;

    WITH RECURSIVE result_nodes(value) AS (
        SELECT p_result
        UNION ALL
        SELECT child.value
        FROM result_nodes AS node
        CROSS JOIN LATERAL (
            SELECT member.value
            FROM jsonb_each(
                CASE WHEN jsonb_typeof(node.value) = 'object'
                     THEN node.value ELSE '{}'::jsonb END
            ) AS member
            UNION ALL
            SELECT member.value
            FROM jsonb_array_elements(
                CASE WHEN jsonb_typeof(node.value) = 'array'
                     THEN node.value ELSE '[]'::jsonb END
            ) AS member
        ) AS child
    )
    SELECT EXISTS (
        SELECT 1
        FROM result_nodes AS node
        CROSS JOIN LATERAL jsonb_object_keys(
            CASE WHEN jsonb_typeof(node.value) = 'object'
                 THEN node.value ELSE '{}'::jsonb END
        ) AS member(key)
        WHERE regexp_replace(lower(member.key), '[^a-z0-9]', '', 'g') ~
            '(credential|secret|password|token|apikey|privatekey|sshkey|command|argv|path|url|definition|xml)'
    ) INTO v_has_forbidden_key;
    IF v_has_forbidden_key THEN
        RAISE EXCEPTION 'external boot authority result contains forbidden fields'
            USING ERRCODE = '22023';
    END IF;

    IF (v_operation = 'activate' AND EXISTS (
           SELECT 1 FROM jsonb_object_keys(p_result) AS field
           WHERE field <> ALL (ARRAY[
               'schema', 'operation', 'result_ref', 'evidence',
               'activation_readiness_deadline'
           ])
       ))
       OR (v_operation IN ('recover', 'resolve-conflict', 'cleanup') AND EXISTS (
           SELECT 1 FROM jsonb_object_keys(p_result) AS field
           WHERE field <> ALL (ARRAY['schema', 'operation', 'result_ref', 'evidence'])
       ))
       OR (v_operation = 'release' AND EXISTS (
           SELECT 1 FROM jsonb_object_keys(p_result) AS field
           WHERE field <> ALL (ARRAY[
               'schema', 'operation', 'result_ref', 'release_identity', 'evidence'
           ])
       ))
       OR (v_operation = 'teardown' AND EXISTS (
           SELECT 1 FROM jsonb_object_keys(p_result) AS field
           WHERE field <> ALL (ARRAY[
               'schema', 'operation', 'result_ref', 'teardown_evidence', 'cleanup_evidence'
           ])
       ))
       OR (v_operation = 'deadline' AND EXISTS (
           SELECT 1 FROM jsonb_object_keys(p_result) AS field
           WHERE field <> ALL (ARRAY['schema', 'operation', 'deadline'])
       ))
       OR (v_operation = 'recovery-attempt' AND EXISTS (
           SELECT 1 FROM jsonb_object_keys(p_result) AS field
           WHERE field <> ALL (ARRAY[
               'schema', 'operation', 'attempt_id', 'recovery_basis', 'deadline'
           ])
       ))
       OR (v_operation = 'fail' AND EXISTS (
           SELECT 1 FROM jsonb_object_keys(p_result) AS field
           WHERE field <> ALL (ARRAY[
               'schema', 'operation', 'error_category', 'failure_context', 'terminal'
           ])
       )) THEN
        RAISE EXCEPTION 'external boot authority result has unexpected fields'
            USING ERRCODE = '22023';
    END IF;

    IF v_operation IN ('activate', 'recover', 'resolve-conflict', 'release', 'cleanup')
       AND EXISTS (
           SELECT 1
           FROM jsonb_object_keys(
               CASE WHEN jsonb_typeof(p_result -> 'evidence') = 'object'
                    THEN p_result -> 'evidence' ELSE '{}'::jsonb END
           ) AS field
           WHERE field <> ALL (
               CASE
                   WHEN v_operation = 'release' THEN ARRAY[
                       'schema', 'activation_id', 'system_id', 'store_identity', 'owner_key',
                       'reserved_bytes', 'enumeration_complete', 'objects', 'verified_at'
                   ]
                   WHEN v_operation = 'cleanup' THEN ARRAY[
                       'schema', 'activation_id', 'system_id', 'release_identity', 'mode',
                       'teardown_identity', 'completed_at'
                   ]
                   ELSE ARRAY[
                       'schema', 'activation_id', 'system_id', 'outcome', 'composite_state',
                       'objects', 'observed_at'
                   ]
               END
           )
       ) THEN
        RAISE EXCEPTION 'external boot authority evidence has unexpected fields'
            USING ERRCODE = '22023';
    END IF;
    IF v_operation = 'teardown'
       AND (
           EXISTS (
               SELECT 1
               FROM jsonb_object_keys(p_result -> 'teardown_evidence') AS field
               WHERE field <> ALL (ARRAY['schema', 'system_id', 'system_state', 'observed_at'])
           )
           OR EXISTS (
               SELECT 1
               FROM jsonb_object_keys(p_result -> 'cleanup_evidence') AS field
               WHERE field <> ALL (ARRAY[
                   'schema', 'activation_id', 'system_id', 'release_identity', 'mode',
                   'teardown_identity', 'completed_at'
               ])
           )
       ) THEN
        RAISE EXCEPTION 'external boot teardown evidence has unexpected fields'
            USING ERRCODE = '22023';
    END IF;
    IF v_operation IN ('activate', 'recover', 'resolve-conflict')
       AND EXISTS (
           SELECT 1
           FROM jsonb_array_elements(
               CASE WHEN jsonb_typeof(p_result #> '{evidence,objects}') = 'array'
                    THEN p_result #> '{evidence,objects}' ELSE '[]'::jsonb END
           ) AS item(value)
           WHERE jsonb_typeof(item.value) <> 'object'
              OR jsonb_typeof(item.value -> 'ref') IS DISTINCT FROM 'string'
              OR octet_length(item.value ->> 'ref') NOT BETWEEN 1 AND 1024
              OR EXISTS (
                  SELECT 1
                  FROM jsonb_object_keys(
                      CASE WHEN jsonb_typeof(item.value) = 'object'
                           THEN item.value ELSE '{}'::jsonb END
                  ) AS field
                  WHERE field <> 'ref'
              )
       ) THEN
        RAISE EXCEPTION 'external boot authority object evidence is invalid'
            USING ERRCODE = '22023';
    END IF;
    IF v_operation = 'release'
       AND (
           jsonb_typeof(p_result #> '{evidence,reserved_bytes}') IS DISTINCT FROM 'number'
           OR (p_result #> '{evidence,reserved_bytes}')::text !~ '^[1-9][0-9]*$'
           OR jsonb_typeof(p_result #> '{evidence,enumeration_complete}')
                IS DISTINCT FROM 'boolean'
           OR p_result #> '{evidence,enumeration_complete}' IS DISTINCT FROM 'true'::jsonb
           OR jsonb_typeof(p_result #> '{evidence,store_identity}') IS DISTINCT FROM 'object'
           OR jsonb_typeof(p_result #> '{evidence,store_identity,ref}')
                IS DISTINCT FROM 'string'
           OR octet_length(p_result #>> '{evidence,store_identity,ref}') NOT BETWEEN 1 AND 1024
           OR jsonb_typeof(p_result #> '{evidence,owner_key}') IS DISTINCT FROM 'object'
           OR jsonb_typeof(p_result #> '{evidence,owner_key,ref}') IS DISTINCT FROM 'string'
           OR octet_length(p_result #>> '{evidence,owner_key,ref}') NOT BETWEEN 1 AND 1024
           OR EXISTS (
               SELECT 1
               FROM jsonb_object_keys(
                   CASE WHEN jsonb_typeof(p_result #> '{evidence,store_identity}') = 'object'
                        THEN p_result #> '{evidence,store_identity}' ELSE '{}'::jsonb END
               ) AS field
               WHERE field <> 'ref'
           )
           OR EXISTS (
               SELECT 1
               FROM jsonb_object_keys(
                   CASE WHEN jsonb_typeof(p_result #> '{evidence,owner_key}') = 'object'
                        THEN p_result #> '{evidence,owner_key}' ELSE '{}'::jsonb END
               ) AS field
               WHERE field <> 'ref'
           )
           OR EXISTS (
               SELECT 1
               FROM jsonb_array_elements(
                   CASE WHEN jsonb_typeof(p_result #> '{evidence,objects}') = 'array'
                        THEN p_result #> '{evidence,objects}' ELSE '[]'::jsonb END
               ) AS item(value)
               WHERE jsonb_typeof(item.value) <> 'object'
                  OR jsonb_typeof(item.value -> 'absent') IS DISTINCT FROM 'boolean'
                  OR item.value -> 'absent' IS DISTINCT FROM 'true'::jsonb
                  OR EXISTS (
                      SELECT 1
                      FROM jsonb_object_keys(
                          CASE WHEN jsonb_typeof(item.value) = 'object'
                               THEN item.value ELSE '{}'::jsonb END
                      ) AS field
                      WHERE field <> ALL (ARRAY['object', 'absent'])
                  )
                  OR jsonb_typeof(item.value -> 'object') IS DISTINCT FROM 'object'
                  OR jsonb_typeof(item.value #> '{object,ref}') IS DISTINCT FROM 'string'
                  OR octet_length(item.value #>> '{object,ref}') NOT BETWEEN 1 AND 1024
                  OR EXISTS (
                      SELECT 1
                      FROM jsonb_object_keys(
                          CASE WHEN jsonb_typeof(item.value -> 'object') = 'object'
                               THEN item.value -> 'object' ELSE '{}'::jsonb END
                      ) AS field
                      WHERE field <> 'ref'
                  )
           )
           OR EXISTS (
               SELECT 1
               FROM (
                   SELECT
                       to_jsonb(item.value #>> '{object,ref}')::text AS encoded_ref,
                       lag(to_jsonb(item.value #>> '{object,ref}')::text) OVER (
                           ORDER BY item.ordinality
                       ) AS previous_encoded_ref
                   FROM jsonb_array_elements(
                       CASE WHEN jsonb_typeof(p_result #> '{evidence,objects}') = 'array'
                            THEN p_result #> '{evidence,objects}' ELSE '[]'::jsonb END
                   ) WITH ORDINALITY AS item(value, ordinality)
               ) AS ordered_objects
               WHERE (previous_encoded_ref COLLATE "C") >= (encoded_ref COLLATE "C")
           )
       ) THEN
        RAISE EXCEPTION 'external boot release object evidence is invalid'
            USING ERRCODE = '22023';
    END IF;

    WITH RECURSIVE result_nodes(value) AS (
        SELECT p_result
        UNION ALL
        SELECT child.value
        FROM result_nodes AS node
        CROSS JOIN LATERAL (
            SELECT member.value
            FROM jsonb_each(
                CASE WHEN jsonb_typeof(node.value) = 'object'
                     THEN node.value ELSE '{}'::jsonb END
            ) AS member
            UNION ALL
            SELECT member.value
            FROM jsonb_array_elements(
                CASE WHEN jsonb_typeof(node.value) = 'array'
                     THEN node.value ELSE '[]'::jsonb END
            ) AS member
        ) AS child
    ), known_nodes(value) AS (
        SELECT seed.value
        FROM (
            VALUES
                (v_activation.materialization),
                (v_activation.recovery_point),
                (v_activation.pre_recovery_evidence),
                (v_activation.terminal_evidence)
        ) AS seed(value)
        UNION ALL
        SELECT child.value
        FROM known_nodes AS node
        CROSS JOIN LATERAL (
            SELECT member.value
            FROM jsonb_each(
                CASE WHEN jsonb_typeof(node.value) = 'object'
                     THEN node.value ELSE '{}'::jsonb END
            ) AS member
            UNION ALL
            SELECT member.value
            FROM jsonb_array_elements(
                CASE WHEN jsonb_typeof(node.value) = 'array'
                     THEN node.value ELSE '[]'::jsonb END
            ) AS member
        ) AS child
    ), known_refs(ref) AS (
        SELECT node.value ->> 'ref'
        FROM known_nodes AS node
        WHERE jsonb_typeof(node.value) = 'object'
          AND jsonb_typeof(node.value -> 'ref') = 'string'
        UNION
        SELECT reservation.store_identity
        FROM public.external_boot_reservations AS reservation
        WHERE reservation.activation_id = p_activation_id
        UNION
        SELECT reservation.owner_key
        FROM public.external_boot_reservations AS reservation
        WHERE reservation.activation_id = p_activation_id
        UNION
        SELECT release.store_identity
        FROM public.external_boot_reservation_releases AS release
        WHERE release.activation_id = p_activation_id
        UNION
        SELECT release.owner_key
        FROM public.external_boot_reservation_releases AS release
        WHERE release.activation_id = p_activation_id
    )
    SELECT EXISTS (
        SELECT 1
        FROM result_nodes AS node
        WHERE jsonb_typeof(node.value) = 'object'
          AND jsonb_typeof(node.value -> 'ref') = 'string'
          AND NOT EXISTS (
              SELECT 1 FROM known_refs WHERE known_refs.ref = node.value ->> 'ref'
          )
    ) INTO v_has_unknown_ref;
    IF v_has_unknown_ref THEN
        RAISE EXCEPTION 'external boot authority result contains an unknown reference'
            USING ERRCODE = '22023';
    END IF;

    FOR v_timestamp_text IN
        SELECT timestamp_value
        FROM (
            VALUES
                (CASE
                    WHEN v_operation IN ('activate', 'recover', 'resolve-conflict')
                        THEN p_result #>> '{evidence,observed_at}'
                    WHEN v_operation = 'release'
                        THEN p_result #>> '{evidence,verified_at}'
                    WHEN v_operation = 'cleanup'
                        THEN p_result #>> '{evidence,completed_at}'
                    WHEN v_operation = 'teardown'
                        THEN p_result #>> '{teardown_evidence,observed_at}'
                    ELSE NULL
                END),
                (CASE WHEN v_operation = 'teardown'
                    THEN p_result #>> '{cleanup_evidence,completed_at}' ELSE NULL END)
        ) AS timestamps(timestamp_value)
        WHERE timestamp_value IS NOT NULL
    LOOP
        IF v_timestamp_text !~ (
            '^[0-9]{4}-(0[1-9]|1[0-2])-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}'
            || '(\.[0-9]{1,6})?Z$'
        ) THEN
            RAISE EXCEPTION 'external boot evidence timestamp is invalid'
                USING ERRCODE = '22023';
        END IF;
        BEGIN
            PERFORM v_timestamp_text::timestamptz;
        EXCEPTION
            WHEN invalid_datetime_format OR datetime_field_overflow
                OR invalid_text_representation THEN
                RAISE EXCEPTION 'external boot evidence timestamp is invalid'
                    USING ERRCODE = '22023';
        END;
    END LOOP;

    IF (v_operation = 'activate' AND (
           v_system.state IS DISTINCT FROM 'ready'
           OR v_run.state IS DISTINCT FROM 'succeeded'
           OR v_activation.state IS DISTINCT FROM 'activating'
       ))
       OR (v_operation IN ('recover', 'resolve-conflict') AND (
           v_system.state NOT IN ('ready', 'crashed')
           OR v_run.state IS DISTINCT FROM 'succeeded'
           OR v_activation.state IS DISTINCT FROM 'recovering'
           OR v_attempt.state IS DISTINCT FROM 'recovering'
       ))
       OR (v_operation = 'release' AND (
           v_activation.state NOT IN (
               'active', 'recovered', 'abandoned', 'recovery_conflict', 'recovery_failed'
           )
           OR v_release.activation_id IS NOT NULL
       ))
       OR (v_operation = 'cleanup' AND (
           v_activation.state NOT IN (
               'recovered', 'abandoned', 'recovery_conflict', 'recovery_failed'
           )
           OR v_activation.cleanup_complete
       ))
       OR (v_operation = 'teardown' AND (
           v_system.state IS DISTINCT FROM 'failed'
           OR v_activation.state NOT IN ('recovery_conflict', 'recovery_failed')
           OR v_activation.cleanup_complete
       ))
       OR (v_operation = 'deadline' AND (
           (p_purpose = 'activate' AND (
               v_system.state IS DISTINCT FROM 'ready'
               OR v_run.state IS DISTINCT FROM 'succeeded'
               OR v_activation.state IS DISTINCT FROM 'activating'
           ))
           OR (p_purpose <> 'activate' AND (
               v_system.state NOT IN ('ready', 'crashed')
               OR v_run.state IS DISTINCT FROM 'succeeded'
               OR v_activation.state IS DISTINCT FROM 'recovering'
               OR v_attempt.state IS DISTINCT FROM 'recovering'
           ))
       ))
       OR (v_operation = 'recovery-attempt' AND (
           v_system.state NOT IN ('ready', 'crashed')
           OR v_run.state IS DISTINCT FROM 'succeeded'
           OR v_activation.state IS DISTINCT FROM 'active'
       ))
       OR (v_operation = 'fail' AND (
           (p_purpose = 'activate' AND (
               v_system.state IS DISTINCT FROM 'ready'
               OR v_run.state IS DISTINCT FROM 'succeeded'
               OR v_activation.state NOT IN ('prepared', 'activating')
           ))
           OR (p_purpose = 'recover' AND (
               v_system.state NOT IN ('ready', 'crashed')
               OR v_run.state IS DISTINCT FROM 'succeeded'
               OR v_activation.state NOT IN ('active', 'recovering')
           ))
           OR (p_purpose = 'resolve-conflict' AND (
               v_system.state NOT IN ('ready', 'crashed', 'failed')
               OR v_run.state IS DISTINCT FROM 'succeeded'
               OR v_activation.state IS DISTINCT FROM 'recovery_conflict'
           ))
           OR (p_purpose = 'release' AND (
               v_run.state IS DISTINCT FROM 'succeeded'
               OR v_activation.state NOT IN (
                   'active', 'recovered', 'abandoned',
                   'recovery_conflict', 'recovery_failed'
               )
           ))
           OR (p_purpose = 'teardown' AND (
               v_system.state IS DISTINCT FROM 'failed'
               OR v_activation.state NOT IN ('recovery_conflict', 'recovery_failed')
           ))
       )) THEN
        RETURN QUERY SELECT 'superseded'::text, NULL::text;
        RETURN;
    END IF;

    IF v_operation = 'cleanup' THEN
        v_evidence := p_result -> 'evidence';
        IF p_purpose <> 'release'
           OR v_activation.state NOT IN (
               'recovered', 'abandoned', 'recovery_conflict', 'recovery_failed'
           )
           OR v_evidence ->> 'schema'
                IS DISTINCT FROM 'external-boot-cleanup-evidence-v1'
           OR v_evidence ->> 'activation_id' IS DISTINCT FROM p_activation_id::text
           OR v_evidence ->> 'system_id' IS DISTINCT FROM p_system_id::text
           OR v_evidence ->> 'release_identity' IS NULL
           OR v_evidence ->> 'release_identity' !~ '^sha256:[0-9a-f]{64}$'
           OR v_evidence ->> 'release_identity' IS DISTINCT FROM v_release.release_identity
           OR jsonb_typeof(v_evidence -> 'completed_at') IS DISTINCT FROM 'string'
           OR (
               v_activation.state IN ('recovered', 'abandoned')
               AND (
                   v_evidence ->> 'mode' IS DISTINCT FROM 'ordinary'
                   OR v_evidence ->> 'teardown_identity' IS NOT NULL
               )
           )
           OR (
               v_activation.state IN ('recovery_conflict', 'recovery_failed')
               AND (
                   v_evidence ->> 'mode' IS DISTINCT FROM 'system_teardown'
                   OR v_evidence ->> 'teardown_identity' IS NULL
                   OR v_evidence ->> 'teardown_identity' !~ '^sha256:[0-9a-f]{64}$'
               )
           ) THEN
            RAISE EXCEPTION 'external boot cleanup evidence is invalid'
                USING ERRCODE = '22023';
        END IF;
        UPDATE public.external_boot_activations
        SET cleanup_complete = true, cleanup_evidence = v_evidence
        WHERE id = p_activation_id;
    ELSIF v_operation = 'teardown' THEN
        IF p_purpose <> 'teardown'
           OR v_activation.state NOT IN ('recovery_conflict', 'recovery_failed')
           OR v_release.activation_id IS NULL
           OR v_release.release_identity IS NULL
           OR v_release.release_identity !~ '^sha256:[0-9a-f]{64}$'
           OR p_result -> 'teardown_evidence' ->> 'schema'
                IS DISTINCT FROM 'external-boot-teardown-evidence-v1'
           OR p_result -> 'teardown_evidence' ->> 'system_id'
                IS DISTINCT FROM p_system_id::text
           OR p_result -> 'teardown_evidence' ->> 'system_state'
                IS DISTINCT FROM 'torn_down'
           OR jsonb_typeof(p_result -> 'teardown_evidence' -> 'observed_at')
                IS DISTINCT FROM 'string'
           OR p_result -> 'cleanup_evidence' ->> 'schema'
                IS DISTINCT FROM 'external-boot-cleanup-evidence-v1'
           OR p_result -> 'cleanup_evidence' ->> 'activation_id'
                IS DISTINCT FROM p_activation_id::text
           OR p_result -> 'cleanup_evidence' ->> 'system_id'
                IS DISTINCT FROM p_system_id::text
           OR p_result -> 'cleanup_evidence' ->> 'mode'
                IS DISTINCT FROM 'system_teardown'
           OR p_result -> 'cleanup_evidence' ->> 'release_identity'
                IS DISTINCT FROM v_release.release_identity
           OR p_result -> 'cleanup_evidence' ->> 'teardown_identity' IS NULL
           OR p_result -> 'cleanup_evidence' ->> 'teardown_identity'
                !~ '^sha256:[0-9a-f]{64}$'
           OR jsonb_typeof(p_result -> 'cleanup_evidence' -> 'completed_at')
                IS DISTINCT FROM 'string' THEN
            RAISE EXCEPTION 'external boot teardown evidence is invalid'
                USING ERRCODE = '22023';
        END IF;
        UPDATE public.systems SET state = 'torn_down' WHERE id = p_system_id;
        UPDATE public.external_boot_activations
        SET cleanup_complete = true,
            teardown_evidence = p_result -> 'teardown_evidence',
            cleanup_evidence = p_result -> 'cleanup_evidence'
        WHERE id = p_activation_id;
    ELSIF v_operation = 'activate' THEN
        v_evidence := p_result -> 'evidence';
        IF p_purpose <> 'activate'
           OR v_evidence ->> 'schema'
                IS DISTINCT FROM 'external-boot-terminal-evidence-v1'
           OR v_evidence ->> 'activation_id' IS DISTINCT FROM p_activation_id::text
           OR v_evidence ->> 'system_id' IS DISTINCT FROM p_system_id::text
           OR v_evidence ->> 'outcome' IS DISTINCT FROM 'active'
           OR v_evidence ->> 'composite_state' IS NULL
           OR v_evidence ->> 'composite_state' !~ '^sha256:[0-9a-f]{64}$'
           OR jsonb_typeof(v_evidence -> 'objects') IS DISTINCT FROM 'array'
           OR jsonb_typeof(v_evidence -> 'observed_at') IS DISTINCT FROM 'string'
           OR NOT (p_result ? 'activation_readiness_deadline') THEN
            RAISE EXCEPTION 'external boot activation evidence is invalid'
                USING ERRCODE = '22023';
        END IF;
        BEGIN
            v_deadline := (p_result ->> 'activation_readiness_deadline')::timestamptz;
            IF v_deadline IS NULL THEN
                RAISE invalid_parameter_value;
            END IF;
        EXCEPTION
            WHEN invalid_datetime_format OR datetime_field_overflow
                OR invalid_text_representation OR invalid_parameter_value THEN
                RAISE EXCEPTION 'external boot activation deadline is invalid'
                    USING ERRCODE = '22023';
        END;
        UPDATE public.external_boot_activations
        SET state = 'active', terminal_evidence = v_evidence,
            activation_readiness_deadline = v_deadline
        WHERE id = p_activation_id;
    ELSIF v_operation IN ('recover', 'resolve-conflict') THEN
        v_evidence := p_result -> 'evidence';
        IF p_purpose <> v_operation OR v_attempt.attempt_id IS NULL
           OR v_evidence ->> 'schema'
                IS DISTINCT FROM 'external-boot-terminal-evidence-v1'
           OR v_evidence ->> 'activation_id' IS DISTINCT FROM p_activation_id::text
           OR v_evidence ->> 'system_id' IS DISTINCT FROM p_system_id::text
           OR v_evidence ->> 'outcome' IS DISTINCT FROM 'recovered'
           OR v_evidence ->> 'composite_state' IS NULL
           OR v_evidence ->> 'composite_state' !~ '^sha256:[0-9a-f]{64}$'
           OR jsonb_typeof(v_evidence -> 'objects') IS DISTINCT FROM 'array'
           OR jsonb_typeof(v_evidence -> 'observed_at') IS DISTINCT FROM 'string'
           OR v_attempt.authority_generation <> p_generation THEN
            RAISE EXCEPTION 'external boot recovery evidence is invalid'
                USING ERRCODE = '22023';
        END IF;
        UPDATE public.external_boot_recovery_attempts
        SET state = 'recovered', terminal_evidence = v_evidence,
            conflict_evidence = NULL, recovery_readiness_deadline = NULL
        WHERE activation_id = p_activation_id AND attempt_id = v_attempt.attempt_id;
        UPDATE public.external_boot_activations
        SET state = 'recovered', terminal_evidence = v_evidence
        WHERE id = p_activation_id;
    ELSIF v_operation = 'release' THEN
        v_evidence := p_result -> 'evidence';
        IF p_purpose <> 'release'
           OR p_result ->> 'release_identity' IS NULL
           OR p_result ->> 'release_identity' !~ '^sha256:[0-9a-f]{64}$'
           OR v_evidence ->> 'schema'
                IS DISTINCT FROM 'external-boot-release-evidence-v1'
           OR v_evidence ->> 'activation_id' IS DISTINCT FROM p_activation_id::text
           OR v_evidence ->> 'system_id' IS DISTINCT FROM p_system_id::text
           OR v_evidence #>> '{store_identity,ref}' IS NULL
           OR v_evidence #>> '{owner_key,ref}' IS NULL
           OR v_evidence ->> 'reserved_bytes' IS NULL
           OR v_evidence ->> 'enumeration_complete' IS DISTINCT FROM 'true'
           OR jsonb_typeof(v_evidence -> 'objects') IS DISTINCT FROM 'array'
           OR jsonb_typeof(v_evidence -> 'verified_at') IS DISTINCT FROM 'string'
           OR NOT EXISTS (
               SELECT 1 FROM public.external_boot_reservations AS reservation
               WHERE reservation.activation_id = p_activation_id
                 AND reservation.state = 'ready'
                 AND reservation.store_identity = v_evidence #>> '{store_identity,ref}'
                 AND reservation.owner_key = v_evidence #>> '{owner_key,ref}'
                 AND reservation.reserved_bytes::text = v_evidence ->> 'reserved_bytes'
           ) THEN
            RAISE EXCEPTION 'external boot release evidence is invalid'
                USING ERRCODE = '22023';
        END IF;
        INSERT INTO public.external_boot_reservation_releases (
            activation_id, store_identity, owner_key, reserved_bytes,
            release_identity, release_evidence
        )
        SELECT r.activation_id, r.store_identity, r.owner_key, r.reserved_bytes,
               p_result ->> 'release_identity', v_evidence
        FROM public.external_boot_reservations AS r
        WHERE r.activation_id = p_activation_id AND r.state = 'ready';
        IF NOT FOUND THEN
            RAISE EXCEPTION 'external boot release reservation is not ready'
                USING ERRCODE = '22023';
        END IF;
        DELETE FROM public.external_boot_reservations
        WHERE activation_id = p_activation_id;
    ELSIF v_operation = 'deadline' THEN
        BEGIN
            IF NOT (p_result ? 'deadline') THEN
                RAISE invalid_parameter_value;
            END IF;
            v_deadline := (p_result ->> 'deadline')::timestamptz;
            IF v_deadline IS NULL THEN
                RAISE invalid_parameter_value;
            END IF;
        EXCEPTION
            WHEN invalid_datetime_format OR datetime_field_overflow
                OR invalid_text_representation OR invalid_parameter_value THEN
                RAISE EXCEPTION 'external boot deadline evidence is invalid'
                    USING ERRCODE = '22023';
        END;
        IF p_purpose = 'activate' THEN
            UPDATE public.external_boot_activations
            SET activation_readiness_deadline = v_deadline
            WHERE id = p_activation_id AND state IN ('activating', 'active');
        ELSE
            UPDATE public.external_boot_recovery_attempts
            SET recovery_readiness_deadline = v_deadline
            WHERE activation_id = p_activation_id
              AND attempt_id = v_activation.current_attempt_id
              AND state = 'recovering';
        END IF;
        IF NOT FOUND THEN
            RETURN QUERY SELECT 'superseded'::text, NULL::text;
            RETURN;
        END IF;
    ELSIF v_operation = 'recovery-attempt' THEN
        BEGIN
            IF p_result ->> 'recovery_basis' IS NULL
               OR p_result ->> 'recovery_basis' NOT IN ('recovery_point', 'pre_recovery')
               OR p_result ->> 'attempt_id' IS NULL
               OR p_result ->> 'deadline' IS NULL THEN
                RAISE invalid_parameter_value;
            END IF;
            v_deadline := (p_result ->> 'deadline')::timestamptz;
            INSERT INTO public.external_boot_recovery_attempts (
                activation_id, attempt_number, attempt_id, authority_generation,
                recovery_basis, recovery_readiness_deadline, state
            ) VALUES (
                p_activation_id,
                coalesce((
                    SELECT max(ra.attempt_number) + 1
                    FROM public.external_boot_recovery_attempts AS ra
                    WHERE ra.activation_id = p_activation_id
                ), 1),
                (p_result ->> 'attempt_id')::uuid,
                p_generation,
                p_result ->> 'recovery_basis',
                v_deadline,
                'recovering'
            );
        EXCEPTION
            WHEN invalid_datetime_format OR datetime_field_overflow
                OR invalid_text_representation OR invalid_parameter_value THEN
                RAISE EXCEPTION 'external boot recovery attempt facts are invalid'
                    USING ERRCODE = '22023';
        END;
        UPDATE public.external_boot_activations
        SET state = 'recovering', current_attempt_id = (p_result ->> 'attempt_id')::uuid
        WHERE id = p_activation_id;
    ELSIF v_operation = 'fail' THEN
        v_failure_context := p_result -> 'failure_context';
        IF NOT (p_result ? 'error_category')
           OR jsonb_typeof(p_result -> 'error_category') IS DISTINCT FROM 'string'
           OR p_result ->> 'error_category' NOT IN (
               'configuration_error', 'missing_dependency', 'build_failure', 'boot_timeout',
               'readiness_failure', 'debug_attach_failure', 'infrastructure_failure',
               'stale_handle', 'transport_conflict', 'not_implemented', 'allocation_denied',
               'lease_expired', 'provisioning_failure', 'install_failure',
               'transport_failure', 'control_failure', 'authorization_denied'
           )
           OR NOT (p_result ? 'failure_context')
           OR jsonb_typeof(v_failure_context) IS DISTINCT FROM 'object'
           OR pg_column_size(v_failure_context) > 32768
           OR NOT (p_result ? 'terminal')
           OR jsonb_typeof(p_result -> 'terminal') IS DISTINCT FROM 'boolean'
           OR EXISTS (
               SELECT 1
               FROM jsonb_object_keys(
                   CASE WHEN jsonb_typeof(v_failure_context) = 'object'
                        THEN v_failure_context ELSE '{}'::jsonb END
               ) AS field
               WHERE field <> 'phase'
           )
           OR (
               v_failure_context ? 'phase'
               AND (
                   jsonb_typeof(v_failure_context -> 'phase') IS DISTINCT FROM 'string'
                   OR v_failure_context ->> 'phase' NOT IN (
                       'admission', 'preparation', 'provider-call', 'observation', 'commit'
                   )
               )
           ) THEN
            RAISE EXCEPTION 'external boot failure context is invalid'
                USING ERRCODE = '22023';
        END IF;
        v_terminal := (p_result ->> 'terminal')::boolean OR v_job.attempt >= v_job.max_attempts;
        UPDATE public.jobs
        SET state = CASE WHEN v_terminal THEN 'failed' ELSE 'queued' END,
            error_category = CASE WHEN v_terminal THEN p_result ->> 'error_category' ELSE NULL END,
            failure_context = CASE WHEN v_terminal THEN v_failure_context ELSE '{}'::jsonb END,
            worker_id = CASE WHEN v_terminal THEN worker_id ELSE NULL END,
            lease_expires_at = CASE WHEN v_terminal THEN lease_expires_at ELSE NULL END,
            heartbeat_at = CASE WHEN v_terminal THEN heartbeat_at ELSE NULL END
        WHERE id = p_job_id
        RETURNING * INTO v_job;
        IF v_terminal THEN
            UPDATE public.runs
            SET state = 'failed', failure_category = p_result ->> 'error_category'
            WHERE id = p_run_id AND state IN ('created', 'running');
            v_outcome := 'result_failed';
        ELSE
            v_outcome := 'result_requeued';
        END IF;
    END IF;

    IF v_operation NOT IN ('deadline', 'recovery-attempt', 'fail') THEN
        UPDATE public.jobs
        SET state = 'succeeded', result_ref = v_result_ref
        WHERE id = p_job_id
        RETURNING * INTO v_job;
    ELSIF v_operation IN ('deadline', 'recovery-attempt') THEN
        SELECT j.* INTO v_job FROM public.jobs AS j WHERE j.id = p_job_id;
    END IF;
    IF v_operation NOT IN ('deadline', 'recovery-attempt') THEN
        UPDATE public.external_boot_authorities
        SET state = 'retired', retired_at = clock_timestamp()
        WHERE id = p_authority_id AND state = 'current';
    END IF;
    INSERT INTO public.external_boot_authority_audit (
        authority_id, system_id, allocation_id, activation_id, run_id, plan_identity, job_id,
        job_attempt, worker_incarnation, generation, purpose, provider_kind,
        authority_instance, operation, operation_identity, operation_digest,
        journal_sequence, journal_digest, outcome
    ) VALUES (
        p_authority_id, p_system_id, v_authority.allocation_id, p_activation_id,
        p_run_id, p_plan_identity, p_job_id, p_attempt, v_incarnation, p_generation, p_purpose,
        p_provider_kind, p_authority_instance, v_operation, p_operation_identity,
        p_operation_digest, p_journal_sequence, p_journal_digest, v_outcome
    );
    RETURN QUERY SELECT 'applied'::text, v_job.state;
END
$$;

REVOKE ALL ON TABLE
    public.external_boot_authority_counters,
    public.external_boot_authorities,
    public.external_boot_authority_acknowledgements,
    public.external_boot_authority_audit
FROM PUBLIC, kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness,
    kdive_provider_authority;

GRANT SELECT ON TABLE
    public.external_boot_authorities,
    public.external_boot_authority_acknowledgements
TO kdive_provider_authority;

REVOKE ALL ON FUNCTION
    public.reject_external_boot_authority_binding_change(),
    public.reject_external_boot_authority_immutable_row(),
    public.allocate_external_boot_authority(
        bytea, uuid, integer, uuid, uuid, uuid, text, text, text, text, text
    ),
    public.acknowledge_external_boot_authority(
        uuid, bigint, uuid, uuid, uuid, uuid, text, uuid, integer, text, text, text,
        text, text, text, text, bigint, text, text
    ),
    public.commit_external_boot_authority_result(
        bytea, uuid, integer, uuid, bigint, uuid, uuid, uuid, text, text, text, text,
        text, text, bigint, text, jsonb
    )
FROM PUBLIC, kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness,
    kdive_provider_authority;

GRANT EXECUTE ON FUNCTION public.allocate_external_boot_authority(
    bytea, uuid, integer, uuid, uuid, uuid, text, text, text, text, text
) TO kdive_worker;
GRANT EXECUTE ON FUNCTION public.acknowledge_external_boot_authority(
    uuid, bigint, uuid, uuid, uuid, uuid, text, uuid, integer, text, text, text,
    text, text, text, text, bigint, text, text
) TO kdive_provider_authority;
GRANT EXECUTE ON FUNCTION public.commit_external_boot_authority_result(
    bytea, uuid, integer, uuid, bigint, uuid, uuid, uuid, text, text, text, text,
    text, text, bigint, text, jsonb
) TO kdive_worker;
