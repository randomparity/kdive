-- Explicit process data authority and credential-bound job writes (ADR-0533, #1803).
--
-- This matrix is intentionally enumerated. New relations receive no runtime authority until
-- their owning process and operations are demonstrated in code and added here.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM
    kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM
    kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness;

GRANT SELECT ON TABLE
    public.allocations, public.artifacts, public.audit_log, public.budgets,
    public.build_artifact_gc_cursors, public.component_uploads,
    public.cost_class_coefficients, public.debug_sessions, public.egress_probe_guests,
    public.idempotency_keys, public.image_catalog, public.investigation_build_gc_cursor,
    public.investigation_build_tombstones, public.investigation_builds,
    public.investigations, public.inventory_overrides, public.jobs, public.ledger,
    public.object_write_leases, public.ops_control, public.platform_audit_log,
    public.provider_components, public.quotas, public.resources, public.rootfs_fetch_leases,
    public.run_steps, public.runs, public.snapshots, public.system_bootstrap_keys,
    public.system_object_sweep_cursors, public.system_shapes, public.systems,
    public.tool_invocation, public.upload_manifests
TO kdive_server;
GRANT INSERT ON TABLE
    public.allocations, public.artifacts, public.audit_log, public.budgets,
    public.component_uploads, public.cost_class_coefficients, public.debug_sessions,
    public.egress_probe_guests, public.idempotency_keys, public.image_catalog,
    public.investigation_builds, public.investigations, public.inventory_overrides,
    public.jobs, public.ledger, public.object_write_leases, public.ops_control,
    public.platform_audit_log, public.provider_components, public.quotas, public.resources,
    public.rootfs_fetch_leases, public.run_steps, public.runs, public.snapshots,
    public.system_bootstrap_keys, public.system_shapes, public.systems,
    public.tool_invocation, public.upload_manifests
TO kdive_server;
GRANT UPDATE ON TABLE
    public.allocations, public.artifacts, public.budgets, public.component_uploads,
    public.cost_class_coefficients, public.debug_sessions, public.egress_probe_guests,
    public.idempotency_keys, public.image_catalog, public.investigation_builds,
    public.investigations, public.inventory_overrides, public.jobs,
    public.object_write_leases, public.ops_control, public.provider_components,
    public.quotas, public.resources, public.rootfs_fetch_leases, public.run_steps,
    public.runs, public.snapshots, public.system_bootstrap_keys, public.system_shapes,
    public.systems, public.upload_manifests
TO kdive_server;
GRANT DELETE ON TABLE
    public.artifacts, public.component_uploads, public.debug_sessions,
    public.egress_probe_guests, public.idempotency_keys, public.image_catalog,
    public.inventory_overrides, public.provider_components, public.resources,
    public.rootfs_fetch_leases, public.run_steps, public.snapshots,
    public.system_bootstrap_keys, public.system_shapes, public.tool_invocation,
    public.upload_manifests
TO kdive_server;

GRANT SELECT ON TABLE
    public.allocations, public.artifacts, public.budgets, public.component_uploads,
    public.cost_class_coefficients, public.debug_sessions,
    public.egress_probe_guests, public.image_catalog,
    public.investigation_build_tombstones, public.investigation_builds,
    public.investigations, public.jobs, public.ledger, public.object_write_leases,
    public.ops_control, public.provider_components, public.quotas, public.resources,
    public.rootfs_fetch_leases, public.run_steps, public.runs, public.snapshots,
    public.system_bootstrap_keys, public.system_shapes, public.systems,
    public.upload_manifests
TO kdive_worker;
GRANT INSERT ON TABLE
    public.artifacts, public.component_uploads, public.egress_probe_guests, public.ledger,
    public.object_write_leases, public.rootfs_fetch_leases, public.run_steps,
    public.snapshots, public.upload_manifests
TO kdive_worker;
GRANT UPDATE ON TABLE
    public.allocations, public.artifacts, public.budgets, public.component_uploads,
    public.debug_sessions, public.egress_probe_guests, public.image_catalog,
    public.investigation_builds, public.investigations, public.run_steps, public.runs,
    public.snapshots, public.systems, public.upload_manifests
TO kdive_worker;
GRANT DELETE ON TABLE
    public.artifacts, public.object_write_leases, public.rootfs_fetch_leases, public.run_steps,
    public.snapshots, public.system_bootstrap_keys, public.upload_manifests
TO kdive_worker;

GRANT SELECT ON TABLE
    public.allocations, public.artifacts, public.budgets,
    public.build_artifact_gc_cursors, public.component_uploads,
    public.cost_class_coefficients, public.debug_sessions,
    public.egress_probe_guests, public.idempotency_keys, public.image_catalog,
    public.investigation_build_gc_cursor, public.investigation_build_tombstones,
    public.investigation_builds, public.investigations, public.inventory_overrides,
    public.jobs, public.ledger, public.object_write_leases, public.ops_control,
    public.provider_components, public.quotas, public.resources, public.rootfs_fetch_leases,
    public.run_steps, public.runs, public.snapshots, public.system_bootstrap_keys,
    public.system_object_sweep_cursors, public.system_shapes, public.systems,
    public.upload_manifests
TO kdive_reconciler;
GRANT INSERT ON TABLE
    public.artifacts, public.cost_class_coefficients, public.image_catalog,
    public.investigation_build_tombstones, public.inventory_overrides, public.jobs,
    public.ledger, public.resources
TO kdive_reconciler;
GRANT UPDATE ON TABLE
    public.allocations, public.budgets, public.build_artifact_gc_cursors,
    public.cost_class_coefficients, public.debug_sessions, public.egress_probe_guests,
    public.image_catalog, public.investigation_build_gc_cursor,
    public.investigation_builds, public.investigations, public.jobs, public.resources,
    public.runs, public.snapshots, public.system_object_sweep_cursors, public.systems,
    public.upload_manifests
TO kdive_reconciler;
GRANT DELETE ON TABLE
    public.artifacts, public.idempotency_keys, public.image_catalog,
    public.investigation_builds,
    public.inventory_overrides, public.jobs, public.object_write_leases, public.resources,
    public.rootfs_fetch_leases, public.run_steps, public.snapshots,
    public.system_bootstrap_keys, public.upload_manifests
TO kdive_reconciler;

CREATE FUNCTION heartbeat_worker_job(
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
    v_lease_expires_at timestamptz;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_worker', 'member') THEN
        RAISE EXCEPTION 'worker authority is required' USING ERRCODE = '42501';
    END IF;
    IF p_job_id IS NULL
       OR p_credential_hash IS NULL
       OR octet_length(p_credential_hash) <> 32
       OR p_attempt IS NULL
       OR p_attempt < 1
       OR p_lease IS NULL
       OR p_lease <= interval '0 seconds' THEN
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
      AND w.fence_protocol = 2
    FOR UPDATE;
    IF NOT FOUND THEN
        RETURN false;
    END IF;

    UPDATE public.jobs
    SET heartbeat_at = now(), lease_expires_at = now() + p_lease
    WHERE id = p_job_id
      AND worker_id = v_incarnation
      AND attempt = p_attempt
      AND state = 'running'
    RETURNING lease_expires_at INTO v_lease_expires_at;
    IF NOT FOUND THEN
        RETURN false;
    END IF;

    UPDATE public.investigation_build_uses
    SET lease_expires_at = v_lease_expires_at
    WHERE job_id = p_job_id
      AND holder_worker_id = v_incarnation
      AND attempt = p_attempt;
    RETURN true;
END
$$;

CREATE FUNCTION complete_worker_job(
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
      AND w.fence_protocol = 2
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

CREATE FUNCTION fail_worker_job(
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
      AND w.fence_protocol = 2
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
        worker_id = CASE
            WHEN p_terminal OR attempt >= max_attempts THEN worker_id ELSE NULL
        END,
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

REVOKE ALL ON FUNCTION
    heartbeat_worker_job(uuid, bytea, integer, interval),
    complete_worker_job(uuid, bytea, integer, text),
    fail_worker_job(uuid, bytea, integer, text, jsonb, boolean)
FROM PUBLIC, kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness;
GRANT EXECUTE ON FUNCTION
    heartbeat_worker_job(uuid, bytea, integer, interval),
    complete_worker_job(uuid, bytea, integer, text),
    fail_worker_job(uuid, bytea, integer, text, jsonb, boolean)
TO kdive_worker;
